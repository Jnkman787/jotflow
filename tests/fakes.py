"""Test doubles shared by the setup tests. No network."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jotflow.slack_auth import ApiResult, SlackUnreachableError
from jotflow.slack_manifest import BOT_SCOPES

GOOD_BOT = "xoxb-good-bot-token"
GOOD_APP = "xapp-good-app-token"


def auth_ok(scopes: Iterable[str] | None = BOT_SCOPES) -> ApiResult:
    body: Mapping[str, Any] = {
        "ok": True,
        "team": "Example Team",
        "team_id": "T0EXAMPLE",
        "user": "jotflow",
        "user_id": "U0EXAMPLE",
        "bot_id": "B0EXAMPLE",
    }
    return ApiResult(body, None if scopes is None else frozenset(scopes))


def error(code: str, **extra: Any) -> ApiResult:
    return ApiResult({"ok": False, "error": code, **extra})


@dataclass
class FakeSlackApi:
    """Answers ``(method, token)`` from a table; unknown pairs are ``invalid_auth``."""

    responses: dict[tuple[str, str], ApiResult | Exception] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def working(cls) -> FakeSlackApi:
        return cls(
            {
                ("auth.test", GOOD_BOT): auth_ok(),
                ("apps.connections.open", GOOD_APP): ApiResult(
                    {"ok": True, "url": "wss://example.invalid/"}
                ),
            }
        )

    def call(self, method: str, token: str) -> ApiResult:
        self.calls.append((method, token))
        answer = self.responses.get((method, token), error("invalid_auth"))
        if isinstance(answer, Exception):
            raise answer
        return answer


def unreachable() -> SlackUnreachableError:
    return SlackUnreachableError("could not reach Slack: offline")
