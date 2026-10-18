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
from jotflow.slack_manifest import BOT_SCOPES, render_manifest
from tests.fakes import GOOD_APP, GOOD_BOT, FakeSlackApi, auth_ok, unreachable


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


@pytest.fixture
def options(tmp_path: Path) -> InitOptions:
    return InitOptions(env_file=tmp_path / ".env", manifest_out=tmp_path / "manifest.json")


def test_fresh_setup_writes_env_and_manifest(options: InitOptions) -> None:
    script = Script(answers=["Idea Bot"], secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0

    assert read_env(options.env_file) == {BOT_TOKEN_KEY: GOOD_BOT, APP_TOKEN_KEY: GOOD_APP}
    assert stat.S_IMODE(options.env_file.stat().st_mode) == 0o600
    manifest = json.loads(options.manifest_out.read_text())
    assert manifest == render_manifest("Idea Bot")
    assert "api.slack.com/apps?new_app=1" in script.text
    assert "Connected to Slack as @jotflow in Example Team" in script.text
    assert "/invite @jotflow" in script.text


def test_tokens_are_never_printed(options: InitOptions) -> None:
    script = Script(answers=[""], secrets=[GOOD_BOT, GOOD_APP])
    run_init(script.console(), FakeSlackApi.working(), options)
    assert GOOD_BOT not in script.text
    assert GOOD_APP not in script.text
    assert all(GOOD_BOT not in p and GOOD_APP not in p for p in script.prompts)


def test_default_bot_name(options: InitOptions) -> None:
    script = Script(answers=[""], secrets=[GOOD_BOT, GOOD_APP])
    run_init(script.console(), FakeSlackApi.working(), options)
    assert json.loads(options.manifest_out.read_text())["display_information"]["name"] == (
        "JotFlow"
    )


def test_invalid_bot_name_is_asked_again(options: InitOptions) -> None:
    script = Script(answers=["<bad>", "Good Bot"], secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "may contain only letters" in script.text
    assert json.loads(options.manifest_out.read_text())["display_information"]["name"] == (
        "Good Bot"
    )


def test_missing_scope_is_named_and_a_new_token_is_accepted(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    weak = "xoxb-missing-pins"
    api.responses[("auth.test", weak)] = auth_ok([s for s in BOT_SCOPES if s != "pins:write"])
    script = Script(answers=[""], secrets=[weak, GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), api, options) == 0
    assert "missing this scope: pins:write" in script.text
    assert read_env(options.env_file)[BOT_TOKEN_KEY] == GOOD_BOT


def test_swapped_tokens_get_specific_messages(options: InitOptions) -> None:
    script = Script(answers=[""], secrets=[GOOD_APP, GOOD_BOT, GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert "That is an app-level token (xapp-)" in script.text
    assert "That is a bot token (xoxb-)" in script.text


def test_running_out_of_input_saves_nothing(options: InitOptions) -> None:
    script = Script(answers=[""], secrets=[GOOD_BOT])
    args = argparse.Namespace(env_file=options.env_file, manifest_out=options.manifest_out)
    assert init.run(args, console=script.console(), api=FakeSlackApi.working()) == 1
    assert not options.env_file.exists()
    assert "was not changed" in script.text


def test_too_many_bad_tokens_stops(options: InitOptions) -> None:
    script = Script(answers=[""], secrets=["xoxb-wrong"] * 5)
    args = argparse.Namespace(env_file=options.env_file, manifest_out=options.manifest_out)
    assert init.run(args, console=script.console(), api=FakeSlackApi.working()) == 1
    assert "Too many attempts" in script.text
    assert not options.env_file.exists()


def test_slack_unreachable_offers_retry(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    flaky = "xoxb-flaky"
    api.responses[("auth.test", flaky)] = unreachable()
    script = Script(answers=["", "y"], secrets=[flaky, GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), api, options) == 0
    assert "could not reach Slack" in script.text


def test_unverified_scopes_warn(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    api.responses[("auth.test", GOOD_BOT)] = auth_ok(None)
    script = Script(answers=[""], secrets=[GOOD_BOT, GOOD_APP])
    assert run_init(script.console(), api, options) == 0
    assert "could not be checked" in script.text


def test_rerun_keeps_working_tokens_without_asking_again(options: InitOptions) -> None:
    options.env_file.write_text(
        f"# mine\nOTHER=1\n{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n"
    )
    before = options.env_file.read_text()
    script = Script(answers=["", ""])
    assert run_init(script.console(), FakeSlackApi.working(), options) == 0
    assert script.text.count("Keep it?") == 0  # prompts go to the reader, not the output
    assert sum("Keep it?" in p for p in script.prompts) == 2
    assert not any("Paste" in p for p in script.prompts)
    assert not options.manifest_out.exists()
    assert options.env_file.read_text() == before


def test_rerun_can_replace_tokens(options: InitOptions) -> None:
    api = FakeSlackApi.working()
    new_bot, new_app = "xoxb-new", "xapp-new"
    api.responses[("auth.test", new_bot)] = auth_ok()
    api.responses[("apps.connections.open", new_app)] = api.responses[
        ("apps.connections.open", GOOD_APP)
    ]
    options.env_file.write_text(f"{BOT_TOKEN_KEY}={GOOD_BOT}\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    script = Script(answers=["n", "n", ""], secrets=[new_bot, new_app])
    assert run_init(script.console(), api, options) == 0
    assert read_env(options.env_file) == {BOT_TOKEN_KEY: new_bot, APP_TOKEN_KEY: new_app}


def test_rerun_with_revoked_token_starts_over(options: InitOptions) -> None:
    options.env_file.write_text(f"{BOT_TOKEN_KEY}=xoxb-revoked\n{APP_TOKEN_KEY}={GOOD_APP}\n")
    script = Script(answers=["y", ""], secrets=[GOOD_BOT])
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
