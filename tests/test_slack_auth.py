from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from typing import Any

import pytest

from jotflow import slack_auth
from jotflow.slack_auth import (
    ApiResult,
    HttpSlackApi,
    SlackUnreachableError,
    TokenError,
    check_app_token,
    check_bot_token,
    parse_scopes,
)
from jotflow.slack_manifest import BOT_SCOPES
from tests.fakes import GOOD_APP, GOOD_BOT, FakeSlackApi, auth_ok, error


def test_working_bot_token() -> None:
    identity = check_bot_token(FakeSlackApi.working(), GOOD_BOT)
    assert identity.team == "Example Team"
    assert identity.bot_user == "jotflow"
    assert identity.scopes_verified


def test_missing_scopes_are_named() -> None:
    granted = [s for s in BOT_SCOPES if s not in {"pins:write", "groups:read"}]
    api = FakeSlackApi({("auth.test", GOOD_BOT): auth_ok(granted)})
    with pytest.raises(TokenError) as exc:
        check_bot_token(api, GOOD_BOT)
    message = str(exc.value)
    assert "groups:read" in message
    assert "pins:write" in message
    assert "channels:history" not in message
    assert "Reinstall" in message


def test_one_missing_scope_reads_naturally() -> None:
    granted = [s for s in BOT_SCOPES if s != "pins:write"]
    api = FakeSlackApi({("auth.test", GOOD_BOT): auth_ok(granted)})
    with pytest.raises(TokenError, match="missing this scope: pins:write"):
        check_bot_token(api, GOOD_BOT)


def test_extra_scopes_are_fine() -> None:
    api = FakeSlackApi({("auth.test", GOOD_BOT): auth_ok([*BOT_SCOPES, "files:read"])})
    assert check_bot_token(api, GOOD_BOT).scopes_verified


def test_unreported_scopes_are_accepted_but_flagged() -> None:
    api = FakeSlackApi({("auth.test", GOOD_BOT): auth_ok(None)})
    assert check_bot_token(api, GOOD_BOT).scopes_verified is False


@pytest.mark.parametrize(
    ("token", "fragment"),
    [
        ("", "empty"),
        ("xapp-123", "app-level token (xapp-)"),
        ("xoxp-123", "user token (xoxp-)"),
        ("hello", "starts with xoxb-"),
        ("xoxb-12 34", "spaces"),
    ],
)
def test_bot_token_shape_is_checked_before_calling_slack(token: str, fragment: str) -> None:
    api = FakeSlackApi.working()
    with pytest.raises(TokenError, match=fragment.replace("(", r"\(").replace(")", r"\)")):
        check_bot_token(api, token)
    assert api.calls == []


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("invalid_auth", "does not recognise"),
        ("token_revoked", "revoked"),
        ("account_inactive", "deactivated"),
        ("ratelimited", "rate limiting"),
        ("something_new", "something_new"),
    ],
)
def test_bot_token_slack_errors(code: str, fragment: str) -> None:
    api = FakeSlackApi({("auth.test", GOOD_BOT): error(code)})
    with pytest.raises(TokenError, match=fragment):
        check_bot_token(api, GOOD_BOT)


def test_non_bot_token_is_rejected() -> None:
    result = auth_ok()
    body = {k: v for k, v in result.body.items() if k != "bot_id"}
    api = FakeSlackApi({("auth.test", GOOD_BOT): ApiResult(body, result.scopes)})
    with pytest.raises(TokenError, match="bot user"):
        check_bot_token(api, GOOD_BOT)


def test_token_never_appears_in_messages() -> None:
    secret = "xoxb-super-secret-value"
    api = FakeSlackApi({("auth.test", secret): auth_ok(["chat:write"])})
    with pytest.raises(TokenError) as exc:
        check_bot_token(api, secret)
    assert secret not in str(exc.value)


def test_working_app_token() -> None:
    check_app_token(FakeSlackApi.working(), GOOD_APP)


def test_app_token_missing_scope_names_connections_write() -> None:
    api = FakeSlackApi(
        {("apps.connections.open", GOOD_APP): error("missing_scope", needed="connections:write")}
    )
    with pytest.raises(TokenError, match="connections:write"):
        check_app_token(api, GOOD_APP)


def test_bot_token_pasted_as_app_token() -> None:
    api = FakeSlackApi.working()
    with pytest.raises(TokenError, match=r"bot token \(xoxb-\)"):
        check_app_token(api, GOOD_BOT)
    assert api.calls == []


def test_app_token_wrong_type() -> None:
    api = FakeSlackApi({("apps.connections.open", GOOD_APP): error("not_allowed_token_type")})
    with pytest.raises(TokenError, match="not an app-level token"):
        check_app_token(api, GOOD_APP)


