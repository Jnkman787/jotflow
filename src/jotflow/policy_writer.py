"""Write a :class:`~jotflow.policy.Policy` back out as ``jotflow.toml``.

The standard library reads TOML but cannot write it, and JotFlow adds no
dependency for this, so the few shapes the policy uses are written here.
:func:`render_policy` checks its own output with :func:`~jotflow.policy.parse_policy`,
so a file it returns always loads back to the same policy.
"""

from __future__ import annotations

from collections.abc import Iterable

from jotflow.policy import Policy, parse_policy

HEADER = (
    "# jotflow.toml: admin policy for JotFlow, written by `jotflow init`.\n"
    "# Edit freely; jotflow.example.toml explains every option.\n"
    "# Secrets never go here: the Slack tokens live in .env.\n"
)


def toml_string(value: str) -> str:
    """Quote ``value`` as a TOML basic string."""
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _array(values: Iterable[str]) -> str:
    return "[" + ", ".join(toml_string(v) for v in values) + "]"


def _bool(value: bool) -> str:
    return "true" if value else "false"


def render_policy(policy: Policy) -> str:
    """Return TOML text that loads back to exactly ``policy``."""
    lines = [
        HEADER,
        f"issue_links = {toml_string(policy.issue_links)}",
        f"debounce_seconds = {policy.debounce_seconds}",
        f"topic_window_days = {policy.topic_window_days}",
        "",
        "[agent]",
    ]
    if policy.agent.preset is not None:
        lines.append(f"preset = {toml_string(policy.agent.preset)}")
    else:
        lines.append('# preset = "claude-code"  # unset: JotFlow connects but starts no agent runs')
    lines.append(f"max_concurrent_runs = {policy.agent.max_concurrent_runs}")
    lines += [
        "",
        "[approval]",
        f"approvers = {_array(policy.approval.approvers)}",
        f"reactions = {_bool(policy.approval.reactions)}",
        f"reaction = {toml_string(policy.approval.reaction)}",
        "",
        "[help]",
        f"guide = {_bool(policy.help.guide)}",
        f"newcomer_tip = {_bool(policy.help.newcomer_tip)}",
        f"near_miss_nudge = {_bool(policy.help.near_miss_nudge)}",
    ]
    for channel_id, channel in policy.channels.items():
        # Channel ids are validated to [CG][A-Z0-9]+, so they are bare keys.
        lines += ["", f"[channels.{channel_id}]"]
        if channel.debounce_seconds is not None:
            lines.append(f"debounce_seconds = {channel.debounce_seconds}")
        for repo in channel.repos:
            lines += [
                "",
                f"[[channels.{channel_id}.repos]]",
                f"repo = {toml_string(repo.repo)}",
                f"nickname = {toml_string(repo.nickname)}",
                f"description = {toml_string(repo.description)}",
            ]
            # No labels line means any label that exists on the repo.
            if repo.labels is not None:
                lines.append(f"labels = {_array(repo.labels)}")
        own_help = channel.help.model_dump(exclude_none=True)
        if own_help:
            lines += ["", f"[channels.{channel_id}.help]"]
            lines += [f"{key} = {_bool(value)}" for key, value in own_help.items()]
    text = "\n".join(lines) + "\n"
    if parse_policy(text) != policy:
        raise RuntimeError("rendered jotflow.toml does not load back to the same policy")
    return text
