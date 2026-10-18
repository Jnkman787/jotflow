from __future__ import annotations

import json
from pathlib import Path

import pytest

from jotflow.slack_manifest import (
    DEFAULT_BOT_NAME,
    MAX_BOT_NAME_LENGTH,
    BotNameError,
    main,
    render_manifest,
    render_manifest_json,
    validate_bot_name,
)

CHECKED_IN = Path(__file__).resolve().parents[1] / "manifests" / "slack-app.json"


def test_bot_scopes_match_architecture_permissions() -> None:
    scopes = render_manifest()["oauth_config"]["scopes"]["bot"]
    assert sorted(scopes) == sorted(
        [
            "channels:history",
            "groups:history",
            "channels:read",
            "groups:read",
            "chat:write",
            "reactions:read",
            "reactions:write",
            "pins:write",
            "users:read",
            "commands",
        ]
    )
    assert len(scopes) == len(set(scopes))


def test_no_user_scopes_and_no_mentions_before_m5() -> None:
    manifest = render_manifest()
    assert "user" not in manifest["oauth_config"]["scopes"]
    assert "app_mentions:read" not in manifest["oauth_config"]["scopes"]["bot"]
    assert "app_mention" not in manifest["settings"]["event_subscriptions"]["bot_events"]


def test_bot_cannot_join_channels_by_itself() -> None:
    assert "channels:join" not in render_manifest()["oauth_config"]["scopes"]["bot"]


def test_app_home_tabs_are_off() -> None:
    app_home = render_manifest()["features"]["app_home"]
    assert app_home == {
        "home_tab_enabled": False,
        "messages_tab_enabled": False,
        "messages_tab_read_only_enabled": False,
    }


def test_event_subscriptions() -> None:
    events = render_manifest()["settings"]["event_subscriptions"]["bot_events"]
    assert sorted(events) == ["message.channels", "message.groups", "reaction_added"]


def test_socket_mode_needs_no_public_urls() -> None:
    settings = render_manifest()["settings"]
    assert settings["socket_mode_enabled"] is True
    assert settings["interactivity"]["is_enabled"] is True
    assert "request_url" not in settings["event_subscriptions"]
    assert "request_url" not in settings["interactivity"]


def test_slash_command() -> None:
    commands = render_manifest()["features"]["slash_commands"]
    assert [c["command"] for c in commands] == ["/jotflow"]
    assert "url" not in commands[0]


def test_not_distributable() -> None:
    manifest = render_manifest()
    assert manifest["settings"]["org_deploy_enabled"] is False
    assert "distribution" not in json.dumps(manifest)
    assert "redirect_urls" not in manifest["oauth_config"]


def test_default_bot_name() -> None:
    manifest = render_manifest()
    assert manifest["display_information"]["name"] == DEFAULT_BOT_NAME
    assert manifest["features"]["bot_user"]["display_name"] == DEFAULT_BOT_NAME


def test_custom_bot_name_is_applied() -> None:
    manifest = render_manifest("  Idea Bot  ")
    assert manifest["display_information"]["name"] == "Idea Bot"
    assert manifest["features"]["bot_user"]["display_name"] == "Idea Bot"
    assert "Idea Bot" in manifest["features"]["slash_commands"][0]["description"]


@pytest.mark.parametrize("name", ["a", "Ideas-Bot_2", "team.ideas", "x" * MAX_BOT_NAME_LENGTH])
def test_valid_bot_names(name: str) -> None:
    assert validate_bot_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "x" * (MAX_BOT_NAME_LENGTH + 1),
        "-leading-dash",
        " .hidden",
        "bot<script>",
        'quote"bot',
        "new\nline",
        "tab\tbot",
        "emoji 🤖",
        "@channel",
    ],
)
def test_invalid_bot_names_are_rejected(name: str) -> None:
    with pytest.raises(BotNameError):
        render_manifest(name)


def test_json_is_stable_and_parses() -> None:
    text = render_manifest_json("Idea Bot")
    assert text.endswith("\n")
    assert json.loads(text) == render_manifest("Idea Bot")
    assert text == render_manifest_json("Idea Bot")


def test_checked_in_manifest_matches_renderer() -> None:
    assert CHECKED_IN.read_text(encoding="utf-8") == render_manifest_json(), (
        "manifests/slack-app.json is out of date; run `make manifest`"
    )


def test_main_prints_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["Idea Bot"]) == 0
    assert json.loads(capsys.readouterr().out)["display_information"]["name"] == "Idea Bot"


def test_main_rejects_bad_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["<bad>"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err


def test_main_rejects_extra_arguments() -> None:
    assert main(["a", "b"]) == 2
