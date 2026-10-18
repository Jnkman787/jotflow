"""The live smoke check behind ``jotflow check --live``.

One round trip against the real workspace in ``.env`` and ``jotflow.toml``:
verify the bot token and scopes, check channel membership, connect over
Socket Mode, post one marked test message, receive its event, confirm
JotFlow ignores its own message and logs a message without its text, then
delete the test message.

The steps talk to Slack only through :class:`LiveSlack`, so tests drive every
path with a fake. :class:`BoltLiveSlack` is the real adapter. Nothing here
names a workspace or channel; both come from the team's own configuration.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from jotflow.orchestrator.ingest import BotIdentity, EventFilter, Ignored, log_decision
from jotflow.orchestrator.slack import (
    NETWORK_ERRORS,
    SlackSetupError,
    fetch_identity,
    missing_channels,
)
from jotflow.slack_auth import (
    HttpSlackApi,
    SlackApi,
    SlackUnreachableError,
    TokenError,
    check_bot_token,
)

EXIT_PASSED = 0
EXIT_FAILED = 1

DEFAULT_TIMEOUT = 30.0

# A user id no real member has, for the "message from someone else" view.
_STRANGER = BotIdentity(user_id="U0LIVECHECK")


class LiveCheckError(Exception):
    """A Slack call failed. The message says what to fix."""


@dataclass(frozen=True)
class LiveIdentity:
    bot: BotIdentity
    name: str
    team: str
    scopes_verified: bool


OnMessage = Callable[[Mapping[str, Any]], None]


class LiveSlack(Protocol):
    async def identity(self) -> LiveIdentity: ...
    async def is_member(self, channel_id: str) -> bool: ...
    async def connect(self, on_message: OnMessage) -> None: ...
    async def post(self, channel_id: str, text: str) -> str: ...
    async def delete(self, channel_id: str, ts: str) -> None: ...
    async def close(self) -> None: ...


class StepFailed(Exception):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


class Report:
    """One line per step on the maintainer's terminal."""

    def __init__(self, out: TextIO) -> None:
        self._out = out

    def ok(self, step: str, detail: str) -> None:
        print(f"ok    {step}: {detail}", file=self._out, flush=True)

    def fail(self, step: str, reason: str) -> None:
        print(f"FAIL  {step}: {reason}", file=self._out, flush=True)

    def note(self, text: str) -> None:
        print(f"      {text}", file=self._out, flush=True)


def marked_message_text(nonce: str) -> str:
    return f"JotFlow live check {nonce}. This test message deletes itself in a few seconds."


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@contextmanager
def _captured_ingest_log() -> Iterator[_Capture]:
    logger = logging.getLogger("jotflow.ingest")
    capture = _Capture()
    previous = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous)


def _as_stranger(event: Mapping[str, Any]) -> dict[str, Any]:
    """The same payload as if a person, not the bot, had posted it."""
    copy = dict(event)
    copy.pop("bot_id", None)
    copy.pop("bot_profile", None)
    if copy.get("subtype") == "bot_message":
        copy.pop("subtype")
    return copy


def check_self_filter(event: Mapping[str, Any], channel_id: str, bot: BotIdentity) -> str:
    """JotFlow must ignore its own message. Returns the reason it gave."""
    decision = EventFilter([channel_id], bot).classify(event)
    if not isinstance(decision, Ignored):
        raise StepFailed(
            "own message", "JotFlow accepted its own message; its replies would wake it up again"
        )
    return decision.reason


def check_redaction(
    event: Mapping[str, Any], channel_id: str, ts: str, text: str, nonce: str
) -> str:
    """The log line for an accepted message carries pointers and never the text.

    The nonce appears only in the message text, so finding it in a log line
    means text leaked.
    """
    with _captured_ingest_log() as capture:
        decision = EventFilter([channel_id], _STRANGER).classify(_as_stranger(event))
        log_decision(decision)
    if isinstance(decision, Ignored):
        raise StepFailed(
            "redacted log",
            f"a message from someone else would be ignored ({decision.reason})",
        )
    for line in capture.lines:
        if text in line or nonce in line:
            raise StepFailed("redacted log", "the log line contains message text")
    [line] = capture.lines
    if channel_id not in line or ts not in line:
        raise StepFailed("redacted log", f"the log line lacks the channel id or ts: {line}")
    return line


async def _wait_for(
    queue: asyncio.Queue[Mapping[str, Any]], channel_id: str, ts: str, timeout: float
) -> Mapping[str, Any]:
    async def matching() -> Mapping[str, Any]:
        while True:
            event = await queue.get()
            if event.get("channel") == channel_id and event.get("ts") == ts:
                return event

    try:
        return await asyncio.wait_for(matching(), timeout)
    except TimeoutError:
        raise StepFailed(
            "receive",
            f"no event for the test message within {timeout:g}s. Check that the app "
            "subscribes to message.channels and message.groups, and that Socket Mode is on",
        ) from None


