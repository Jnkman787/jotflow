from pathlib import Path

import pytest

from jotflow.policy import (
    InvalidLabelError,
    LabelNotAllowedError,
    Policy,
    PolicyError,
    RepoNotAllowedError,
    UnknownChannelError,
    load_policy,
    parse_policy,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "jotflow.example.toml"

MINIMAL = """
[channels.C0TEST1]

[[channels.C0TEST1.repos]]
repo = "acme/web"
nickname = "web"
description = "The web app"
labels = ["bug", "Enhancement"]

[[channels.C0TEST1.repos]]
repo = "acme/api"
nickname = "api"
description = "The API"
"""


def problems(text: str) -> list[str]:
    with pytest.raises(PolicyError) as exc:
        parse_policy(text)
    return exc.value.problems


def test_example_file_is_valid() -> None:
    policy = load_policy(EXAMPLE)
    assert policy.channels
    assert policy.approval.approvers_enabled is False


def test_defaults_are_off_and_permalinks() -> None:
    policy = parse_policy(MINIMAL)
    assert policy.issue_links == "permalinks"
    assert policy.approval.approvers == ()
    assert policy.approval.approvers_enabled is False
    assert policy.approval.reactions is False
    assert policy.agent.preset is None
    assert policy.debounce_for("C0TEST1") == 120


def test_channel_debounce_overrides_default() -> None:
    text = MINIMAL.replace("[channels.C0TEST1]\n", "[channels.C0TEST1]\ndebounce_seconds = 30\n")
    assert parse_policy(text).debounce_for("C0TEST1") == 30


def test_unknown_top_level_key_is_named() -> None:
    assert problems("issue_link = 'none'\n" + MINIMAL) == ["issue_link: unknown key"]


def test_unknown_nested_key_is_named() -> None:
    text = MINIMAL.replace('nickname = "api"', 'nickname = "api"\nlabel = ["bug"]')
    assert problems(text) == ["channels.C0TEST1.repos[1].label: unknown key"]


def test_missing_key_is_named() -> None:
    text = MINIMAL.replace('description = "The API"', "")
    assert problems(text) == ["channels.C0TEST1.repos[1].description: missing required key"]


def test_invalid_value_names_key_and_reason() -> None:
    text = MINIMAL.replace('nickname = "web"', 'nickname = "Web App"')
    [problem] = problems(text)
    assert problem.startswith("channels.C0TEST1.repos[0].nickname: 'Web App' is not a nickname")


def test_bad_channel_id_is_named() -> None:
    text = MINIMAL.replace("C0TEST1", "general")
    [problem] = problems(text)
    assert problem.startswith("channels.general: 'general' is not a Slack channel id")


def test_issue_links_accepts_only_known_values() -> None:
    [problem] = problems("issue_links = 'some'\n" + MINIMAL)
    assert problem.startswith("issue_links: ")


def test_every_problem_is_reported() -> None:
    text = "debounce_seconds = 0\nissue_links = 'some'\n" + MINIMAL
    keys = [p.split(":")[0] for p in problems(text)]
    assert keys == ["issue_links", "debounce_seconds"]


def test_channels_are_required() -> None:
    assert problems("") == ["channels: missing required key"]


def test_channel_needs_a_repo() -> None:
    [problem] = problems("[channels.C0TEST1]\nrepos = []\n")
    assert problem.startswith("channels.C0TEST1.repos: ")


def test_duplicate_nickname_is_rejected() -> None:
    text = MINIMAL.replace('nickname = "api"', 'nickname = "web"')
    assert problems(text) == ["channels.C0TEST1: nicknames used more than once: web"]


def test_duplicate_repo_is_rejected_ignoring_case() -> None:
    text = MINIMAL.replace('repo = "acme/api"', 'repo = "Acme/Web"')
    assert problems(text) == ["channels.C0TEST1: repos listed more than once: Acme/Web"]


def test_approver_ids_are_checked() -> None:
    [problem] = problems("[approval]\napprovers = ['alice']\n" + MINIMAL)
    assert problem.startswith("approval.approvers[0]: 'alice' is not a Slack user id")


def test_approvers_on_when_listed() -> None:
    policy = parse_policy("[approval]\napprovers = ['U0ALICE']\nreactions = true\n" + MINIMAL)
    assert policy.approval.approvers_enabled is True
    assert policy.approval.reactions is True


def test_invalid_toml_reports_location() -> None:
    [problem] = problems("issue_links = \n")
    assert problem.startswith("not valid TOML:")
    assert "line 1" in problem


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyError) as exc:
        load_policy(tmp_path / "jotflow.toml")
    assert exc.value.problems == ["file not found; run `jotflow init` to create it"]
    assert "jotflow.toml" in str(exc.value)


def test_load_from_file(tmp_path: Path) -> None:
    file = tmp_path / "jotflow.toml"
    file.write_text(MINIMAL, encoding="utf-8")
    assert isinstance(load_policy(file), Policy)


def test_unknown_channel() -> None:
    policy = parse_policy(MINIMAL)
    with pytest.raises(UnknownChannelError, match="C0OTHER"):
        policy.channel("C0OTHER")


def test_repo_lookup_by_name_or_nickname() -> None:
    policy = parse_policy(MINIMAL)
    assert policy.repo("C0TEST1", "acme/web").nickname == "web"
    assert policy.repo("C0TEST1", "ACME/Web").nickname == "web"
    assert policy.repo("C0TEST1", "api").repo == "acme/api"


def test_repo_outside_allowlist() -> None:
    policy = parse_policy(MINIMAL)
    with pytest.raises(RepoNotAllowedError) as exc:
        policy.repo("C0TEST1", "acme/secret")
    assert exc.value.channel_id == "C0TEST1"
    assert exc.value.repo == "acme/secret"


