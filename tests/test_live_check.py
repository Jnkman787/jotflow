import asyncio
import io
import logging
from typing import Any

import aiohttp
import pytest
from slack_sdk.errors import SlackApiError

from jotflow.live_check import (
    EXIT_FAILED,
    EXIT_PASSED,
    BoltLiveSlack,
    LiveCheckError,
    LiveIdentity,
    OnMessage,
    marked_message_text,
    run_live_check,
)
from jotflow.orchestrator.ingest import BotIdentity
from jotflow.slack_auth import ApiResult
from jotflow.slack_manifest import BOT_SCOPES

CHANNEL = "C0TEST1"
NONCE = "jf-abc123"
TS = "1700000000.000100"
BOT = BotIdentity(user_id="U0BOT", bot_id="B0BOT")


def own_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": CHANNEL,
        "user": "U0BOT",
        "bot_id": "B0BOT",
        "text": marked_message_text(NONCE),
        "ts": TS,
    }
    event.update(overrides)
    return event


class FakeLiveSlack:
    """Scripted Slack for the steps; records what the check asked for."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.member = True
        self.identity_error: LiveCheckError | None = None
        self.connect_error: LiveCheckError | None = None
        self.post_error: LiveCheckError | None = None
        self.delete_error: LiveCheckError | None = None
        self.events: list[dict[str, Any]] = [own_event()]
        self.posted_text: str | None = None
        self._on_message: OnMessage | None = None

    async def identity(self) -> LiveIdentity:
        self.calls.append("identity")
        if self.identity_error:
            raise self.identity_error
        return LiveIdentity(bot=BOT, name="jotbot", team="Example", scopes_verified=True)

    async def is_member(self, channel_id: str) -> bool:
        self.calls.append(f"is_member {channel_id}")
        return self.member

    async def connect(self, on_message: OnMessage) -> None:
        self.calls.append("connect")
        if self.connect_error:
            raise self.connect_error
        self._on_message = on_message

    async def post(self, channel_id: str, text: str) -> str:
        self.calls.append(f"post {channel_id}")
        if self.post_error:
            raise self.post_error
        self.posted_text = text
        assert self._on_message is not None
        on_message = self._on_message
        events = self.events
        # Deliver the events the way Socket Mode does: after the post, asynchronously.
        loop = asyncio.get_running_loop()
        for event in events:
            loop.call_soon(on_message, event)
        return TS

    async def delete(self, channel_id: str, ts: str) -> None:
        self.calls.append(f"delete {channel_id} {ts}")
        if self.delete_error:
            raise self.delete_error

    async def close(self) -> None:
        self.calls.append("close")


async def check(slack: FakeLiveSlack, timeout: float = 1.0) -> tuple[int, str]:
    out = io.StringIO()
    code = await run_live_check(slack, CHANNEL, out=out, timeout=timeout, nonce=NONCE)
    return code, out.getvalue()


async def test_passing_check_runs_every_step_and_cleans_up() -> None:
    slack = FakeLiveSlack()
    code, out = await check(slack)
    assert code == EXIT_PASSED
    assert slack.calls == [
        "identity",
        f"is_member {CHANNEL}",
        "connect",
        f"post {CHANNEL}",
        f"delete {CHANNEL} {TS}",
        "close",
    ]
    for step in ("bot token", "membership", "socket mode", "post", "receive", "own message"):
        assert f"ok    {step}:" in out
    assert "ok    redacted log: message channel=C0TEST1 ts=1700000000.000100" in out
    assert "ok    clean up: test message deleted" in out
    assert "Live check passed." in out
    assert slack.posted_text is not None
    assert NONCE in slack.posted_text


async def test_message_text_never_reaches_the_report() -> None:
    _, out = await check(FakeLiveSlack())
    # The nonce is only in the message text; the report prints pointers.
    assert NONCE not in out


async def test_unrelated_events_are_skipped() -> None:
    slack = FakeLiveSlack()
    slack.events = [own_event(ts="1.1"), own_event(channel="C0OTHER"), own_event()]
    code, _ = await check(slack)
    assert code == EXIT_PASSED


async def test_legacy_bot_message_subtype_still_passes() -> None:
    slack = FakeLiveSlack()
    slack.events = [own_event(subtype="bot_message")]
    code, out = await check(slack)
    assert code == EXIT_PASSED
    assert "ok    own message: ignored (subtype 'bot_message')" in out


async def test_bad_token_fails_before_anything_is_posted() -> None:
    slack = FakeLiveSlack()
    slack.identity_error = LiveCheckError("The bot token is missing this scope: pins:write.")
    code, out = await check(slack)
    assert code == EXIT_FAILED
    assert "FAIL  slack: The bot token is missing this scope: pins:write." in out
    assert slack.calls == ["identity", "close"]


async def test_non_member_fails_with_invite_hint() -> None:
    slack = FakeLiveSlack()
    slack.member = False
    code, out = await check(slack)
    assert code == EXIT_FAILED
    assert "FAIL  membership: not a member of channel C0TEST1; invite the bot there with " in out
    assert "/invite @jotbot" in out
    assert not any(call.startswith("post") for call in slack.calls)
    assert slack.calls[-1] == "close"


async def test_socket_mode_failure_posts_nothing() -> None:
    slack = FakeLiveSlack()
    slack.connect_error = LiveCheckError("Slack refused the Socket Mode connection (invalid_auth)")
    code, out = await check(slack)
    assert code == EXIT_FAILED
    assert "FAIL  slack: Slack refused the Socket Mode connection" in out
    assert not any(call.startswith(("post", "delete")) for call in slack.calls)


async def test_failed_post_needs_no_clean_up() -> None:
    slack = FakeLiveSlack()
    slack.post_error = LiveCheckError("chat.postMessage failed (not_in_channel)")
    code, out = await check(slack)
    assert code == EXIT_FAILED
    assert "FAIL  slack: chat.postMessage failed (not_in_channel)" in out
    assert not any(call.startswith("delete") for call in slack.calls)
    assert slack.calls[-1] == "close"


async def test_missing_event_times_out_and_still_deletes() -> None:
    slack = FakeLiveSlack()
    slack.events = []
    code, out = await check(slack, timeout=0.05)
    assert code == EXIT_FAILED
    assert "FAIL  receive: no event for the test message within 0.05s" in out
    assert "message.channels and message.groups" in out
    assert f"delete {CHANNEL} {TS}" in slack.calls
    assert "ok    clean up: test message deleted" in out


async def test_own_message_accepted_is_a_failure() -> None:
    slack = FakeLiveSlack()
    # A payload that does not carry the bot's ids, so the filter cannot tell it apart.
    slack.events = [own_event(user="U0SOMEONE", bot_id=None)]
    code, out = await check(slack)
    assert code == EXIT_FAILED
    assert "FAIL  own message: JotFlow accepted its own message" in out
    assert f"delete {CHANNEL} {TS}" in slack.calls


async def test_text_in_the_log_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import jotflow.live_check as live

    def leaky(decision: Any) -> None:
        logging.getLogger("jotflow.ingest").info("message text=%s", marked_message_text(NONCE))

    monkeypatch.setattr(live, "log_decision", leaky)
    code, out = await check(FakeLiveSlack())
    assert code == EXIT_FAILED
    assert "FAIL  redacted log: the log line contains message text" in out


async def test_log_without_pointers_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import jotflow.live_check as live

    def vague(decision: Any) -> None:
        logging.getLogger("jotflow.ingest").info("message received")

    monkeypatch.setattr(live, "log_decision", vague)
    code, out = await check(FakeLiveSlack())
    assert code == EXIT_FAILED
    assert "FAIL  redacted log: the log line lacks the channel id or ts" in out


async def test_message_ignored_for_others_is_a_failure() -> None:
    slack = FakeLiveSlack()
    slack.events = [own_event(subtype="channel_join")]
    code, out = await check(slack)
    assert code == EXIT_FAILED
    assert "FAIL  redacted log: a message from someone else would be ignored" in out


async def test_failed_delete_names_the_message_and_fails() -> None:
    slack = FakeLiveSlack()
    slack.delete_error = LiveCheckError("chat.delete failed (cant_delete_message)")
    code, out = await check(slack)
    assert code == EXIT_FAILED
    assert f"FAIL  clean up: chat.delete failed (cant_delete_message). Delete message ts={TS}" in (
        out
    )
    assert "Live check passed." not in out


async def test_capture_restores_the_ingest_logger() -> None:
    logger = logging.getLogger("jotflow.ingest")
    before = (logger.level, list(logger.handlers))
    await check(FakeLiveSlack())
    assert (logger.level, list(logger.handlers)) == before


# BoltLiveSlack, against fake Bolt objects: no network.


def slack_error(error: str) -> SlackApiError:
    return SlackApiError(error, {"ok": False, "error": error})


class FakeApi:
    def __init__(self, body: dict[str, Any] | None = None, scopes: Any = "all") -> None:
        self.body = body or {
            "ok": True,
            "team": "Example",
            "user": "jotbot",
            "user_id": "U0BOT",
            "bot_id": "B0BOT",
        }
        self.scopes = frozenset(BOT_SCOPES) if scopes == "all" else scopes

    def call(self, method: str, token: str) -> ApiResult:
        assert method == "auth.test"
        return ApiResult(self.body, self.scopes)


class FakeClient:
    def __init__(self) -> None:
        self.errors: dict[str, BaseException] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.member = True

    def _maybe_fail(self, method: str) -> None:
        if method in self.errors:
            raise self.errors[method]

    async def auth_test(self) -> dict[str, Any]:
        self._maybe_fail("auth_test")
        return {"user_id": "U0BOT", "bot_id": "B0BOT", "user": "jotbot"}

    async def conversations_info(self, channel: str) -> dict[str, Any]:
        self._maybe_fail("conversations_info")
        return {"ok": True, "channel": {"is_member": self.member}}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("chat_postMessage", kwargs))
        self._maybe_fail("chat_postMessage")
        return {"ok": True, "ts": TS}

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("chat_delete", kwargs))
        self._maybe_fail("chat_delete")
        return {"ok": True}


class FakeApp:
    def __init__(self) -> None:
        self.client = FakeClient()
        self.listeners: dict[str, Any] = {}

    def event(self, name: str) -> Any:
        def register(fn: Any) -> Any:
            self.listeners[name] = fn
            return fn

        return register


class FakeHandler:
    def __init__(self, app: Any, app_token: str, error: BaseException | None = None) -> None:
        self.app_token = app_token
        self.error = error
        self.calls: list[str] = []

    async def connect_async(self) -> None:
        self.calls.append("connect")
        if self.error:
            raise self.error

    async def close_async(self) -> None:
        self.calls.append("close")


def adapter(
    api: FakeApi | None = None, handler_error: BaseException | None = None
) -> tuple[BoltLiveSlack, FakeApp, list[FakeHandler]]:
    app = FakeApp()
    handlers: list[FakeHandler] = []

    def factory(bolt_app: Any, app_token: str) -> FakeHandler:
        assert bolt_app is app
        handler = FakeHandler(bolt_app, app_token, handler_error)
        handlers.append(handler)
        return handler

    slack = BoltLiveSlack(
        "xoxb-test",
        "xapp-test",
        api=api or FakeApi(),
        app=app,  # type: ignore[arg-type]
        handler_factory=factory,
    )
    return slack, app, handlers


async def test_adapter_identity_checks_scopes_and_ids() -> None:
    slack, _, _ = adapter()
    who = await slack.identity()
    assert who == LiveIdentity(bot=BOT, name="jotbot", team="Example", scopes_verified=True)


async def test_adapter_identity_reports_missing_scopes() -> None:
    slack, _, _ = adapter(api=FakeApi(scopes=frozenset({"chat:write"})))
    with pytest.raises(LiveCheckError, match="missing these scopes"):
        await slack.identity()


async def test_adapter_identity_reports_an_unreachable_slack() -> None:
    slack, app, _ = adapter()
    app.client.errors["auth_test"] = aiohttp.ClientConnectionError("no route")
    with pytest.raises(LiveCheckError, match="could not reach Slack"):
        await slack.identity()


async def test_adapter_membership() -> None:
    slack, app, _ = adapter()
    assert await slack.is_member(CHANNEL)
    app.client.member = False
    assert not await slack.is_member(CHANNEL)
    app.client.errors["conversations_info"] = slack_error("missing_scope")
    with pytest.raises(LiveCheckError, match="channels:read"):
        await slack.is_member(CHANNEL)


async def test_adapter_connect_routes_messages_and_close_disconnects() -> None:
    slack, app, handlers = adapter()
    received: list[Any] = []
    await slack.connect(received.append)
    [handler] = handlers
    assert handler.app_token == "xapp-test"
    await app.listeners["message"](event=own_event())
    assert received == [own_event()]
    await slack.close()
    await slack.close()  # closing twice is harmless
    assert handler.calls == ["connect", "close"]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (slack_error("invalid_auth"), "Slack refused the Socket Mode connection"),
        (aiohttp.ClientConnectionError("no route"), "could not reach Slack for Socket Mode"),
    ],
)
async def test_adapter_connect_failures(error: BaseException, message: str) -> None:
    slack, _, _ = adapter(handler_error=error)
    with pytest.raises(LiveCheckError, match=message):
        await slack.connect(lambda event: None)


async def test_adapter_post_and_delete() -> None:
    slack, app, _ = adapter()
    assert await slack.post(CHANNEL, "hello") == TS
    await slack.delete(CHANNEL, TS)
    assert app.client.calls == [
        ("chat_postMessage", {"channel": CHANNEL, "text": "hello"}),
        ("chat_delete", {"channel": CHANNEL, "ts": TS}),
    ]


@pytest.mark.parametrize("method", ["chat_postMessage", "chat_delete"])
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (slack_error("not_in_channel"), "failed \\(not_in_channel\\)"),
        (TimeoutError(), "could not reach Slack"),
    ],
)
async def test_adapter_write_failures(method: str, error: BaseException, message: str) -> None:
    slack, app, _ = adapter()
    app.client.errors[method] = error
    with pytest.raises(LiveCheckError, match=message):
        if method == "chat_postMessage":
            await slack.post(CHANNEL, "hello")
        else:
            await slack.delete(CHANNEL, TS)


async def test_adapter_post_without_ts_is_an_error() -> None:
    slack, app, _ = adapter()

    async def no_ts(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    app.client.chat_postMessage = no_ts  # type: ignore[method-assign]
    with pytest.raises(LiveCheckError, match="no ts"):
        await slack.post(CHANNEL, "hello")


def test_adapter_builds_a_bolt_app_that_keeps_self_events() -> None:
    slack = BoltLiveSlack("xoxb-test", "xapp-test", api=FakeApi())
    # The check must receive the bot's own message, so Bolt's self filter is off.
    middleware = [type(m).__name__ for m in slack._app._async_middleware_list]
    assert "AsyncIgnoringSelfEvents" not in middleware
