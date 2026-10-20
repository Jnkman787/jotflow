"""What ``jotflow init`` writes for the agent containers (#126, D6).

Each runner has its own secret. The drafting runner's lives in ``agent.env``,
and in ``.env`` as ``JOTFLOW_RUNNER_SECRET``. The execution runner's lives in
``agent-exec.env`` under the same name, and in ``.env`` as
``JOTFLOW_RUNNER_EXEC_SECRET``. The two must differ: a drafting run can read
its own runner's secret, so it must not be able to start an execution run.

This module only decides values. The command writes them, and never prints one.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

ENV_DRAFTING_KEY = "JOTFLOW_RUNNER_SECRET"
ENV_EXECUTION_KEY = "JOTFLOW_RUNNER_EXEC_SECRET"
# Each agent env file names its own runner's secret the same way.
AGENT_SECRET_KEY = "JOTFLOW_RUNNER_SECRET"
PRESET_KEY = "JOTFLOW_AGENT_PRESET"
MODEL_KEY = "JOTFLOW_AGENT_MODEL"
COMMAND_KEY = "JOTFLOW_AGENT_COMMAND"
PROFILES_KEY = "COMPOSE_PROFILES"
AGENT_PROFILE = "agent"
READ_TOKEN_KEY = "JOTFLOW_GITHUB_READ_TOKEN"
WRITE_TOKEN_KEY = "JOTFLOW_GITHUB_WRITE_TOKEN"
# The variables the runner passes to the agent, by name, comma-separated.
ENV_ALLOW_KEY = "JOTFLOW_AGENT_ENV_ALLOW"
ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
# Codex reads either; setup writes the first, which names its one use.
CODEX_KEYS = ("CODEX_API_KEY", "OPENAI_API_KEY")
# The model login each preset can read from its env file. claude-code can log
# in inside its container instead, so a missing key is fine; a set one must pass.
LOGIN_KEYS: Mapping[str, tuple[str, ...]] = {
    "claude-code": (ANTHROPIC_KEY,),
    "codex": CODEX_KEYS,
}

# The runner refuses a shorter secret.
MIN_SECRET_LENGTH = 32

How = Literal["kept", "copied", "regenerated", "generated", "rotated"]

# Tries to make the execution secret differ from the drafting one.
MAX_DISTINCT_TRIES = 5


def new_secret() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Secret:
    """A runner secret to write to both its files, and how it was chosen.

    The value never appears in ``repr``, so a log line or a failed test
    cannot show it.
    """

    value: str
    how: How

    def __repr__(self) -> str:
        return f"Secret(how={self.how!r})"


def _usable(value: str | None) -> str | None:
    return value if value and len(value) >= MIN_SECRET_LENGTH else None


def choose_secret(
    in_env: str | None,
    in_agent: str | None,
    *,
    rotate: bool = False,
    generate: Callable[[], str] = new_secret,
) -> Secret:
    """Keep the pair's secret, or make a new one.

    A mismatch gets a fresh secret for both files, never either side's value:
    one side may hold a rotation made by hand after a leak, or the half of an
    interrupted rotation that was written. A value too short for the runner is
    replaced.
    """
    if rotate:
        return Secret(generate(), "rotated")
    env, agent = _usable(in_env), _usable(in_agent)
    if env and agent:
        return Secret(env, "kept") if env == agent else Secret(generate(), "regenerated")
    if env or agent:
        return Secret(env or agent or "", "copied")
    return Secret(generate(), "generated")


def choose_secrets(
    env: Mapping[str, str],
    agent: Mapping[str, str],
    agent_exec: Mapping[str, str],
    *,
    rotate_drafting: bool = False,
    rotate_execution: bool = False,
    generate: Callable[[], str] = new_secret,
) -> tuple[Secret, Secret]:
    """The drafting and execution secrets. They always differ."""
    drafting = choose_secret(
        env.get(ENV_DRAFTING_KEY),
        agent.get(AGENT_SECRET_KEY),
        rotate=rotate_drafting,
        generate=generate,
    )
    execution = choose_secret(
        env.get(ENV_EXECUTION_KEY),
        agent_exec.get(AGENT_SECRET_KEY),
        rotate=rotate_execution,
        generate=generate,
    )
    for _ in range(MAX_DISTINCT_TRIES):
        if execution.value != drafting.value:
            return drafting, execution
        # Only a hand edit makes them equal; JotFlow refuses to start that way.
        execution = Secret(generate(), "generated")
    raise RuntimeError("could not make two different runner secrets")


def with_agent_profile(current: str | None) -> str | None:
    """``COMPOSE_PROFILES`` with ``agent`` added, or None if it is already there."""
    profiles = [p.strip() for p in (current or "").split(",") if p.strip()]
    if AGENT_PROFILE in profiles:
        return None
    return ",".join([*profiles, AGENT_PROFILE])


def allowed(agent_env: Mapping[str, str]) -> list[str]:
    """The names an agent env file passes to the agent, in order."""
    raw = agent_env.get(ENV_ALLOW_KEY, "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def unpassed_logins(agent_env: Mapping[str, str], preset: str) -> list[str]:
    """Login keys the file sets for ``preset`` but doesn't pass, so no run can sign in (#239).

    Empty when a set key is already passed: codex needs only one of its two.
    """
    names = allowed(agent_env)
    filled = [k for k in LOGIN_KEYS.get(preset, ()) if agent_env.get(k, "").strip()]
    if any(k in names for k in filled):
        return []
    return filled


def uses_command(agent_env: Mapping[str, str]) -> bool:
    """The file sets the raw-command escape hatch, so a preset would be an error."""
    return bool(agent_env.get(COMMAND_KEY, "").strip())
