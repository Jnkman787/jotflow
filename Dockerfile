# syntax=docker/dockerfile:1
#
# The JotFlow container: orchestrator and MCP server only. It carries no Node
# and no agent CLI; the team's agent runs in its own container (D6).
#
# Base images are pinned by tag and digest. Dependabot keeps them current;
# both stages must use the same Python image so the venv's interpreter matches.

FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

# Builder: resolve exactly what uv.lock records, nothing more.
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# Dependencies first, so source edits do not invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Runtime: the virtual environment and an unprivileged user, nothing else.
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime

ARG JOTFLOW_UID=10001

RUN groupadd --system --gid "${JOTFLOW_UID}" jotflow \
    && useradd --system --uid "${JOTFLOW_UID}" --gid jotflow \
        --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin jotflow \
    && install -d -o jotflow -g jotflow -m 0700 /data

COPY --from=builder /app/.venv /app/.venv

# JOTFLOW_IMAGE marks JotFlow's own image. `jotflow check --live` refuses to
# run here, because Compose's env_file would carry an opt-in left in .env.
ENV PATH="/app/.venv/bin:${PATH}" \
    JOTFLOW_IMAGE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JOTFLOW_DATABASE_PATH=/data/jotflow.db

WORKDIR /data
USER jotflow:jotflow

CMD ["jotflow", "run"]