def test_labels_inside_the_list_ignore_case() -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", "web")
    assert repo.check_labels(["BUG", "enhancement", "bug"]) == ["bug", "Enhancement"]


def test_a_label_list_narrows() -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", "web")
    with pytest.raises(LabelNotAllowedError) as exc:
        repo.check_labels(["bug", "wontfix"])
    assert exc.value.labels == ["wontfix"]


def test_a_repo_without_labels_allows_any_label() -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", "api")
    assert repo.labels is None
    assert not repo.restricts_labels
    assert repo.check_labels([]) == []
    assert repo.check_labels(["bug", "Needs Design", "size:m"]) == ["bug", "Needs Design", "size:m"]


def test_an_explicit_empty_label_list_is_an_error() -> None:
    text = MINIMAL.replace('description = "The API"', 'description = "The API"\nlabels = []')
    assert problems(text) == [
        "channels.C0TEST1.repos[1].labels: an empty labels list is ambiguous; remove the "
        "labels line to allow any existing label, or list the labels to allow"
    ]


def test_unrestricted_labels_drop_duplicates_ignoring_case() -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", "api")
    assert repo.check_labels(["Bug", "bug", "BUG", "docs"]) == ["Bug", "docs"]


def test_a_listed_repo_restricts_labels() -> None:
    assert parse_policy(MINIMAL).repo("C0TEST1", "web").restricts_labels


def test_label_list_error_names_the_list() -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", "web")
    with pytest.raises(LabelNotAllowedError, match="not in acme/web's label list: 'wontfix'"):
        repo.check_labels(["wontfix"])


def test_policy_is_immutable() -> None:
    policy = parse_policy(MINIMAL)
    with pytest.raises(ValueError):
        policy.debounce_seconds = 5  # type: ignore[misc]


def test_repo_names_are_ascii_only() -> None:
    [problem] = problems(MINIMAL.replace('repo = "acme/api"', 'repo = "acmé/api"'))
    assert problem.startswith("channels.C0TEST1.repos[1].repo: ")


def test_lists_are_immutable() -> None:
    policy = parse_policy("[approval]\napprovers = ['U0ALICE']\n" + MINIMAL)
    assert isinstance(policy.approval.approvers, tuple)
    assert isinstance(policy.channel("C0TEST1").repos, tuple)
    assert isinstance(policy.repo("C0TEST1", "web").labels, tuple)


@pytest.mark.parametrize("repo", ["acme/.", "acme/.."])
def test_dot_repo_names_are_rejected(repo: str) -> None:
    [problem] = problems(MINIMAL.replace('repo = "acme/api"', f'repo = "{repo}"'))
    assert problem.startswith("channels.C0TEST1.repos[1].repo: ")


def test_rejected_values_are_cut_short() -> None:
    token = "xoxb-" + "1234567890" * 6
    [problem] = problems(f"[approval]\napprovers = ['{token}']\n" + MINIMAL)
    assert token not in problem
    assert token[:40] in problem


def test_a_label_listed_twice_is_rejected_ignoring_case() -> None:
    text = MINIMAL.replace('labels = ["bug", "Enhancement"]', 'labels = ["bug", "Bug"]')
    assert problems(text) == ["channels.C0TEST1.repos[0]: labels listed more than once: Bug"]


UNSAFE_LABELS = [
    "",
    "x" * 51,
    " bug",
    "bug ",
    "two\nlines",
    "tab\there",
    "bell\x07",
    "line\u2028separator",
    "bidi\u202eoverride",
    "isolate\u2066x",
    "b\u200bug",
    "bug\ufeff",
    "word\u2060joiner",
    "soft\u00adhyphen",
    "lone\u200djoiner",
    "\U0001f680\u200d",
    "\u200d\U0001f680",
    "a\u200d\U0001f680",
]


@pytest.mark.parametrize("repo_ref", ["web", "api"])
@pytest.mark.parametrize("bad", UNSAFE_LABELS)
def test_unsafe_label_names_are_rejected_either_way(repo_ref: str, bad: str) -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", repo_ref)
    with pytest.raises(InvalidLabelError) as exc:
        repo.check_labels(["bug", bad])
    assert exc.value.labels == [bad]


@pytest.mark.parametrize(
    "good",
    [
        "size:m",
        "Needs Design",
        "good first issue",
        "\U0001f680 launch",
        "\U0001f469\u200d\U0001f4bb dev",  # woman technologist
        "\U0001f469\U0001f3fd\u200d\U0001f4bb dev",  # with a skin tone
        "\U0001f3f3\ufe0f\u200d\U0001f308 pride",  # rainbow flag
        "caf\u00e9",
    ],
)
def test_ordinary_and_emoji_labels_are_allowed(good: str) -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", "api")
    assert repo.check_labels([good]) == [good]


@pytest.mark.parametrize("bad", ["bug\\n", " bug", "bidi\\u202e"])
def test_unsafe_labels_in_the_file_are_named(bad: str) -> None:
    text = MINIMAL.replace('labels = ["bug", "Enhancement"]', f'labels = ["{bad}"]')
    [problem] = problems(text)
    assert problem.startswith("channels.C0TEST1.repos[0].labels[0]: label ")


def test_invalid_label_error_says_why() -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", "api")
    with pytest.raises(InvalidLabelError, match="must not contain control characters"):
        repo.check_labels(["a\nb"])


def test_invisible_characters_are_named() -> None:
    repo = parse_policy(MINIMAL).repo("C0TEST1", "api")
    with pytest.raises(InvalidLabelError, match="invisible characters"):
        repo.check_labels(["b\u200bug"])
