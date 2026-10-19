"""The redacting logger (#25): no message text and no secret at the default level."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from jotflow.commands import run as run_command
from jotflow.logs import (
    BODIES_WARNING,
    THIRD_PARTY_LOGGERS,
    BodiesRefused,
    RedactingFormatter,
    check_bodies_allowed,
    configure_logging,
    redact,
)
from jotflow.orchestrator.ingest import BotIdentity, EventFilter, log_decision
from jotflow.testing.threads import builtin_paths, load_thread


# Made-up secrets in each shape the formatter must catch. They are assembled at
# runtime so this file never holds a token-shaped string for scanners to flag.
def _fake(prefix: str, body: str) -> str:
    return prefix + body


SECRETS = {
    "slack bot": _fake("xox" + "b-", "1234567890-0987654321-" + "Ab" * 8),
    "slack user": _fake("xox" + "p-", "1234567890-0987654321-" + "Cd" * 3),
    "slack app": _fake("xa" + "pp-", "1-A0EXAMPLE-1234567890-" + "ef" * 8),
    "slack refresh": _fake("xox" + "e-", "1-" + "gh" * 8),
    "jotflow run": _fake("jf" + "r_", "a" * 43),
    "jotflow operator": _fake("jf" + "o_", "b-" * 21 + "c"),
    "github classic": _fake("gh" + "p_", "A1b2C3d4E5f6G7h8I9j0" * 2),
    "github fine-grained": _fake("github" + "_pat_", "11ABCDEFG0" + "x" * 50),
    "github app": _fake("gh" + "s_", "Z9" * 18),
    "anthropic": _fake("sk-" + "ant-", "api03-" + "Qx9_" * 20 + "AA"),
    "openai project": _fake("s" + "k-", "proj-" + "Ab3-" * 30),
    "openai legacy": _fake("s" + "k-", "Zz8" * 16),
}

MODEL_KEYS = ("anthropic", "openai project", "openai legacy")


@pytest.mark.parametrize("name", MODEL_KEYS)
@pytest.mark.parametrize(
    "template",
    [
        "starting agent with OPENAI_API_KEY={key} and nothing else",
        '{{"api_key": "{key}", "model": "gpt-5.5"}}',
        "x-api-key:{key};retry=1",
        "GET /v1?key={key}&page=2 failed",
    ],
)
def test_a_model_key_inside_a_longer_line_is_redacted(name: str, template: str) -> None:
    key = SECRETS[name]
    line = template.format(key=key)
    out = redact(line, bodies=False)
    assert key not in out
    assert key[:12] not in out  # no prefix of the key survives either
    assert "[redacted token]" in out
    before, _, after = template.partition("{key}")
    assert out.startswith(before.replace("{{", "{")) and out.endswith(after.replace("}}", "}"))


def test_ordinary_words_that_start_with_sk_are_left_alone() -> None:
    line = "installed sk-learn and sk-ant-colony, then ask-me-anything"
    assert redact(line, bodies=False) == line


@pytest.fixture(autouse=True)
def restore_logging() -> object:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in (*THIRD_PARTY_LOGGERS, "jotflow")}
    yield None
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, value in levels.items():
        logging.getLogger(name).setLevel(value)


@pytest.mark.parametrize("secret", SECRETS.values(), ids=SECRETS.keys())
@pytest.mark.parametrize("bodies", [False, True])
def test_secrets_are_always_redacted(secret: str, bodies: bool) -> None:
    line = f"connecting with {secret}, then {secret}."
    out = redact(line, bodies=bodies)
    assert secret not in out
    assert out.count("[redacted token]") == 2


@pytest.mark.parametrize(
    "line",
    [
        "headers={'Authorization': 'Bearer some-opaque-token.value'}",
        "Authorization: Bearer abc.def-ghi",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",  # gitleaks:allow  # noqa: E501
        "-----BEGIN PRIVATE KEY-----\nMIIEvQ\n-----END PRIVATE KEY-----",  # gitleaks:allow
    ],
)
def test_bearer_values_and_private_keys_are_redacted(line: str) -> None:
    out = redact(line, bodies=True)
    assert "some-opaque" not in out and "abc.def" not in out and "MIIE" not in out


SECRET_TEXT = "the launch code is swordfish, it's under the mat"


@pytest.mark.parametrize(
    "line",
    [
        json.dumps({"type": "message", "text": SECRET_TEXT, "ts": "1.0"}),
        json.dumps({"event": {"text": SECRET_TEXT + ' with a "quote"'}}),
        repr({"type": "message", "text": SECRET_TEXT}),
        repr({"text": "plain words only"}).replace("plain words only", SECRET_TEXT),
        f"chat_postMessage(channel='C0EXAMPLE1', text={SECRET_TEXT!r})",
        f'chat_postMessage(text="{SECRET_TEXT}")',
        f"body: channel=C0EXAMPLE1&text={SECRET_TEXT.replace(' ', '+')}&ts=1.0",
        # JSON serialized inside JSON, with an escaped quote inside the value.
        json.dumps({"body": json.dumps({"text": SECRET_TEXT + ' and a "quote"', "ts": "1"})}),
    ],
)
def test_payload_text_is_redacted_by_default_and_kept_with_bodies(line: str) -> None:
    assert "swordfish" not in redact(line)
    assert "[redacted]" in redact(line)
    assert "swordfish" in redact(line, bodies=True)


@pytest.mark.parametrize(
    "line",
    [
        "message channel=C0EXAMPLE1 ts=1792310400.000100 thread_ts=None",
        "Node.js context=next textual=1",
        "mcp method=tools/call tool=link_message run=run-1 status=200 outcome=served ms=3",
    ],
)
def test_ordinary_lines_pass_unchanged(line: str) -> None:
    assert redact(line) == line


def test_the_formatter_redacts_tracebacks_and_stacks() -> None:
    formatter = RedactingFormatter()
    token = SECRETS["slack bot"]
    try:
        raise RuntimeError(f"auth failed for {token} on {json.dumps({'text': SECRET_TEXT})}")
    except RuntimeError:
        record = logging.LogRecord("slack_sdk", logging.ERROR, __file__, 1, "%r", (token,), None)
        import sys

        record.exc_info = sys.exc_info()
        record.stack_info = f"Stack: called with {token}"
    out = formatter.format(record)
    assert token not in out
    assert "swordfish" not in out
    assert "RuntimeError" in out


def test_configure_logging_caps_libraries_and_installs_the_formatter() -> None:
    configure_logging("debug")
    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, RedactingFormatter)
    assert handler.formatter.bodies is False
    for name in THIRD_PARTY_LOGGERS:
        assert logging.getLogger(name).level == logging.INFO
    configure_logging("warning")
    assert all(logging.getLogger(n).level == logging.WARNING for n in THIRD_PARTY_LOGGERS)


def test_log_bodies_lifts_the_cap_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("debug", bodies=True)
    assert all(logging.getLogger(n).level == logging.DEBUG for n in THIRD_PARTY_LOGGERS)
    logging.getLogger("slack_sdk").debug("payload %s", json.dumps({"text": SECRET_TEXT}))
    logging.getLogger("slack_sdk").debug("token %s", SECRETS["slack bot"])
    err = capsys.readouterr().err
    assert BODIES_WARNING in err
    assert "swordfish" in err
    assert SECRETS["slack bot"] not in err


@pytest.mark.parametrize("env", [{"CI": "true"}, {"JOTFLOW_IMAGE": "1"}])
def test_log_bodies_is_refused_in_ci_and_in_the_image(env: dict[str, str]) -> None:
    with pytest.raises(BodiesRefused):
        check_bodies_allowed(env)
    check_bodies_allowed({"CI": "false", "JOTFLOW_IMAGE": "0"})


def test_jotflow_run_refuses_log_bodies_in_ci(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CI", "true")
    args = argparse.Namespace(log_level="info", log_bodies=True)
    assert run_command.run(args) == run_command.EXIT_CONFIG
    assert "--log-bodies never runs in CI" in capsys.readouterr().err


# The representative events: every built-in fixture thread.

PATHS = list(builtin_paths())


@pytest.mark.parametrize("path", PATHS, ids=[p.stem for p in PATHS])
def test_no_fixture_text_or_token_reaches_the_default_output(
    path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    thread = load_thread(path)
    configure_logging("info")
    bolt, sdk = logging.getLogger("slack_bolt"), logging.getLogger("slack_sdk")
    bot = thread.workspace.bot
    events = EventFilter(thread.policy.channels, BotIdentity(bot.user_id, bot.bot_id))
    for event in thread.events:
        payload = event.model_dump(exclude_none=True)
        # What libraries print: whole payloads at DEBUG, and pieces at INFO.
        bolt.debug("Received event: %r", payload)
        sdk.info("Received body: %s", json.dumps({"event": payload}))
        sdk.info("calling with token=%s", SECRETS["slack bot"])
        log_decision(events.classify(payload))
    out = capsys.readouterr().err
    texts = [m.text for m in thread.messages.values() if len(m.text) >= 6]
    assert texts, "the fixture should carry message text to hide"
    for text in texts:
        assert text not in out
        # Long single words would show a partial leak too.
        for word in (w for w in text.split() if len(w) >= 9 and w.isalpha()):
            assert word not in out, word
    assert SECRETS["slack bot"] not in out
    assert "message channel=" in out or "ignored" in out or "reaction channel=" in out


def test_the_bodies_warning_shows_even_at_a_quiet_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("error", bodies=True)
    assert BODIES_WARNING in capsys.readouterr().err


def test_escaped_json_keeps_what_follows_the_text_value() -> None:
    inner = json.dumps({"text": SECRET_TEXT + ' "q"', "ts": "1.000100"})
    out = redact(json.dumps({"body": inner}))
    assert "swordfish" not in out
    assert "1.000100" in out


CONTENT_KEYS = (
    "text",
    "fallback",
    "pretext",
    "title",
    "value",
    "footer",
    "comment",
    "preview",
    "plain_text",
)


@pytest.mark.parametrize("key", CONTENT_KEYS)
@pytest.mark.parametrize(
    "shape",
    [
        lambda key: json.dumps({"attachments": [{key: SECRET_TEXT}]}),
        lambda key: repr({"initial_comment": {key: SECRET_TEXT}}),
        lambda key: json.dumps({"event": json.dumps({key: SECRET_TEXT})}),
    ],
    ids=["json", "repr", "nested-json"],
)
def test_every_content_field_is_redacted(key: str, shape: object) -> None:
    line = shape(key)  # type: ignore[operator]
    out = redact(line)
    assert "swordfish" not in out
    assert "swordfish" in redact(line, bodies=True)


@pytest.mark.parametrize(
    "line",
    [
        "run 1a2b started (drafting)",
        "MCP server listening on 127.0.0.1:8765/mcp",
        "ignored: subtype 'channel_join' channel=C0EXAMPLE1",
        "subtitle=ok valued=1 commentary=none",
    ],
)
def test_lines_without_content_fields_are_unchanged(line: str) -> None:
    assert redact(line) == line
