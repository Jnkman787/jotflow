"""`jotflow runner`: the agent container's runner (#54, D6).

Runs inside the agent container, never in JotFlow's. It reads its settings
from its own process environment, which Compose fills from ``agent.env``; it
never reads a ``.env`` file. See :mod:`jotflow.adapters.runner`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

import uvicorn
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from jotflow.adapters.runner import (
    RUNNER_PORT,
    ConfigError,
    Runner,
    RunnerConfig,
    build_runner_app,
    parse_command,
    remove_stale,
)
from jotflow.logs import configure_logging
from jotflow.models import RunKind

EXIT_CONFIG = 2


class RunnerSettings(BaseSettings):
    """The runner's environment. Not JotFlow's Settings: no Slack token belongs here."""

    model_config = SettingsConfigDict(
        env_prefix="JOTFLOW_", env_file=None, extra="ignore", hide_input_in_errors=True
    )

    runner_secret: SecretStr = Field(description="Shared with JotFlow; never given to the agent")
    agent_command: str = Field(description="JSON list: the agent CLI and its arguments")
    github_read_token: SecretStr | None = None
    github_write_token: SecretStr | None = None
    agent_env_allow: str = Field(default="", description="Comma-separated names to pass on")
    mcp_url: str = "http://jotflow:8765/mcp"
    runner_host: str = "0.0.0.0"  # the internal Compose network only; no port is published
    runner_port: int = Field(default=RUNNER_PORT, ge=1, le=65535)
    runner_allowed_hosts: str = Field(default="agent", description="Comma-separated Host names")
    runner_kinds: str = Field(
        default="drafting,question",
        description="The run kinds this runner serves: drafting,question or execution",
    )


def _kinds(raw: str) -> frozenset[RunKind]:
    try:
        return frozenset(RunKind(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError:
        raise ConfigError(f"JOTFLOW_RUNNER_KINDS has an unknown run kind: {raw!r}") from None


def build_config(settings: RunnerSettings) -> RunnerConfig:
    port = settings.runner_port
    names = [h.strip() for h in settings.runner_allowed_hosts.split(",") if h.strip()]
    hosts = {f"{name}:{port}" for name in [*names, "127.0.0.1", "localhost"]}
    return RunnerConfig(
        secret=settings.runner_secret.get_secret_value(),
        mcp_url=settings.mcp_url,
        command=parse_command(settings.agent_command),
        read_token=(
            settings.github_read_token.get_secret_value() if settings.github_read_token else None
        ),
        write_token=(
            settings.github_write_token.get_secret_value() if settings.github_write_token else None
        ),
        env_allow=tuple(n.strip() for n in settings.agent_env_allow.split(",") if n.strip()),
        kinds=_kinds(settings.runner_kinds),
        allowed_hosts=frozenset(hosts),
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        default="info",
        help="The runner's log level (default: info)",
    )


async def serve(settings: RunnerSettings, config: RunnerConfig) -> int:
    removed = remove_stale(config.workdir)
    if removed:
        print(f"jotflow runner: removed {removed} leftover run directories", file=sys.stderr)
    runner = Runner(config)
    server = uvicorn.Server(
        uvicorn.Config(
            build_runner_app(runner, config),
            host=settings.runner_host,
            port=settings.runner_port,
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=False,
            limit_concurrency=16,
            timeout_keep_alive=5,
        )
    )
    try:
        await server.serve()
    finally:
        await runner.close()
    return 0


def run(args: argparse.Namespace) -> int:
    configure_logging(getattr(args, "log_level", "info"))
    try:
        settings = RunnerSettings()  # type: ignore[call-arg]  # filled from the environment
        config = build_config(settings)
    except ValidationError as exc:
        names = ", ".join(f"JOTFLOW_{str(e['loc'][0]).upper()}" for e in exc.errors())
        print(f"jotflow runner: configuration problem in {names}", file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"jotflow runner: configuration problem: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(serve(settings, config))
    return 130
