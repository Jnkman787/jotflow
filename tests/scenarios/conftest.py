"""Each scenario runs on both stores, with the scenario's own clock (#380)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from jotflow.store import MemoryStore, SqliteStore, Store
from tests.scenarios.harness import Clock


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(params=["memory", "sqlite"])
async def store(
    request: pytest.FixtureRequest, tmp_path: Path, clock: Clock
) -> AsyncIterator[Store]:
    made: Store = (
        MemoryStore(clock=clock)
        if request.param == "memory"
        else await SqliteStore.open(tmp_path / "jotflow.db", clock=clock)
    )
    yield made
    await made.close()
