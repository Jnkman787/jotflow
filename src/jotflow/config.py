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
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # Agent runners (#54, #21). Each run kind goes to its own runner with its
    # own secret, and a secret is only ever sent to its own runner.
    runner_url: str = Field(
        default="http://agent:8766",
        description="Runner for drafting and question runs",
    )
    runner_exec_url: str = Field(
        default="http://agent-exec:8766",
        description="Runner for execution runs",
    )
    runner_secret: SecretStr | None = Field(
        default=None,
        description="Shared secret of the drafting runner; matches agent.env",
    )
    runner_exec_secret: SecretStr | None = Field(
        default=None,
        description="Shared secret of the execution runner; matches agent-exec.env",
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

    @field_validator("runner_url", "runner_exec_url")
    @classmethod
    def _runner_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("must be an http or https URL with only a host and port")
        return value.rstrip("/")

    @field_validator("runner_secret", "runner_exec_secret", mode="before")
    @classmethod
    def _empty_secret(cls, value: object) -> object:
        # An empty line in .env means unset, not an empty secret.
        return None if value == "" else value

    @model_validator(mode="after")
    def _distinct_runner_secrets(self) -> Settings:
        drafting, execution = self.runner_secret, self.runner_exec_secret
        if (
            drafting is not None
            and execution is not None
            and drafting.get_secret_value() == execution.get_secret_value()
        ):
            # The execution runner's isolation rests on a drafting run, which
            # can read its own runner's secret, not holding the other one.
            raise ValueError("JOTFLOW_RUNNER_SECRET and JOTFLOW_RUNNER_EXEC_SECRET must differ")
        return self

    # Admin policy file; see examples/jotflow.example.toml
    config_path: str = Field(
        default="jotflow.toml",
        validation_alias="JOTFLOW_CONFIG",
        description="Path to the jotflow.toml policy file",
    )