async def run_live_check(
    slack: LiveSlack,
    channel_id: str,
    *,
    out: TextIO,
    timeout: float = DEFAULT_TIMEOUT,
    nonce: str | None = None,
) -> int:
    """Run every step, stop at the first failure, and always clean up."""
    report = Report(out)
    # The prefix keeps the nonce from ever matching digits in a channel id or ts.
    nonce = nonce or f"jf-{secrets.token_hex(4)}"
    text = marked_message_text(nonce)
    posted: str | None = None
    result = EXIT_PASSED
    try:
        try:
            who = await slack.identity()
            scopes = "all manifest scopes" if who.scopes_verified else "scopes not reported"
            report.ok("bot token", f"@{who.name} in {who.team or 'the workspace'}, {scopes}")

            if not await slack.is_member(channel_id):
                raise StepFailed(
                    "membership",
                    f"not a member of channel {channel_id}; invite the bot there with "
                    f"/invite @{who.name}",
                )
            report.ok("membership", f"in channel {channel_id}")

            queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
            await slack.connect(queue.put_nowait)
            report.ok("socket mode", "connected")

            posted = await slack.post(channel_id, text)
            report.ok("post", f"test message ts={posted} in channel {channel_id}")

            event = await _wait_for(queue, channel_id, posted, timeout)
            report.ok("receive", "its event arrived over Socket Mode")

            reason = check_self_filter(event, channel_id, who.bot)
            report.ok("own message", f"ignored ({reason})")

            line = check_redaction(event, channel_id, posted, text, nonce)
            report.ok("redacted log", line)
        except LiveCheckError as exc:
            report.fail("slack", str(exc))
            result = EXIT_FAILED
        except StepFailed as exc:
            report.fail(exc.step, exc.reason)
            result = EXIT_FAILED
        finally:
            if posted is not None:
                try:
                    await slack.delete(channel_id, posted)
                    report.ok("clean up", "test message deleted")
                except LiveCheckError as exc:
                    report.fail("clean up", f"{exc}. Delete message ts={posted} by hand")
                    result = EXIT_FAILED
    finally:
        await slack.close()
    if result == EXIT_PASSED:
        report.note("Live check passed.")
    return result


def _slack_error(method: str, exc: SlackApiError) -> LiveCheckError:
    return LiveCheckError(f"{method} failed ({exc.response.get('error', 'unknown error')})")


def _unreachable(method: str, exc: BaseException) -> LiveCheckError:
    return LiveCheckError(f"could not reach Slack for {method} ({type(exc).__name__})")


HandlerFactory = Callable[[AsyncApp, str], Any]


class BoltLiveSlack:
    """The real adapter over slack-bolt, used only by ``jotflow check --live``."""

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        *,
        api: SlackApi | None = None,
        app: AsyncApp | None = None,
        handler_factory: HandlerFactory = AsyncSocketModeHandler,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._api = api or HttpSlackApi()
        # Bolt drops the bot's own events by default. This check needs to
        # receive exactly that event, so the filter is off for this app only.
        self._app = app or AsyncApp(
            token=bot_token,
            logger=logging.getLogger("slack_bolt"),
            ignoring_self_events_enabled=False,
        )
        self._handler_factory = handler_factory
        self._handler: Any = None

    async def identity(self) -> LiveIdentity:
        try:
            checked = await asyncio.to_thread(check_bot_token, self._api, self._bot_token)
            bot, name = await fetch_identity(self._app.client)
        except TokenError as exc:
            raise LiveCheckError(str(exc)) from None
        except (SlackUnreachableError, SlackSetupError) as exc:
            raise LiveCheckError(str(exc)) from None
        return LiveIdentity(
            bot=bot, name=name, team=checked.team, scopes_verified=checked.scopes_verified
        )

    async def is_member(self, channel_id: str) -> bool:
        try:
            return not await missing_channels(self._app.client, [channel_id])
        except SlackSetupError as exc:
            raise LiveCheckError(str(exc)) from None

    async def connect(self, on_message: OnMessage) -> None:
        async def listener(event: dict[str, Any]) -> None:
            on_message(event)

        self._app.event("message")(listener)
        self._handler = self._handler_factory(self._app, self._app_token)
        try:
            await self._handler.connect_async()
        except SlackApiError as exc:
            raise LiveCheckError(
                f"Slack refused the Socket Mode connection "
                f"({exc.response.get('error', 'unknown error')}); check JOTFLOW_SLACK_APP_TOKEN"
            ) from None
        except NETWORK_ERRORS as exc:
            raise _unreachable("Socket Mode", exc) from None

    async def post(self, channel_id: str, text: str) -> str:
        try:
            response = await self._app.client.chat_postMessage(channel=channel_id, text=text)
        except SlackApiError as exc:
            raise _slack_error("chat.postMessage", exc) from None
        except NETWORK_ERRORS as exc:
            raise _unreachable("chat.postMessage", exc) from None
        ts = response.get("ts")
        if not isinstance(ts, str):
            raise LiveCheckError("chat.postMessage returned no ts")
        return ts

    async def delete(self, channel_id: str, ts: str) -> None:
        try:
            await self._app.client.chat_delete(channel=channel_id, ts=ts)
        except SlackApiError as exc:
            raise _slack_error("chat.delete", exc) from None
        except NETWORK_ERRORS as exc:
            raise _unreachable("chat.delete", exc) from None

    async def close(self) -> None:
        if self._handler is not None:
            await self._handler.close_async()
            self._handler = None
