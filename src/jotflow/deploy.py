"""The deployment files a team gets without cloning the repository (#87).

The package carries copies of the repository's own ``docker/docker-compose.yml``,
``docker/docker-compose.policy.yml``, and ``examples/``'s agent env examples
under ``jotflow/templates``. A test fails if a copy ever differs from the
repository's file; ``make templates`` copies them again. They live in the
package, not force-included at build time, because the image builds see only
``src/``. :func:`published_compose` turns the base Compose file into one
that runs the published images: it drops each ``build:`` block and pins the
image defaults to this release. The ``${JOTFLOW_IMAGE_REF:-…}`` overrides stay,
so a team can still run another image from ``.env`` (#39).
"""

from __future__ import annotations

import re
from importlib import resources

from jotflow import __version__

REGISTRY = "ghcr.io/jnkman787"
COMPOSE = "docker-compose.yml"
COMPOSE_POLICY = "docker-compose.policy.yml"
# Per-run uids in the agent containers (#181), opt-in through COMPOSE_FILE.
COMPOSE_ISOLATION = "docker-compose.isolation.yml"
AGENT_EXAMPLES = ("agent.env.example", "agent-exec.env.example")
TEMPLATES = (COMPOSE, COMPOSE_POLICY, COMPOSE_ISOLATION, *AGENT_EXAMPLES)

# The image defaults in the checkout's Compose file, built locally.
_LOCAL_IMAGES = {"jotflow:local": "jotflow", "jotflow-agent:local": "jotflow-agent"}
_RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class NoPublishedImageError(RuntimeError):
    """This version of JotFlow has no published image to pin, such as a development build."""


def template(name: str) -> str:
    """A deployment file's text, from the package."""
    if name not in TEMPLATES:
        raise ValueError(f"no deployment template {name!r}")
    return resources.files("jotflow").joinpath("templates", name).read_text(encoding="utf-8")


def release_version(version: str = __version__) -> str:
    """The version to pin images to. Only a plain X.Y.Z release has published images."""
    if not _RELEASE.fullmatch(version) or version == "0.0.1":
        raise NoPublishedImageError(
            f"JotFlow {version} is not a published release, so there is no image to pin"
        )
    return version


def published_compose(text: str, version: str) -> str:
    """The checkout's Compose file, made to run the published images of ``version``."""
    lines: list[str] = []
    skipping: int | None = None
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        if skipping is not None:
            if line.strip() and indent <= skipping:
                skipping = None
            else:
                continue
        if line.strip().startswith("build:"):
            # A one-line `build: .` or a block whose lines are indented deeper.
            skipping = None if line.strip() != "build:" else indent
            continue
        lines.append(line)
    header = [
        f"# Written by `jotflow init` for JotFlow {version}. It runs the published images,",
        "# so no checkout is needed. Set JOTFLOW_IMAGE_REF or JOTFLOW_AGENT_IMAGE_REF in .env",
        "# to run others.",
        "#",
    ]
    out = "\n".join([*header, *lines]) + "\n"
    for local, name in _LOCAL_IMAGES.items():
        out = out.replace(f":-{local}}}", f":-{REGISTRY}/{name}:{version}}}")
    return out
