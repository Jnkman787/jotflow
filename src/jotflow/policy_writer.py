"""Write a :class:`~jotflow.policy.Policy` back out as ``jotflow.toml``.

The standard library reads TOML but cannot write it, and JotFlow adds no
dependency for this, so the few shapes the policy uses are written here.
:func:`render_policy` checks its own output with :func:`~jotflow.policy.parse_policy`,
so a file it returns always loads back to the same policy.
"""

from __future__ import annotations

from collections.abc import Iterable

from jotflow.policy import IssuesPolicy, Policy, QuestionsPolicy, parse_policy

HEADER = (
    "# jotflow.toml: admin policy for JotFlow, written by `jotflow init`.\n"
    "# Edit freely; examples/jotflow.example.toml in JotFlow's repo explains every option.\n"
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
        f"issue_reaction = {toml_string(policy.issue_reaction)}",
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
        f"require_second_approver = {_bool(policy.approval.require_second_approver)}",
        f"reactions = {_bool(policy.approval.reactions)}",
        f"reaction = {toml_string(policy.approval.reaction)}",
        "",
        "[help]",
        f"guide = {_bool(policy.help.guide)}",
        f"newcomer_tip = {_bool(policy.help.newcomer_tip)}",
        f"near_miss_nudge = {_bool(policy.help.near_miss_nudge)}",
        "",
        "[identity]",
        f"admins = {_array(policy.identity.admins)}",
    ]
    # Questions (#57): written only when a team changed them, so a default setup is unchanged.
    if policy.questions != QuestionsPolicy():
        lines += [
            "",
            "[questions]",
            f"enabled = {_bool(policy.questions.enabled)}",
            f"per_hour = {policy.questions.per_hour}",
            f"per_person_seconds = {policy.questions.per_person_seconds}",
        ]
    # What every new issue gets (#306): written only when set, like [questions].
    if policy.issues != IssuesPolicy():
        lines += ["", "[issues]"]
        if policy.issues.label is not None:
            lines.append(f"label = {toml_string(policy.issues.label)}")
        if policy.issues.title_prefix is not None:
            lines.append(f"title_prefix = {toml_string(policy.issues.title_prefix)}")
    for channel_id, channel in policy.channels.items():
        # Channel ids are validated to [CG][A-Z0-9]+, so they are bare keys.
        lines += ["", f"[channels.{channel_id}]"]
        if channel.debounce_seconds is not None:
            lines.append(f"debounce_seconds = {channel.debounce_seconds}")
        if channel.triage_guide is not None:
            # The team's guide to choosing a repo and boards (#317), as written.
            lines.append(f"triage_guide = {toml_string(channel.triage_guide)}")
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
            if repo.visibility is not None:
                lines.append(f"visibility = {toml_string(repo.visibility)}")
            # The team's own issue rules (#216): kept on a rewrite, newlines and all.
            if repo.title_pattern is not None:
                lines.append(f"title_pattern = {toml_string(repo.title_pattern)}")
            if repo.conventions is not None:
                lines.append(f"conventions = {toml_string(repo.conventions)}")
            # Docs a drafting run may read (#209).
            if repo.read_paths is not None:
                lines.append(f"read_paths = {_array(repo.read_paths)}")
        for scope in channel.scopes:
            # Pattern scopes (#236): every field, so a rewrite keeps what was written.
            lines += [
                "",
                f"[[channels.{channel_id}.scopes]]",
                f"owner = {toml_string(scope.owner)}",
                f"match = {_array(scope.match)}",
                f"exclude = {_array(scope.exclude)}",
                f"include_forks = {_bool(scope.include_forks)}",
                f"include_archived = {_bool(scope.include_archived)}",
            ]
            if scope.labels is not None:
                lines.append(f"labels = {_array(scope.labels)}")
            if scope.description is not None:
                lines.append(f"description = {toml_string(scope.description)}")
            if scope.aliases:
                # Nicknames are [a-z0-9_-], so they are bare keys.
                pairs = ", ".join(f"{k} = {toml_string(v)}" for k, v in scope.aliases.items())
                lines.append(f"aliases = {{ {pairs} }}")
        for board in channel.boards:
            # Projects boards (#309), as configured: the description is the team's.
            lines += [
                "",
                f"[[channels.{channel_id}.boards]]",
                f"board = {toml_string(board.board)}",
                f"nickname = {toml_string(board.nickname)}",
                f"description = {toml_string(board.description)}",
            ]
        own_help = channel.help.model_dump(exclude_none=True)
        if own_help:
            lines += ["", f"[channels.{channel_id}.help]"]
            lines += [f"{key} = {_bool(value)}" for key, value in own_help.items()]
        own_questions = channel.questions.model_dump(exclude_none=True)
        if own_questions:
            lines += ["", f"[channels.{channel_id}.questions]"]
            lines += [
                f"{key} = {_bool(value) if isinstance(value, bool) else value}"
                for key, value in own_questions.items()
            ]
    text = "\n".join(lines) + "\n"
    if parse_policy(text) != policy:
        raise RuntimeError("rendered jotflow.toml does not load back to the same policy")
    return text
