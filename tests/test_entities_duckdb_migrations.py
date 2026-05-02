"""Migration checks for DuckDbEntityStore."""

from pathlib import Path
from typing import Protocol, cast

import duckdb
import pytest

from caqrs.entities import EntityStoreError
from caqrs.entities.duckdb import DuckDbEntityStore


class _DuckDbConnection(Protocol):
    def close(self) -> None: ...

    def execute(self, query: str) -> object: ...


def test_init_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "store.duckdb"
    with DuckDbEntityStore(path=path):
        pass

    with DuckDbEntityStore(path=path) as store:
        version = store._conn.execute("SELECT version FROM entities_schema_version").fetchone()

    assert version == (1,)


def test_unsupported_schema_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "store.duckdb"
    conn = cast(_DuckDbConnection, duckdb.connect(str(path)))
    try:
        conn.execute("CREATE TABLE entities_schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO entities_schema_version (version) VALUES (99)")
    finally:
        conn.close()

    with pytest.raises(EntityStoreError, match="unsupported entities schema version 99"):
        DuckDbEntityStore(path=path)
