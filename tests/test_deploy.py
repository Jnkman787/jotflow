"""The deployment files a team gets without cloning (#87)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jotflow import deploy

ROOT = Path(__file__).resolve().parents[1]


def test_templates_are_the_checkouts_own_files() -> None:
    for name in deploy.TEMPLATES:
        assert deploy.template(name) == (ROOT / name).read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        deploy.template("Dockerfile")


def test_the_published_compose_file_runs_this_releases_images_and_builds_nothing() -> None:
    text = deploy.published_compose(deploy.template(deploy.COMPOSE), "1.2.3")
    assert "build:" not in text and "context:" not in text and "dockerfile:" not in text
    assert "image: ${JOTFLOW_IMAGE_REF:-ghcr.io/jnkman787/jotflow:1.2.3}" in text
    assert (
        text.count("image: ${JOTFLOW_AGENT_IMAGE_REF:-ghcr.io/jnkman787/jotflow-agent:1.2.3}") == 2
    )
    assert ":local" not in text
    assert text.startswith("# Written by `jotflow init` for JotFlow 1.2.3.")


def test_everything_but_the_build_and_image_lines_is_unchanged() -> None:
    source = deploy.template(deploy.COMPOSE)
    published = deploy.published_compose(source, "1.2.3")
    kept = [line for line in source.splitlines() if "image:" not in line]
    out = [line for line in published.splitlines() if "image:" not in line]
    # Every published line, after the header, comes from the source, in order.
    body = out[4:]
    it = iter(kept)
    assert all(any(line == candidate for candidate in it) for line in body)
    for word in ("cap_drop", "no-new-privileges:true", "read_only: true", "profiles: [agent]"):
        assert published.count(word) == source.count(word), word


def test_a_build_block_ends_at_the_next_key_of_its_service() -> None:
    text = (
        "services:\n  a:\n    build:\n      context: .\n      args:\n        X: 1\n"
        "    image: ${R:-jotflow:local}\n  b:\n    build: .\n    image: x\n"
    )
    out = deploy.published_compose(text, "2.0.0")
    assert "build" not in out and "context" not in out and "X: 1" not in out
    assert "    image: ${R:-ghcr.io/jnkman787/jotflow:2.0.0}" in out and "    image: x" in out


@pytest.mark.parametrize("version", ["0.0.1", "1.2", "1.2.3.dev4", "1.2.3+local", "v1.2.3"])
def test_only_a_published_release_has_images_to_pin(version: str) -> None:
    with pytest.raises(deploy.NoPublishedImageError):
        deploy.release_version(version)


def test_a_release_version_is_pinned_as_is() -> None:
    assert deploy.release_version("1.2.3") == "1.2.3"


def test_the_wheel_force_includes_every_template() -> None:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    included = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included == {name: f"jotflow/templates/{name}" for name in deploy.TEMPLATES}