def test_parse_scopes() -> None:
    assert parse_scopes("chat:write, commands,,users:read ") == {
        "chat:write",
        "commands",
        "users:read",
    }


class _FakeResponse:
    def __init__(self, body: Any, scopes: str | None) -> None:
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = Message()
        if scopes is not None:
            self.headers["X-OAuth-Scopes"] = scopes

    def read(self, limit: int = -1) -> bytes:
        return self._raw if limit < 0 else self._raw[:limit]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeOpener:
    def __init__(self, answer: _FakeResponse | Exception) -> None:
        self.answer = answer
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float) -> _FakeResponse:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _api(answer: _FakeResponse | Exception) -> tuple[HttpSlackApi, _FakeOpener]:
    opener = _FakeOpener(answer)
    return HttpSlackApi(opener=opener), opener


def test_http_client_sends_unredirected_bearer_token_and_reads_scopes() -> None:
    api, opener = _api(_FakeResponse({"ok": True, "bot_id": "B1"}, "chat:write,commands"))
    result = api.call("auth.test", "xoxb-1")
    (request,) = opener.requests
    assert request.full_url == "https://slack.com/api/auth.test"
    assert request.get_method() == "POST"
    assert request.unredirected_hdrs == {"Authorization": "Bearer xoxb-1"}
    assert "Authorization" not in request.headers
    assert result.ok
    assert result.scopes == {"chat:write", "commands"}


def test_default_opener_refuses_redirects() -> None:
    handler = slack_auth._RefuseRedirects()
    request = urllib.request.Request("https://slack.com/api/auth.test")
    request.add_unredirected_header("Authorization", "Bearer xoxb-1")
    assert (
        handler.redirect_request(
            request, io.BytesIO(), 302, "Found", Message(), "https://elsewhere.example/"
        )
        is None
    )


def test_a_real_redirect_is_not_followed() -> None:
    """Drive urllib's own redirect handling, with no network, through a stub handler."""

    seen: list[str] = []

    class RedirectEverything(urllib.request.BaseHandler):
        handler_order = 100  # before the real HTTPSHandler, so nothing leaves the machine

        def https_open(self, req: urllib.request.Request) -> Any:
            seen.append(req.full_url)
            headers = Message()
            headers["Location"] = "https://elsewhere.example/steal"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            response.msg = "Found"
            return response

    opener = urllib.request.build_opener(slack_auth._RefuseRedirects(), RedirectEverything())
    api = HttpSlackApi(opener=opener)
    with pytest.raises(SlackUnreachableError, match="redirect"):
        api.call("auth.test", "xoxb-1")
    assert seen == ["https://slack.com/api/auth.test"]


def test_plain_http_base_url_is_refused() -> None:
    with pytest.raises(ValueError, match="https"):
        HttpSlackApi(base_url="http://slack.com/api/")


def test_http_client_without_scope_header() -> None:
    api, _ = _api(_FakeResponse({"ok": True}, None))
    assert api.call("apps.connections.open", "xapp-1").scopes is None


def test_http_client_rate_limit() -> None:
    error_429 = urllib.error.HTTPError(
        "https://slack.com/api/auth.test", 429, "Too Many", Message(), io.BytesIO()
    )
    api, _ = _api(error_429)
    assert api.call("auth.test", "xoxb-1").error == "ratelimited"


@pytest.mark.parametrize("failure", [urllib.error.URLError("no route"), TimeoutError("slow")])
def test_http_client_unreachable(failure: Exception) -> None:
    api, _ = _api(failure)
    with pytest.raises(SlackUnreachableError):
        api.call("auth.test", "xoxb-1")


@pytest.mark.parametrize("raw", [b"<html>", b"[1, 2]"])
def test_http_client_rejects_non_object_json(raw: bytes) -> None:
    api, _ = _api(_FakeResponse(raw, None))
    with pytest.raises(SlackUnreachableError):
        api.call("auth.test", "xoxb-1")


def test_http_client_bounds_the_response() -> None:
    big = b'{"ok": true, "pad": "' + b"x" * slack_auth.MAX_RESPONSE_BYTES + b'"}'
    api, _ = _api(_FakeResponse(big, None))
    with pytest.raises(SlackUnreachableError, match="large"):
        api.call("auth.test", "xoxb-1")


def test_missing_scope_advice_never_suggests_a_second_app() -> None:
    granted = [s for s in BOT_SCOPES if s != "pins:write"]
    api = FakeSlackApi({("auth.test", GOOD_BOT): auth_ok(granted)})
    with pytest.raises(TokenError) as exc:
        check_bot_token(api, GOOD_BOT)
    message = str(exc.value)
    assert "recreate" not in message
    assert "Do not create a second app" in message
    assert "Bot Token Scopes" in message
