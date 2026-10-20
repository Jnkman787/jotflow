"""`jotflow runner`: the agent container's runner (#54, D6).

Runs inside the agent container, never in JotFlow's. It reads its settings
from its own process environment, which Compose fills from ``agent.env``; it
never reads a ``.env`` file. See :mod:`jotflow.adapters.runner`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import PurePosixPath

import uvicorn
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from jotflow.adapters.presets import DEFAULT_GITHUB_MCP, EXPERIMENTAL
from jotflow.adapters.runner import (
    RUNNER_PORT,
    ConfigError,
    Runner,
    RunnerConfig,
    build_runner_app,
    parse_command,
    parse_preset,
    remove_stale,
)
from jotflow.logs import configure_logging
from jotflow.models import RunKind
from jotflow.netcheck import AddressRefused

EXIT_CONFIG = 2

log = logging.getLogger("jotflow.runner")


class RunnerSettings(BaseSettings):
    """The runner's environment. Not JotFlow's Settings: no Slack token belongs here."""

    model_config = SettingsConfigDict(
        env_prefix="JOTFLOW_", env_file=None, extra="ignore", hide_input_in_errors=True
    )

    runner_secret: SecretStr = Field(description="Shared with JotFlow; never given to the agent")
    agent_preset: str | None = Field(default=None, description="A preset name, such as claude-code")
    agent_command: str | None = Field(
        default=None, description="JSON list: the agent CLI and its arguments, without a preset"
    )
    agent_model: str | None = Field(default=None, description="The model for every run kind")
    agent_model_drafting: str | None = None
    agent_model_question: str | None = None
    agent_model_execution: str | None = None
    github_mcp_command: str = Field(
        default=DEFAULT_GITHUB_MCP, description="The pinned GitHub MCP server, for presets"
    )
    github_read_token: SecretStr | None = None
    github_write_token: SecretStr | None = None
    agent_env_allow: str = Field(default="", description="Comma-separated names to pass on")
    mcp_url: str = "http://jotflow:8765/mcp"
    mcp_allow_public: bool = Field(
        default=False,
        description="Allow an MCP URL whose host resolves to a public address (#234)",
    )
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


def _models(settings: RunnerSettings, kinds: frozenset[RunKind]) -> dict[RunKind, str]:
    """JOTFLOW_AGENT_MODEL for every kind served, then any per-kind override."""
    models = {kind: settings.agent_model for kind in kinds if settings.agent_model}
    for kind, model in (
        (RunKind.DRAFTING, settings.agent_model_drafting),
        (RunKind.QUESTION, settings.agent_model_question),
        (RunKind.EXECUTION, settings.agent_model_execution),
    ):
        if model:
            models[kind] = model
    return models


def build_config(settings: RunnerSettings) -> RunnerConfig:
    if (settings.agent_preset is None) == (settings.agent_command is None):
        raise ConfigError("set exactly one of JOTFLOW_AGENT_PRESET and JOTFLOW_AGENT_COMMAND")
    preset = None if settings.agent_preset is None else parse_preset(settings.agent_preset)
    check_install = getattr(preset, "check_install", None)
    if check_install is not None and (problem := check_install()) is not None:
        raise ConfigError(problem)  # fail closed: a preset the installed CLI can't honour
    if preset is not None and preset.needs_github_mcp:
        path = settings.github_mcp_command
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise ConfigError(f"the GitHub MCP server is not installed at {path}")
    kinds = _kinds(settings.runner_kinds)
    port = settings.runner_port
    names = [h.strip() for h in settings.runner_allowed_hosts.split(",") if h.strip()]
    hosts = {f"{name}:{port}" for name in [*names, "127.0.0.1", "localhost"]}
    return RunnerConfig(
        secret=settings.runner_secret.get_secret_value(),
        mcp_url=settings.mcp_url,
        mcp_allow_public=settings.mcp_allow_public,
        command=() if settings.agent_command is None else parse_command(settings.agent_command),
        preset=preset,
        models=_models(settings, kinds),
        github_mcp=settings.github_mcp_command,
        read_token=(
            settings.github_read_token.get_secret_value() if settings.github_read_token else None
        ),
        write_token=(
            settings.github_write_token.get_secret_value() if settings.github_write_token else None
        ),
        env_allow=tuple(n.strip() for n in settings.agent_env_allow.split(",") if n.strip()),
        kinds=kinds,
        allowed_hosts=frozenset(hosts),
    )


def startup_line(config: RunnerConfig) -> str:
    """What the runner serves, for its log: kinds and the preset, or the program alone.

    ``[agent] preset`` in JotFlow's jotflow.toml must name the same agent.
    """
    kinds = ", ".join(k.value for k in RunKind if k in config.kinds)
    if config.preset is not None:
        note = " (experimental)" if config.preset.name in EXPERIMENTAL else ""
        return f"runner serving {kinds} with preset {config.preset.name}{note}"
    program = PurePosixPath(config.command[0]).name if config.command else "?"
    return f"runner serving {kinds} with command {program}"


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
    log.info("%s", startup_line(config))
    runner = Runner(config)
    try:
        await runner.check_mcp_host()
    except AddressRefused as refused:
        if not refused.unresolved:
            print(f"jotflow runner: configuration problem: {refused}", file=sys.stderr)
            return EXIT_CONFIG
        # JotFlow may still be starting; each launch checks again.
        log.warning("%s", refused)
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
