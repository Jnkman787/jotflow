from pathlib import Path

import pytest
from pydantic import ValidationError

from jotflow.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Keep a developer's real .env and environment out of these tests.
    monkeypatch.chdir(tmp_path)
    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "MCP_HOST", "MCP_PORT", "CONFIG"):
        monkeypatch.delenv(f"JOTFLOW_{name}", raising=False)
    monkeypatch.setenv("JOTFLOW_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JOTFLOW_SLACK_APP_TOKEN", "xapp-test")


def test_settings_from_environment() -> None:
    settings = Settings()
    assert settings.slack_bot_token.get_secret_value() == "xoxb-test"
    assert settings.mcp_host == "127.0.0.1"
    assert settings.config_path == "jotflow.toml"


def test_config_path_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOTFLOW_CONFIG", "/etc/jotflow/jotflow.toml")
    assert Settings().config_path == "/etc/jotflow/jotflow.toml"


def test_settings_hold_no_github_credentials() -> None:
    assert not [name for name in Settings.model_fields if "github" in name]


def test_policy_values_are_not_settings() -> None:
    assert "debounce_seconds" not in Settings.model_fields


def test_tokens_are_not_printed() -> None:
    assert "xoxb-test" not in repr(Settings())


def test_validation_errors_do_not_echo_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOTFLOW_MCP_PORT", "xoxb-secret-value")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert "xoxb-secret-value" not in str(exc.value)
