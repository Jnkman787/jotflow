# syntax=docker/dockerfile:1
#
# The JotFlow container: orchestrator and MCP server only. It carries no Node
# and no agent CLI; the team's agent runs in its own container (D6).
#
# Base images are pinned by tag and digest. Dependabot keeps them current;
# both stages must use the same Python image so the venv's interpreter matches.

FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

# Builder: resolve exactly what uv.lock records, nothing more.
FROM python:3.15-slim-trixie@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS builder

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
FROM python:3.15-slim-trixie@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS runtime

ARG JOTFLOW_UID=10001

# Distrust the compromised eMudhra/emSign roots, and fail the build if any is
# still trusted (#360): see docker/distrust-ca.sh.
RUN --mount=type=bind,source=docker/distrust-ca.sh,target=/tmp/distrust-ca.sh \
    sh /tmp/distrust-ca.sh

RUN groupadd --system --gid "${JOTFLOW_UID}" jotflow \
    && useradd --system --uid "${JOTFLOW_UID}" --gid jotflow \
        --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin jotflow \
    && install -d -o jotflow -g jotflow -m 0700 /data

COPY --from=builder /app/.venv /app/.venv

# JOTFLOW_IMAGE marks JotFlow's own image. `jotflow check --live` refuses to
# run here, because Compose's env_file would carry an opt-in left in .env.
# A guard against accidents, not a boundary: env_file or -e can override it.
ENV PATH="/app/.venv/bin:${PATH}" \
    JOTFLOW_IMAGE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JOTFLOW_DATABASE_PATH=/data/jotflow.db

WORKDIR /data
USER jotflow:jotflow

CMD ["jotflow", "run"]
