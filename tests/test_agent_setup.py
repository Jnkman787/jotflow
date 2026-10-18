from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from jotflow.agent_setup import (
    MIN_SECRET_LENGTH,
    Secret,
    choose_secret,
    choose_secrets,
    new_secret,
    uses_command,
    with_agent_profile,
)

OLD = "o" * MIN_SECRET_LENGTH
OTHER = "x" * MIN_SECRET_LENGTH


def counter() -> Callable[[], str]:
    numbers: Iterator[int] = iter(range(1, 1000))
    return lambda: f"new-{next(numbers):0{MIN_SECRET_LENGTH}d}"


def test_new_secrets_are_long_enough_and_different() -> None:
    first, second = new_secret(), new_secret()
    assert len(first) >= MIN_SECRET_LENGTH and first != second


@pytest.mark.parametrize(
    ("in_env", "in_agent", "value", "how"),
    [
        (OLD, OLD, OLD, "kept"),
        (OLD, OTHER, "fresh", "regenerated"),
        (OLD, None, OLD, "copied"),
        (None, OLD, OLD, "copied"),
        ("short", OLD, OLD, "copied"),
        (None, None, "fresh", "generated"),
        ("", "short", "fresh", "generated"),
    ],
)
def test_choosing_a_pairs_secret(
    in_env: str | None, in_agent: str | None, value: str, how: str
) -> None:
    assert choose_secret(in_env, in_agent, generate=lambda: "fresh") == Secret(value, how)  # type: ignore[arg-type]


def test_rotation_replaces_a_good_secret() -> None:
    assert choose_secret(OLD, OLD, rotate=True, generate=lambda: "fresh") == Secret(
        "fresh", "rotated"
    )


def test_a_first_run_makes_two_different_secrets() -> None:
    drafting, execution = choose_secrets({}, {}, {})
    assert drafting.how == execution.how == "generated"
    assert drafting.value != execution.value


def test_a_rerun_keeps_both() -> None:
    env = {"JOTFLOW_RUNNER_SECRET": OLD, "JOTFLOW_RUNNER_EXEC_SECRET": OTHER}
    drafting, execution = choose_secrets(
        env, {"JOTFLOW_RUNNER_SECRET": OLD}, {"JOTFLOW_RUNNER_SECRET": OTHER}
    )
    assert (drafting, execution) == (Secret(OLD, "kept"), Secret(OTHER, "kept"))


@pytest.mark.parametrize(("drafting_rotates", "execution_rotates"), [(True, False), (False, True)])
def test_either_secret_rotates_alone(drafting_rotates: bool, execution_rotates: bool) -> None:
    env = {"JOTFLOW_RUNNER_SECRET": OLD, "JOTFLOW_RUNNER_EXEC_SECRET": OTHER}
    drafting, execution = choose_secrets(
        env,
        {"JOTFLOW_RUNNER_SECRET": OLD},
        {"JOTFLOW_RUNNER_SECRET": OTHER},
        rotate_drafting=drafting_rotates,
        rotate_execution=execution_rotates,
        generate=counter(),
    )
    assert (drafting.how == "rotated") is drafting_rotates
    assert (execution.how == "rotated") is execution_rotates
    assert (drafting.value == OLD) is not drafting_rotates
    assert (execution.value == OTHER) is not execution_rotates


def test_equal_secrets_from_a_hand_edit_are_split() -> None:
    env = {"JOTFLOW_RUNNER_SECRET": OLD, "JOTFLOW_RUNNER_EXEC_SECRET": OLD}
    drafting, execution = choose_secrets(env, {}, {}, generate=counter())
    assert drafting == Secret(OLD, "copied")
    assert execution.how == "generated" and execution.value != OLD


def test_a_secret_never_shows_in_its_repr() -> None:
    assert OLD not in repr(Secret(OLD, "kept"))


@pytest.mark.parametrize(
    ("current", "result"),
    [
        (None, "agent"),
        ("", "agent"),
        ("debug", "debug,agent"),
        (" debug , tools ", "debug,tools,agent"),
        ("debug,agent", None),
    ],
)
def test_the_agent_profile_joins_any_others(current: str | None, result: str | None) -> None:
    assert with_agent_profile(current) == result


def test_the_escape_hatch_is_a_non_empty_command() -> None:
    assert uses_command({"JOTFLOW_AGENT_COMMAND": '["my-agent"]'})
    assert not uses_command({"JOTFLOW_AGENT_COMMAND": "  "})
    assert not uses_command({})


def test_a_generator_that_repeats_itself_is_refused() -> None:
    env = {"JOTFLOW_RUNNER_SECRET": OLD, "JOTFLOW_RUNNER_EXEC_SECRET": OLD}
    with pytest.raises(RuntimeError, match="two different runner secrets"):
        choose_secrets(env, {}, {}, generate=lambda: OLD)
