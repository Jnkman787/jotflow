from __future__ import annotations

import argparse
import io
import json
import re
import stat
import urllib.parse
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import pytest

from jotflow.commands import init
from jotflow.commands.init import (
    APP_TOKEN_KEY,
    BOT_TOKEN_KEY,
    COMPOSE_FILE_KEY,
    COMPOSE_FILE_VALUE,
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
    script = Script(answers=["", "Idea Bot", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0

    assert read_env(options.env_file) == {
        BOT_TOKEN_KEY: GOOD_BOT,
        APP_TOKEN_KEY: GOOD_APP,
        COMPOSE_FILE_KEY: COMPOSE_FILE_VALUE,
    }
    assert stat.S_IMODE(options.env_file.stat().st_mode) == 0o600
    manifest = json.loads(options.manifest_out.read_text())
    assert manifest == render_manifest("Idea Bot")
    assert "api.slack.com/apps?new_app=1" in script.text
    assert "Connected to Slack as @jotflow in Example Team" in script.text
    assert "/invite @jotflow" in script.text


def test_tokens_are_never_printed(options: InitOptions) -> None:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    run_init(script.console(), FakeSlackApi.working(), options)
    assert GOOD_BOT not in script.text
    assert GOOD_APP not in script.text
    assert all(GOOD_BOT not in p and GOOD_APP not in p for p in script.prompts)


def test_default_bot_name(options: InitOptions) -> None:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    run_init(script.console(), FakeSlackApi.working(), options)
    assert json.loads(options.manifest_out.read_text())["display_information"]["name"] == (
        "JotFlow"
    )


def test_invalid_bot_name_is_asked_again(options: InitOptions) -> None:
    script = Script(answers=["", "<bad>", "Good Bot", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "may contain only letters" in script.text
    assert json.loads(options.manifest_out.read_text())["display_information"]["name"] == (
        "Good Bot"
    )


def test_missing_scope_is_named_and_a_new_token_is_accepted(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    weak = "xoxb-missing-pins"
    api.responses[("auth.test", weak)] = auth_ok([s for s in BOT_SCOPES if s != "pins:write"])
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, weak, GOOD_BOT])
    assert run_init(script.console(), api, options) == 0
    assert "missing this scope: pins:write" in script.text
    assert read_env(options.env_file)[BOT_TOKEN_KEY] == GOOD_BOT


def test_swapped_tokens_get_specific_messages(options: InitOptions) -> None:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_BOT, GOOD_APP, GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "That is an app-level token (xapp-)" in script.text
    assert "That is a bot token (xoxb-)" in script.text


def test_running_out_of_input_saves_nothing(options: InitOptions) -> None:
    script = Script(answers=["", ""], secrets=[GOOD_BOT])
    args = _args(options)
    assert init.run(args, console=script.console(), api=FakeSlackApi.working()) == 1
    assert not options.env_file.exists()
    assert "were not changed" in script.text


def test_too_many_bad_tokens_stops(options: InitOptions) -> None:
    script = Script(answers=["", ""], secrets=["xoxb-wrong"] * 5)
    args = _args(options)
    assert init.run(args, console=script.console(), api=FakeSlackApi.working()) == 1
    assert "Too many attempts" in script.text
    assert not options.env_file.exists()


def test_slack_unreachable_offers_retry(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    flaky = "xoxb-flaky"
    api.responses[("auth.test", flaky)] = unreachable()
    script = Script(answers=["", "", "y", *POLICY], secrets=[GOOD_APP, flaky, GOOD_BOT])
    assert run_init(script.console(), api, options) == 0
    assert "could not reach Slack" in script.text


def test_unverified_scopes_warn(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    api.responses[("auth.test", GOOD_BOT)] = auth_ok(None)
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
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
    assert options.env_file.read_text() == before + f"\n{COMPOSE_FILE_KEY}={COMPOSE_FILE_VALUE}\n"
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
    script = Script(answers=["n", "n", "", "", *POLICY], secrets=[new_app, new_bot])
    assert run_init(script.console(), api, options) == 0
    assert read_env(options.env_file) == {
        BOT_TOKEN_KEY: new_bot,
        APP_TOKEN_KEY: new_app,
        COMPOSE_FILE_KEY: COMPOSE_FILE_VALUE,
    }


def test_rerun_with_revoked_token_starts_over(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}=xoxb-revoked\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    script = Script(answers=["y", "", "", *POLICY], secrets=[GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "no longer works" in script.text
    assert read_env(options.env_file) == {
        BOT_TOKEN_KEY: GOOD_BOT,
        APP_TOKEN_KEY: GOOD_APP,
        COMPOSE_FILE_KEY: COMPOSE_FILE_VALUE,
    }


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
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert stat.S_IMODE(options.config_file.stat().st_mode) == 0o644
    assert stat.S_IMODE(options.env_file.stat().st_mode) == 0o600
    policy = load_policy(options.config_file)
    assert list(policy.channels) == ["C0TEST1"]
    (repo,) = policy.channels["C0TEST1"].repos
    assert (repo.repo, repo.nickname, repo.description) == ("acme/web", "web", "The web app")
    assert repo.labels is None  # Enter: any existing label (#92)
    assert not repo.restricts_labels
    assert policy.agent.preset is None
    assert not policy.approval.approvers_enabled


def test_several_channels_and_repos_with_pasted_links(options: InitOptions) -> None:
    answers = [
        "",
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
        "none",  # no longer a choice: asked again
        "",
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
    script = Script(answers=answers, secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    policy = load_policy(options.config_file)
    assert list(policy.channels) == ["C0LINKS1", "G0PRIV2"]
    first = policy.channels["C0LINKS1"].repos
    assert [(r.repo, r.nickname, r.labels) for r in first] == [
        ("acme/Web-App", "web-app", ("bug", "size:s")),
        ("acme/api", "api", None),
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
    script = Script(answers=["", "", bad, *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert fragment in script.text


def test_invalid_repo_and_long_description_are_explained(options: InitOptions) -> None:
    answers = ["", "", "C0TEST1", "not a repo", "acme/web", "", "x" * 201, "Web", "", "", "", ""]
    script = Script(answers=answers, secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "owner/name" in script.text
    assert "200 characters" in script.text


def test_rerun_rewrite_keeps_other_settings_and_backs_up(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    options.config_file.write_text(EXAMPLE.read_text())
    original = options.config_file.read_text()
    # keep both tokens, rewrite the policy, keep the first channel's repos, drop the second
    answers = ["", "", "n", "C0EXAMPLE1", "", "", "codex-cli", "claude-code"]
    script = Script(answers=answers)
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0

    before = load_policy(EXAMPLE)
    after = load_policy(options.config_file)
    assert list(after.channels) == ["C0EXAMPLE1"]
    assert after.channels["C0EXAMPLE1"] == before.channels["C0EXAMPLE1"]
    assert after.agent.preset == "claude-code"
    assert "does not ship a 'codex-cli' preset yet" in script.text
    assert after.debounce_seconds == before.debounce_seconds
    assert after.issue_links == before.issue_links
    assert after.approval == before.approval
    assert options.config_file.with_name("jotflow.toml.bak").read_text() == original
    assert "a channel you leave out is removed" in script.text


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
    script = Script(answers=["", "", "C0TEST1", "acme/web"], secrets=[GOOD_APP, GOOD_BOT])
    assert init.run(_args(options), console=script.console(), api=FakeSlackApi.working()) == 1
    assert not options.env_file.exists()
    assert not options.config_file.exists()


def test_config_defaults_to_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOTFLOW_CONFIG", str(tmp_path / "custom.toml"))
    assert InitOptions().config_file == tmp_path / "custom.toml"


def test_compose_file_is_written_once(options: InitOptions) -> None:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    first = options.env_file.read_text()
    assert first.count(f"{COMPOSE_FILE_KEY}=") == 1
    assert "docker compose up -d" in script.text
    # a re-run keeping everything leaves .env byte for byte the same
    script = Script(answers=["", "", ""])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert options.env_file.read_text() == first


def test_custom_compose_file_is_kept(options: InitOptions) -> None:
    options.env_file.write_text(
        f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n{COMPOSE_FILE_KEY}=mine.yml\n"
    )
    script = Script(answers=["", "", *POLICY])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert read_env(options.env_file)[COMPOSE_FILE_KEY] == "mine.yml"
    assert "docker-compose.policy.yml" in script.text


def test_rewritten_policy_is_world_readable(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    options.config_file.write_text(EXAMPLE.read_text())
    options.config_file.chmod(0o600)
    script = Script(answers=["", "", "n", *POLICY])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert stat.S_IMODE(options.config_file.stat().st_mode) == 0o644


def test_keeping_an_unreadable_policy_warns(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    options.config_file.write_text(EXAMPLE.read_text())
    options.config_file.chmod(0o600)
    script = Script(answers=["", "", ""])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "chmod 644" in script.text
    assert stat.S_IMODE(options.config_file.stat().st_mode) == 0o600


# --- #90: every prompt explains itself ---------------------------------------

EXPLANATIONS = [
    init.EXPLAIN_EXISTING_APP,
    *init.EXPLAIN_BOT_NAME,
    init.EXPLAIN_CREATE_LINK,
    init.EXPLAIN_PASTE,
    *init.EXPLAIN_APP_TOKEN,
    *init.EXPLAIN_BOT_TOKEN,
    *init.EXPLAIN_CHANNEL,
    *init.EXPLAIN_REPO,
    init.EXPLAIN_NICKNAME,
    *init.EXPLAIN_DESCRIPTION,
    *init.EXPLAIN_LABELS,
    *init.EXPLAIN_PRESET,
]


def _fresh_session(options: InitOptions) -> Script:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    return script


def test_intro_comes_first(options: InitOptions) -> None:
    script = _fresh_session(options)
    assert script.text.startswith(init.INTRO)
    for words in (".env", "jotflow.toml", "Ctrl-C", "no secrets"):
        assert words in init.INTRO


def test_every_prompt_is_explained(options: InitOptions) -> None:
    script = _fresh_session(options)
    for line in EXPLANATIONS:
        assert line in script.text, line


def test_each_explanation_comes_just_before_its_prompt(options: InitOptions) -> None:
    script = _fresh_session(options)
    events: list[str] = []
    console = script.console()
    say, read, secret = console.say, console.read, console.read_secret

    def record_say(message: str = "") -> None:
        events.append(f"say:{message}")
        say(message)

    console.say = record_say  # type: ignore[method-assign]
    console.read = lambda p: (events.append(f"ask:{p}"), read(p))[1]
    console.read_secret = lambda p: (events.append(f"ask:{p}"), secret(p))[1]
    fresh = InitOptions(
        env_file=options.env_file.with_name("fresh.env"),
        manifest_out=options.manifest_out,
        config_file=options.config_file.with_name("fresh.toml"),
    )
    script.answers, script.secrets = ["", "", *POLICY], [GOOD_APP, GOOD_BOT]
    assert run_init(console, FakeSlackApi.working(), fresh) == 0
    pairs = {
        "Have you already created": init.EXPLAIN_EXISTING_APP,
        "Bot name": init.EXPLAIN_BOT_NAME[-1],
        "Paste the app-level token": init.EXPLAIN_PASTE,
        "Paste the bot token": init.EXPLAIN_PASTE,
        "Channel id": init.EXPLAIN_CHANNEL[-1],
        "  GitHub repo": init.EXPLAIN_REPO[-1],
        "  Nickname": init.EXPLAIN_NICKNAME,
        "  What it covers": init.EXPLAIN_DESCRIPTION[-1],
        "  Labels": init.EXPLAIN_LABELS[-1],
        "Agent preset": init.EXPLAIN_PRESET[-1],
    }
    for prompt, explanation in pairs.items():
        index = next(i for i, e in enumerate(events) if e.startswith(f"ask:{prompt}"))
        assert events[index - 1].removeprefix("say:").strip() == explanation, prompt


def test_token_prompts_name_the_slack_page_and_button() -> None:
    app = " ".join(init.EXPLAIN_APP_TOKEN)
    for words in (
        "Basic Information",
        "App-Level Tokens",
        "Generate Token and Scopes",
        "socket-mode",
        "connections:write",
        "xapp-",
    ):
        assert words in app
    bot = " ".join(init.EXPLAIN_BOT_TOKEN)
    for words in ("Install App", "Install to Workspace", "Allow", "Bot User OAuth Token", "xoxb-"):
        assert words in bot


def test_app_level_token_is_asked_before_the_bot_token(options: InitOptions) -> None:
    script = _fresh_session(options)
    pastes = [p for p in script.prompts if p.startswith("Paste")]
    assert [p.split(" (")[0] for p in pastes] == [
        "Paste the app-level token",
        "Paste the bot token",
    ]


def test_help_shows_the_intro() -> None:
    parser = argparse.ArgumentParser(prog="jotflow init")
    init.add_arguments(parser)
    assert init.INTRO in parser.format_help()


def test_examples_are_fictional(options: InitOptions) -> None:
    text = "\n".join([init.INTRO, *EXPLANATIONS, _fresh_session(options).text])
    for owner in re.findall(r"github\.com/([^/\s]+)/", text):
        assert owner == "acme-co", owner
    for owner in re.findall(r"(?<![\w./-])([a-z0-9-]+)/[a-z0-9-]+", " ".join(EXPLANATIONS)):
        assert owner in {"acme-co", "owner"}, owner
    assert "Idea Bot" in init.EXPLAIN_BOT_NAME[0]
    assert "#ideas" in init.INTRO


def test_none_is_not_a_label_choice(options: InitOptions) -> None:
    answers = ["", "", "C0TEST1", "acme-co/web", "", "The web app", "none", "", "", "", ""]
    script = Script(answers=answers, secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "Type any to allow every label the repo already has" in script.text
    (repo,) = load_policy(options.config_file).channels["C0TEST1"].repos
    assert repo.labels is None


def test_a_label_list_narrows(options: InitOptions) -> None:
    answers = ["", "", "C0TEST1", "acme-co/web", "", "The web app", "bug, needs-design", "", "", ""]
    script = Script(answers=answers, secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    (repo,) = load_policy(options.config_file).channels["C0TEST1"].repos
    assert repo.labels == ("bug", "needs-design")


def test_labels_answer_rules() -> None:
    assert init._labels("") == "any"
    assert init._labels(" ANY ") == "any"
    assert init._labels("bug, size:s") == "bug, size:s"
    assert init._split_labels("any") == ()
    assert init._split_labels("bug, size:s") == ("bug", "size:s")
    with pytest.raises(ValueError, match="Type any to allow every label"):
        init._labels("none")
    with pytest.raises(ValueError, match="listed twice"):
        init._labels("bug, Bug")


def test_none_for_labels_is_explained_and_asked_again(options: InitOptions) -> None:
    # POLICY's fifth answer is the labels prompt; answer none, then a list.
    answers = [*POLICY[:4], "none", "bug", *POLICY[5:]]
    script = Script(answers=["", "", *answers], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "Type any to allow every label the repo already has" in script.text
    (repo,) = load_policy(options.config_file).channels["C0TEST1"].repos
    assert repo.labels == ("bug",)


def test_labels_prompt_defaults_to_any(options: InitOptions) -> None:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    [labels_prompt] = [p for p in script.prompts if p.strip().startswith("Labels")]
    assert labels_prompt.strip() == "Labels [any]:"
    assert "Press Enter for any" in script.text


def test_any_writes_no_labels_line(options: InitOptions) -> None:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    text = options.config_file.read_text(encoding="utf-8")
    assert "labels" not in text.split("[[channels.C0TEST1.repos]]", 1)[1]


# --- #100: fresh-eyes fixes ---------------------------------------------------


def test_steps_are_numbered_like_the_readme(options: InitOptions) -> None:
    text = _fresh_session(options).text
    headings = [line for line in text.splitlines() if line.startswith("Setup step ")]
    assert headings == [f"Setup step {n} of 6: {init.STEP_TITLES[n]}" for n in range(1, 7)]
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for n, title in init.STEP_TITLES.items():
        assert f"{n}. **{title}" in readme, title


def test_an_existing_app_skips_the_create_link(options: InitOptions) -> None:
    script = Script(answers=["y", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "api.slack.com/apps?new_app" not in script.text
    assert "Open your existing app" in script.text
    assert not options.manifest_out.exists()
    assert "Setup step 1 of 6" not in script.text
    assert "Setup step 3 of 6" in script.text


def test_request_to_install_is_explained() -> None:
    bot = " ".join(init.EXPLAIN_BOT_TOKEN)
    for words in ("Request to Install", "Ctrl-C", "run setup again", "OAuth & Permissions"):
        assert words in bot


def test_paste_hint_comes_before_each_token(options: InitOptions) -> None:
    assert _fresh_session(options).text.count(init.EXPLAIN_PASTE) == 2


def test_a_channel_name_gets_a_clear_message(options: InitOptions) -> None:
    script = Script(answers=["", "", "#ideas", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "That is the channel's name. Setup needs its id" in script.text


def test_preset_defaults_to_none_and_names_the_first_preset(options: InitOptions) -> None:
    script = _fresh_session(options)
    [prompt] = [p for p in script.prompts if p.startswith("Agent preset")]
    assert "[none]" in prompt
    assert init.FIRST_PRESET in " ".join(init.EXPLAIN_PRESET)
    assert load_policy(options.config_file).agent.preset is None


def test_intro_states_retries_and_no_fixed_time() -> None:
    assert f"{init.MAX_ATTEMPTS} tries" in " ".join(init.INTRO.split())
    assert "five minutes" not in init.INTRO


@pytest.mark.parametrize(
    ("path", "windows"),
    [
        ("/mnt/c/Users/sam/jotflow/.env", True),
        ("/mnt/d/.env", True),
        ("/home/sam/jotflow/.env", False),
        ("/mnt/data/jotflow/.env", False),
    ],
)
def test_windows_drive_detection(path: str, windows: bool) -> None:
    assert init.on_windows_drive(Path(path)) is windows


def test_saved_tokens_message_on_a_windows_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init, "on_windows_drive", lambda path: True)
    message = init._saved_tokens_message(Path(".env"))
    assert "Windows drive" in message
    assert "Only your user" not in message
    monkeypatch.setattr(init, "on_windows_drive", lambda path: False)
    assert "Only your user can read that file" in init._saved_tokens_message(Path(".env"))


# --- #100 round 2 --------------------------------------------------------------


def test_windows_setup_writes_the_compose_separator(options: InitOptions) -> None:
    on_windows = replace(options, windows=True)
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), on_windows) == 0
    env = read_env(on_windows.env_file)
    assert env[init.COMPOSE_SEPARATOR_KEY] == ":"
    assert "COMPOSE_PATH_SEPARATOR=:" in script.text


def test_other_systems_do_not_get_the_separator(options: InitOptions) -> None:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), replace(options, windows=False)) == 0
    assert init.COMPOSE_SEPARATOR_KEY not in read_env(options.env_file)


def test_an_existing_separator_is_left_alone(options: InitOptions) -> None:
    options.env_file.write_text("COMPOSE_PATH_SEPARATOR=;\n")
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), replace(options, windows=True)) == 0
    assert read_env(options.env_file)[init.COMPOSE_SEPARATOR_KEY] == ";"


@pytest.mark.parametrize(
    "link",
    [
        "https://acme-co.slack.com/archives/C0IDEAS01",
        "https://acme-co.slack.com/archives/C0IDEAS01/p1760781600000100",
        "https://app.slack.com/client/T0ACME001/C0IDEAS01",
        "https://app.slack.com/client/T0ACME001/C0IDEAS01/thread/C0IDEAS01-1760781600.000100",
    ],
)
def test_channel_links_give_the_id(link: str) -> None:
    assert init._channel_id(link) == "C0IDEAS01"


def test_unsupported_presets_are_refused() -> None:
    assert init._preset_answer("None") == "none"
    assert init._preset_answer(" Claude-Code ") == "claude-code"
    with pytest.raises(ValueError, match="does not ship"):
        init._preset_answer("some-agent")


def test_the_reuse_question_does_not_assume_the_bot_name(options: InitOptions) -> None:
    script = Script(answers=["", "", *POLICY], secrets=[GOOD_APP, GOOD_BOT])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert any("a Slack app for JotFlow?" in p for p in script.prompts)
    assert init.INTRO.count("Ctrl-C") == 1 and "Ctrl-D" not in init.INTRO
