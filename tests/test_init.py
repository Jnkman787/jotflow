from __future__ import annotations

import argparse
import io
import json
import stat
import urllib.parse
from collections.abc import Iterable
from pathlib import Path

import pytest

from jotflow.commands import init
from jotflow.commands.init import (
    APP_TOKEN_KEY,
    BOT_TOKEN_KEY,
    Console,
    InitOptions,
    create_app_link,
    run_init,
)
from jotflow.envfile import read_env
from jotflow.policy import load_policy
from jotflow.slack_manifest import BOT_SCOPES, render_manifest
from tests.fakes import GOOD_APP, GOOD_BOT, FakeSlackApi, auth_ok, unreachable

EXAMPLE = Path(__file__).resolve().parents[1] / "jotflow.example.toml"


class Script:
    """A scripted terminal: answers prompts in order and records everything shown."""

    def __init__(self, answers: Iterable[str] = (), secrets: Iterable[str] = ()) -> None:
        self.answers = list(answers)
        self.secrets = list(secrets)
        self.prompts: list[str] = []
        self.out = io.StringIO()

    def _next(self, queue: list[str], prompt: str) -> str:
        self.prompts.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    def console(self) -> Console:
        return Console(
            read=lambda p: self._next(self.answers, p),
            read_secret=lambda p: self._next(self.secrets, p),
            out=self.out,
        )

    @property
    def text(self) -> str:
        return self.out.getvalue()


# One channel with one repo, accepting every default.
POLICY = ["C0TEST1", "acme/web", "", "The web app", "", "", "", ""]


@pytest.fixture
def options(tmp_path: Path) -> InitOptions:
    return InitOptions(
        env_file=tmp_path / ".env",
        manifest_out=tmp_path / "manifest.json",
        config_file=tmp_path / "jotflow.toml",
    )


def _args(options: InitOptions) -> argparse.Namespace:
    return argparse.Namespace(
        env_file=options.env_file, manifest_out=options.manifest_out, config=options.config_file
    )


