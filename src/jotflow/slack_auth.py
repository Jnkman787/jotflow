"""Check Slack tokens before JotFlow stores them.

``jotflow init`` uses these checks so that a pasted token is either known to
work or rejected with a message that says what to fix. Every Slack call goes
through the small :class:`SlackApi` protocol, so tests pass a fake and never
touch the network.

Messages never include the token itself.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import Message
from typing import IO, Any, Protocol

from jotflow.slack_manifest import BOT_SCOPES

SLACK_API_URL = "https://slack.com/api/"
APP_TOKEN_SCOPE = "connections:write"


@dataclass(frozen=True)
class ApiResult:
    """One Slack Web API response.

    ``scopes`` holds the token's granted scopes from the ``X-OAuth-Scopes``
    header, or ``None`` when Slack did not send it.
    """

    body: Mapping[str, Any]
    scopes: frozenset[str] | None = None

    @property
    def ok(self) -> bool:
        return self.body.get("ok") is True

    @property
    def error(self) -> str:
        return str(self.body.get("error") or "unknown_error")


class SlackApi(Protocol):
    def call(self, method: str, token: str) -> ApiResult:
        """Call a Slack Web API method with ``token`` and return its response."""


class SlackUnreachableError(Exception):
    """Slack could not be reached at all."""


MAX_RESPONSE_BYTES = 1_000_000


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Slack's Web API never redirects, so a redirect is treated as an error.

    Following one could hand the token to whatever host the redirect names.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


class Opener(Protocol):
    def open(self, request: urllib.request.Request, /, *, timeout: float) -> Any:
        """Send ``request`` and return a response usable as a context manager."""


def _default_opener() -> Opener:
    opener: Opener = urllib.request.build_opener(_RefuseRedirects())
    return opener


class HttpSlackApi:
    """The real client: a POST to Slack's Web API using the standard library."""

    def __init__(
        self,
        base_url: str = SLACK_API_URL,
        timeout: float = 10.0,
        opener: Opener | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError(
                "the Slack API URL must use https://, so tokens are never sent in clear"
            )
        self._base_url = base_url
        self._timeout = timeout
        self._opener = opener or _default_opener()

    def call(self, method: str, token: str) -> ApiResult:
        request = urllib.request.Request(
            self._base_url + method,
            data=b"",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Unredirected: never copied onto a follow-up request to another URL.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                header = response.headers.get("X-OAuth-Scopes")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return ApiResult({"ok": False, "error": "ratelimited"})
            if 300 <= exc.code < 400:
                raise SlackUnreachableError(
                    f"Slack answered {method} with a redirect, which JotFlow does not follow"
                ) from None
            raise SlackUnreachableError(f"Slack answered HTTP {exc.code} for {method}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SlackUnreachableError(f"could not reach Slack: {exc}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SlackUnreachableError(f"Slack's response to {method} was unexpectedly large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise SlackUnreachableError(
                f"Slack sent a response that is not JSON for {method}"
            ) from None
        if not isinstance(body, dict):
            raise SlackUnreachableError(f"Slack sent an unexpected response for {method}")
        scopes = None if header is None else parse_scopes(header)
        return ApiResult(body, scopes)


def parse_scopes(header: str) -> frozenset[str]:
    """Parse a comma-separated ``X-OAuth-Scopes`` header."""
    return frozenset(part.strip() for part in header.split(",") if part.strip())


class TokenError(Exception):
    """A pasted token cannot be used. The message tells the person what to do."""


@dataclass(frozen=True)
class BotIdentity:
    """Who a working bot token belongs to, for the confirmation message."""

    team: str
    team_id: str
    bot_user: str
    bot_user_id: str
    scopes_verified: bool


_REINSTALL = (
    "Reinstall the app from OAuth & Permissions so the new scopes are granted, "
    "then copy the bot token again."
)

_COMMON_ERRORS = {
    "invalid_auth": "Slack does not recognise this token. Copy it again; it may be incomplete.",
    "not_authed": "No token reached Slack. Paste the token again.",
    "account_inactive": "This token belongs to an app or user that has been deactivated.",
    "token_revoked": "This token has been revoked. Reinstall the app and copy the new token.",
    "token_expired": "This token has expired. Turn off token rotation for the app, "
    "reinstall it, and copy the new token.",
    "ratelimited": "Slack is rate limiting requests. Wait a minute and try again.",
}


def _prefix_problem(token: str, expected: str, kind: str, where: str) -> str | None:
    if not token:
        return f"The {kind} is empty."
    if any(ch.isspace() for ch in token):
        return f"The {kind} contains spaces or line breaks. Paste it again as one line."
    if token.startswith(expected):
        return None
    if token.startswith("xapp-"):
        return f"That is an app-level token (xapp-). Paste the {kind} ({expected}) from {where}."
    if token.startswith("xoxb-"):
        return f"That is a bot token (xoxb-). Paste the {kind} ({expected}) from {where}."
    if token.startswith("xoxp-"):
        return f"That is a user token (xoxp-). Paste the {kind} ({expected}) from {where}."
    return f"A {kind} starts with {expected}. Copy it from {where}."


def _api_problem(result: ApiResult) -> str:
    error = result.error
    return _COMMON_ERRORS.get(error, f"Slack rejected the token ({error}).")


def check_bot_token(api: SlackApi, token: str) -> BotIdentity:
    """Validate a bot token with ``auth.test`` and check it has every bot scope."""
    where = "OAuth & Permissions"
    if problem := _prefix_problem(token, "xoxb-", "bot token", where):
        raise TokenError(problem)
    result = api.call("auth.test", token)
    if not result.ok:
        raise TokenError(_api_problem(result))
    if not result.body.get("bot_id"):
        raise TokenError(
            f"This token does not belong to a bot user. Copy the bot token from {where}."
        )
    if result.scopes is not None:
        missing = [scope for scope in BOT_SCOPES if scope not in result.scopes]
        if missing:
            one = len(missing) == 1
            raise TokenError(
                f"The bot token is missing {'this scope' if one else 'these scopes'}: "
                f"{', '.join(missing)}. In the app's settings, open OAuth & Permissions, "
                f"and under Scopes, Bot Token Scopes, add {'it' if one else 'them'}. "
                f"Do not create a second app. {_REINSTALL}"
            )
    return BotIdentity(
        team=str(result.body.get("team") or ""),
        team_id=str(result.body.get("team_id") or ""),
        bot_user=str(result.body.get("user") or ""),
        bot_user_id=str(result.body.get("user_id") or ""),
        scopes_verified=result.scopes is not None,
    )


def check_app_token(api: SlackApi, token: str) -> None:
    """Validate an app-level token with ``apps.connections.open``."""
    where = "Basic Information, under App-Level Tokens"
    if problem := _prefix_problem(token, "xapp-", "app-level token", where):
        raise TokenError(problem)
    result = api.call("apps.connections.open", token)
    if result.ok:
        return
    if result.error == "missing_scope":
        raise TokenError(
            f"The app-level token is missing the {APP_TOKEN_SCOPE} scope. Generate a new "
            f"app-level token under {where} and add {APP_TOKEN_SCOPE} to it."
        )
    if result.error == "not_allowed_token_type":
        raise TokenError(f"This is not an app-level token. Generate one under {where}.")
    raise TokenError(_api_problem(result))
