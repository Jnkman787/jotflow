"""Runtime configuration, loaded from environment variables.

No secrets are ever read from files inside the repository. Teams supply
Slack and GitHub App credentials through the environment or a local
``.env`` file that is gitignored.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UPSHOT_", env_file=".env", extra="ignore")

    # Slack
    slack_bot_token: SecretStr
    slack_app_token: SecretStr = Field(description="App-level token for Socket Mode (xapp-...)")

    # GitHub App installed by the team on the repositories Upshot may write to
    github_app_id: int
    github_app_private_key: SecretStr
    github_installation_id: int

    # Whiteboard MCP server
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765

    # Local state. Pointers only; see docs/architecture.md.
    database_path: str = "upshot.db"

    # Agent adapter: a command template run in headless mode with the MCP server attached
    agent_command: str = Field(
        default="",
        description="Shell command template for the team's agent. Empty disables agent runs.",
    )

    # Seconds a thread must be quiet before it is handed to the agent
    debounce_seconds: int = 120
