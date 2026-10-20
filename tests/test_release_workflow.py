"""release.yml's publish jobs each collect exactly their own image's digests."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
IMAGES = ("jotflow", "jotflow-agent")
ARCHES = ("amd64", "arm64")


def _render(template: str, image: str, arch: str = "") -> str:
    return template.replace("${{ matrix.image }}", image).replace("${{ matrix.arch }}", arch)


def test_each_publish_job_downloads_only_its_own_digests() -> None:
    # v0.1.0's first run failed here: "digest-jotflow-*" also matched jotflow-agent's
    # digests, so the jotflow publish job saw four instead of two.
    text = WORKFLOW.read_text()
    upload = re.search(r"^\s+name: (digest.*)$", text, re.M)
    download = re.search(r"^\s+pattern: (digest.*)$", text, re.M)
    assert upload and download
    names = [_render(upload[1], image, arch) for image in IMAGES for arch in ARCHES]
    for image in IMAGES:
        pattern = _render(download[1], image)
        matched = sorted(name for name in names if fnmatchcase(name, pattern))
        assert matched == sorted(_render(upload[1], image, arch) for arch in ARCHES)
