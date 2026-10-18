"""Runtime settings, loaded from environment variables.

This is the environment tier of D2: secrets and process knobs. Admin policy,
such as which repos a channel may feed, lives in ``jotflow.toml`` and is
loaded by :mod:`jotflow.policy`.

No secrets are ever read from files inside the repository. Teams supply the
Slack tokens through the environment or a local ``.env`` file that is
gitignored. JotFlow's code never uses GitHub credentials (D5); the team's
agent brings its own, in its own container (D6).
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_HOST_NAME = re.compile(r"[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOTFLOW_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
        # Never echo a value in a validation error: it may be a token.
        hide_input_in_errors=True,
    )

    # Slack
    slack_bot_token: SecretStr = Field(description="Bot token (xoxb-...)")
    slack_app_token: SecretStr = Field(description="App-level token for Socket Mode (xapp-...)")

    # Whiteboard MCP server. Outside Compose it stays on loopback.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8765, ge=1, le=65535)
    mcp_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(),
        description="Extra Host names the MCP server answers, comma-separated, such as the "
        "Compose service name; loopback is always allowed",
    )

    # Local state. Pointers only; see docs/architecture.md.
    database_path: str = "jotflow.db"

    @field_validator("mcp_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            value = tuple(part.strip() for part in value.split(",") if part.strip())
        if isinstance(value, tuple | list):
            for name in value:
                if not isinstance(name, str) or not _HOST_NAME.fullmatch(name):
                    raise ValueError("each entry must be a host name, optionally with :port")
        return value

    # Admin policy file; see jotflow.example.toml
    config_path: str = Field(
        default="jotflow.toml",
        validation_alias="JOTFLOW_CONFIG",
        description="Path to the jotflow.toml policy file",
    )
