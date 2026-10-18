"""Per-run bearer tokens: grants, issue, check, expiry, revoke, and secrecy."""

from __future__ import annotations

import pytest

from jotflow.models import RunKind
from jotflow.whiteboard.tokens import (
    MAX_TOKEN_LENGTH,
    TOKEN_PREFIX,
    Grant,
    RegistryFullError,
    TokenCheck,
    TokenRegistry,
)

DRAFT = Grant(run_id="run-1", kind=RunKind.DRAFTING, channel_id="C0EXAMPLE1", thread_ts="1.000100")
EXECUTE = Grant(
    run_id="run-2", kind=RunKind.EXECUTION, channel_id="C0EXAMPLE1", proposal_id="prop-1"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def registry(clock: FakeClock) -> TokenRegistry:
    return TokenRegistry(clock=clock)


def test_an_execution_grant_must_name_its_proposal() -> None:
    with pytest.raises(ValueError, match="must name its proposal"):
        Grant(run_id="r", kind=RunKind.EXECUTION, channel_id="C0EXAMPLE1")


@pytest.mark.parametrize("kind", [RunKind.DRAFTING, RunKind.QUESTION])
def test_only_execution_grants_name_a_proposal(kind: RunKind) -> None:
    with pytest.raises(ValueError, match="only an execution grant"):
        Grant(run_id="r", kind=kind, channel_id="C0EXAMPLE1", thread_ts="1.0", proposal_id="p")


@pytest.mark.parametrize(("run_id", "channel_id"), [("", "C0EXAMPLE1"), ("r", "")])
def test_a_grant_needs_a_run_and_a_channel(run_id: str, channel_id: str) -> None:
    with pytest.raises(ValueError, match="needs a run id and a channel id"):
        Grant(run_id=run_id, kind=RunKind.DRAFTING, channel_id=channel_id, thread_ts="1.000100")


@pytest.mark.parametrize("kind", [RunKind.DRAFTING, RunKind.QUESTION])
@pytest.mark.parametrize("thread_ts", [None, ""])
def test_drafting_and_question_grants_must_name_their_thread(
    kind: RunKind, thread_ts: str | None
) -> None:
    # A top-level post's run uses the post's own ts; None is never a thread.
    with pytest.raises(ValueError, match=f"a {kind.value} grant must name its thread"):
        Grant(run_id="r", kind=kind, channel_id="C0EXAMPLE1", thread_ts=thread_ts)


def test_an_execution_grant_needs_no_thread() -> None:
    assert EXECUTE.thread_ts is None


def test_the_registry_is_a_token_check(registry: TokenRegistry) -> None:
    check: TokenCheck = registry
    assert check.check(registry.issue(DRAFT, 60)) == DRAFT


def test_tokens_are_prefixed_random_and_resolve_to_their_own_run(
    registry: TokenRegistry,
) -> None:
    first = registry.issue(DRAFT, 60)
    second = registry.issue(EXECUTE, 60)
    assert first.startswith(TOKEN_PREFIX)
    assert len(first) < MAX_TOKEN_LENGTH
    assert first != second
    assert registry.check(first) == DRAFT
    assert registry.check(second) == EXECUTE


@pytest.mark.parametrize(
    "token",
    ["", "jfr_", "jfr_not-issued", "not-prefixed", TOKEN_PREFIX + "x" * MAX_TOKEN_LENGTH],
)
def test_unknown_or_malformed_tokens_are_refused(registry: TokenRegistry, token: str) -> None:
    registry.issue(DRAFT, 60)
    assert registry.check(token) is None


def test_a_token_expires_after_its_ttl(registry: TokenRegistry, clock: FakeClock) -> None:
    token = registry.issue(DRAFT, 60)
    clock.now += 59.9
    assert registry.check(token) == DRAFT
    clock.now += 0.1
    assert registry.check(token) is None
    assert registry.active() == 0


def test_revoking_ends_the_run_token(registry: TokenRegistry) -> None:
    token = registry.issue(DRAFT, 60)
    other = registry.issue(EXECUTE, 60)
    assert registry.revoke("run-1") is True
    assert registry.check(token) is None
    assert registry.check(other) == EXECUTE
    assert registry.revoke("run-1") is False


def test_reissuing_for_a_run_revokes_its_old_token(registry: TokenRegistry) -> None:
    old = registry.issue(DRAFT, 60)
    new = registry.issue(DRAFT, 60)
    assert registry.check(old) is None
    assert registry.check(new) == DRAFT
    assert registry.active() == 1


@pytest.mark.parametrize("ttl", [0, -1, 2 * 60 * 60 + 1])
def test_ttl_must_be_positive_and_capped(registry: TokenRegistry, ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl must be more than 0"):
        registry.issue(DRAFT, ttl)


def test_live_tokens_are_bounded_and_expired_ones_free_space(clock: FakeClock) -> None:
    registry = TokenRegistry(clock=clock, max_live=2)
    registry.issue(Grant("a", RunKind.DRAFTING, "C0EXAMPLE1", "1.000100"), 10)
    registry.issue(Grant("b", RunKind.DRAFTING, "C0EXAMPLE1", "1.000100"), 100)
    with pytest.raises(RegistryFullError):
        registry.issue(Grant("c", RunKind.DRAFTING, "C0EXAMPLE1", "1.000100"), 100)
    clock.now += 10
    registry.issue(Grant("c", RunKind.DRAFTING, "C0EXAMPLE1", "1.000100"), 100)
    assert registry.active() == 2


def test_an_old_token_stays_dead_after_a_reissue(registry: TokenRegistry, clock: FakeClock) -> None:
    old = registry.issue(DRAFT, 10)
    clock.now += 5
    new = registry.issue(DRAFT, 60)
    clock.now += 10
    assert registry.check(old) is None
    assert registry.check(new) == DRAFT


def test_the_registry_never_shows_a_token(registry: TokenRegistry) -> None:
    token = registry.issue(DRAFT, 60)
    assert repr(registry) == "TokenRegistry(live=1)"
    assert token not in repr(vars(registry))
    assert token[len(TOKEN_PREFIX) :] not in repr(vars(registry))