def test_fresh_setup_writes_env_and_manifest(options: InitOptions) -> None:
    script = Script(answers=["Idea Bot", *POLICY], secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0

    assert read_env(options.env_file) == {BOT_TOKEN_KEY: GOOD_BOT, APP_TOKEN_KEY: GOOD_APP}
    assert stat.S_IMODE(options.env_file.stat().st_mode) == 0o600
    manifest = json.loads(options.manifest_out.read_text())
    assert manifest == render_manifest("Idea Bot")
    assert "api.slack.com/apps?new_app=1" in script.text
    assert "Connected to Slack as @jotflow in Example Team" in script.text
    assert "/invite @jotflow" in script.text


def test_tokens_are_never_printed(options: InitOptions) -> None:
    script = Script(answers=["", *POLICY], secrets=[GOOD_BOT, GOOD_APP])
    run_init(script.console(), FakeSlackApi.working(), options)
    assert GOOD_BOT not in script.text
    assert GOOD_APP not in script.text
    assert all(GOOD_BOT not in p and GOOD_APP not in p for p in script.prompts)


def test_default_bot_name(options: InitOptions) -> None:
    script = Script(answers=["", *POLICY], secrets=[GOOD_BOT, GOOD_APP])
    run_init(script.console(), FakeSlackApi.working(), options)
    assert json.loads(options.manifest_out.read_text())["display_information"]["name"] == (
        "JotFlow"
    )


def test_invalid_bot_name_is_asked_again(options: InitOptions) -> None:
    script = Script(answers=["<bad>", "Good Bot", *POLICY], secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "may contain only letters" in script.text
    assert json.loads(options.manifest_out.read_text())["display_information"]["name"] == (
        "Good Bot"
    )


def test_missing_scope_is_named_and_a_new_token_is_accepted(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    weak = "xoxb-missing-pins"
    api.responses[("auth.test", weak)] = auth_ok([s for s in BOT_SCOPES if s != "pins:write"])
    script = Script(answers=["", *POLICY], secrets=[weak, GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), api, options) == 0
    assert "missing this scope: pins:write" in script.text
    assert read_env(options.env_file)[BOT_TOKEN_KEY] == GOOD_BOT


def test_swapped_tokens_get_specific_messages(options: InitOptions) -> None:
    script = Script(answers=["", *POLICY], secrets=[GOOD_APP, GOOD_BOT, GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "That is an app-level token (xapp-)" in script.text
    assert "That is a bot token (xoxb-)" in script.text


def test_running_out_of_input_saves_nothing(options: InitOptions) -> None:
    script = Script(answers=[""], secrets=[GOOD_BOT])
    args = _args(options)
    assert init.run(args, console=script.console(), api=FakeSlackApi.working()) == 1
    assert not options.env_file.exists()
    assert "were not changed" in script.text


def test_too_many_bad_tokens_stops(options: InitOptions) -> None:
    script = Script(answers=[""], secrets=["xoxb-wrong"] * 5)
    args = _args(options)
    assert init.run(args, console=script.console(), api=FakeSlackApi.working()) == 1
    assert "Too many attempts" in script.text
    assert not options.env_file.exists()


def test_slack_unreachable_offers_retry(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    flaky = "xoxb-flaky"
    api.responses[("auth.test", flaky)] = unreachable()
    script = Script(answers=["", "y", *POLICY], secrets=[flaky, GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), api, options) == 0
    assert "could not reach Slack" in script.text


def test_unverified_scopes_warn(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    api.responses[("auth.test", GOOD_BOT)] = auth_ok(None)
    script = Script(answers=["", *POLICY], secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), api, options) == 0
    assert "could not be checked" in script.text


def test_rerun_keeps_working_tokens_without_asking_again(options: InitOptions) -> None:
    options.env_file.write_text(
        f"# mine\nOTHER=1\n{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n"
    )
    options.config_file.write_text(EXAMPLE.read_text())
    before = options.env_file.read_text()
    policy_before = options.config_file.read_text()
    script = Script(answers=["", "", ""])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert script.text.count("Keep it?") == 0  # prompts go to the reader, not the output
    assert sum("Keep it?" in p for p in script.prompts) == 3
    assert not any("Paste" in p for p in script.prompts)
    assert not options.manifest_out.exists()
    assert options.env_file.read_text() == before
    assert options.config_file.read_text() == policy_before
    assert not options.config_file.with_name("jotflow.toml.bak").exists()


def test_rerun_can_replace_tokens(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    new_bot, new_app = "xoxb-new", "xapp-new"
    api.responses[("auth.test", new_bot)] = auth_ok()
    api.responses[("apps.connections.open", new_app)] = api.responses[
        ("apps.connections.open", GOOD_APP)
    ]
    options.env_file.write_text(f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    script = Script(answers=["n", "n", "", *POLICY], secrets=[new_bot, new_app])
    assert run_init(script.console(), api, options) == 0
    assert read_env(options.env_file) == {BOT_TOKEN_KEY: new_bot, APP_TOKEN_KEY: new_app}


def test_rerun_with_revoked_token_starts_over(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}=xoxb-revoked\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    script = Script(answers=["y", "", *POLICY], secrets=[GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "no longer works" in script.text
    assert read_env(options.env_file) == {BOT_TOKEN_KEY: GOOD_BOT, APP_TOKEN_KEY: GOOD_APP}


def test_create_app_link_round_trips_the_manifest() -> None:
    manifest = '{"display_information": {"name": "Idea Bot"}}'
    link = create_app_link(manifest)
    assert link.startswith("https://api.slack.com/apps?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    assert query == {"new_app": ["1"], "manifest_json": [manifest]}


def test_refuses_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    args = argparse.Namespace(env_file=tmp_path / ".env", manifest_out=tmp_path / "m.json")
    assert init.run(args) == 2
    assert "interactive" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()


def test_fresh_setup_writes_a_valid_policy(options: InitOptions) -> None:
    script = Script(answers=["", *POLICY], secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    policy = load_policy(options.config_file)
    assert list(policy.channels) == ["C0TEST1"]
    (repo,) = policy.channels["C0TEST1"].repos
    assert (repo.repo, repo.nickname, repo.description) == ("acme/web", "web", "The web app")
    assert repo.labels == ("bug", "enhancement")
    assert policy.agent.preset == "claude-code"
    assert not policy.approval.approvers_enabled


def test_several_channels_and_repos_with_pasted_links(options: InitOptions) -> None:
    answers = [
        "",
        "https://example.slack.com/archives/C0LINKS1",
        "https://github.com/acme/Web-App.git",
        "",
        "Customer web app",
        "bug, size:s",
        "y",
        "acme/api",
        "web-app",  # taken: asked again
        "api",
        "The API",
        "none",
        "n",
        "y",
        "C0LINKS1",  # already listed: asked again
        "G0PRIV2",
        "acme/handbook",
        "",
        "Handbook",
        "",
        "",
        "n",
        "none",
    ]
    script = Script(answers=answers, secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    policy = load_policy(options.config_file)
    assert list(policy.channels) == ["C0LINKS1", "G0PRIV2"]
    first = policy.channels["C0LINKS1"].repos
    assert [(r.repo, r.nickname, r.labels) for r in first] == [
        ("acme/Web-App", "web-app", ("bug", "size:s")),
        ("acme/api", "api", ()),
    ]
    assert policy.agent.preset is None
    assert "already used in this channel" in script.text
    assert "C0LINKS1 is already listed" in script.text


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        ("general", "not a Slack channel id"),
        ("c0lower", "not a Slack channel id"),
        ("D0DIRECT", "not a Slack channel id"),
    ],
)
def test_invalid_channel_ids_are_explained(options: InitOptions, bad: str, fragment: str) -> None:
    script = Script(answers=["", bad, *POLICY], secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert fragment in script.text


def test_invalid_repo_and_long_description_are_explained(options: InitOptions) -> None:
    answers = ["", "C0TEST1", "not a repo", "acme/web", "", "x" * 201, "Web", "", "", "", ""]
    script = Script(answers=answers, secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "owner/name" in script.text
    assert "200 characters" in script.text


def test_rerun_rewrite_keeps_other_settings_and_backs_up(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    options.config_file.write_text(EXAMPLE.read_text())
    original = options.config_file.read_text()
    # keep both tokens, rewrite the policy, keep the first channel's repos, drop the second
    answers = ["", "", "n", "C0EXAMPLE1", "", "", "codex-cli"]
    script = Script(answers=answers)
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0

    before = load_policy(EXAMPLE)
    after = load_policy(options.config_file)
    assert list(after.channels) == ["C0EXAMPLE1"]
    assert after.channels["C0EXAMPLE1"] == before.channels["C0EXAMPLE1"]
    assert after.agent.preset == "codex-cli"
    assert after.debounce_seconds == before.debounce_seconds
    assert after.issue_links == before.issue_links
    assert after.approval == before.approval
    assert options.config_file.with_name("jotflow.toml.bak").read_text() == original


def test_invalid_existing_policy_can_be_kept(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    options.config_file.write_text("unknown_key = 1\n")
    script = Script(answers=["", "", "n"])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "unknown_key: unknown key" in script.text
    assert options.config_file.read_text() == "unknown_key = 1\n"


def test_invalid_existing_policy_can_be_replaced(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    options.config_file.write_text("not toml [\n")
    script = Script(answers=["", "", "", *POLICY])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert list(load_policy(options.config_file).channels) == ["C0TEST1"]
    assert options.config_file.with_name("jotflow.toml.bak").read_text() == "not toml [\n"


def test_stopping_during_policy_questions_saves_nothing(options: InitOptions) -> None:
    script = Script(answers=["", "C0TEST1", "acme/web"], secrets=[GOOD_BOT, GOOD_APP])
    assert init.run(_args(options), console=script.console(), api=FakeSlackApi.working()) == 1
    assert not options.env_file.exists()
    assert not options.config_file.exists()


def test_config_defaults_to_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOTFLOW_CONFIG", str(tmp_path / "custom.toml"))
    assert InitOptions().config_file == tmp_path / "custom.toml"
