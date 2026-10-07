from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

from newsroom.db import Database


ROOT = Path(__file__).resolve().parents[1]
GUARDED_INIT_DOCUMENTS = (
    ROOT / "docs/WORKFLOW.md",
    ROOT / "docs/superpowers/plans/2026-10-05-batch-workspace-finder-workflow.md",
)


def _guarded_init_block(path: Path) -> str:
    blocks = re.findall(r"```bash\n(.*?)\n```", path.read_text(encoding="utf-8"), re.DOTALL)
    matches = [block for block in blocks if "PRAGMA wal_checkpoint(TRUNCATE)" in block]
    assert len(matches) == 1, f"expected one guarded init block in {path}"
    python_blocks = re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", matches[0], re.DOTALL)
    guarded = [source for source in python_blocks if "PRAGMA wal_checkpoint(TRUNCATE)" in source]
    assert len(guarded) == 1, f"missing embedded guarded-init Python in {path}"
    return guarded[0]


def _instrumented_guarded_init(path: Path) -> str:
    source = _guarded_init_block(path)
    assert "from contextlib import closing" in source
    assert "from newsroom.db import Database" in source
    assert re.search(r"with closing\(\s*sqlite3\.connect\(", source)
    validation = "existing_version = Database(database).validate_existing_schema()"
    assert validation in source
    assert source.index(validation) < source.index("with closing(")
    assert "if existing_version != 0:" in source
    close_check = "    assert_connection_closed(opened[-1])\n"
    sidecar_check = "    if (wal.exists() and wal.stat().st_size != 0) or shm.exists():"
    assert sidecar_check in source
    executable = source.replace(sidecar_check, close_check + sidecar_check)
    return executable.replace("    Newsroom(root).init()", "    pass")


def _track_connections(monkeypatch: pytest.MonkeyPatch):
    real_connect = sqlite3.connect
    real_validate = Database.validate_existing_schema
    opened: list[sqlite3.Connection] = []
    checkpoint_calls: list[str] = []
    events: list[str] = []

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "wal_checkpoint" in sql.lower():
                checkpoint_calls.append(sql)
                events.append("checkpoint")
            return super().execute(sql, parameters)

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    def tracking_validate(database: Database) -> int:
        events.append("validate")
        return real_validate(database)

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(Database, "validate_existing_schema", tracking_validate)
    return opened, checkpoint_calls, events


def _assert_connection_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("path", GUARDED_INIT_DOCUMENTS, ids=lambda path: path.name)
def test_guarded_init_documentation_validates_supported_wal_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: Path
):
    executable = _instrumented_guarded_init(path)

    root = tmp_path / path.stem
    database = root / "data/newsroom.db"
    Database(database).initialize("2026-10-06T00:00:00+00:00")
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute("UPDATE schema_version SET applied_at='wal-data' WHERE version=4")
        writer.commit()

        opened, checkpoint_calls, events = _track_connections(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["guarded-init", str(root)])
        namespace = {
            "assert_connection_closed": _assert_connection_closed,
            "opened": opened,
        }
        try:
            exec(compile(executable, str(path), "exec"), namespace)
        except SystemExit as error:
            assert "WAL/SHM sidecars remain" in str(error)
        assert len(checkpoint_calls) == 1
        assert events == ["validate", "checkpoint"]
        assert opened
    finally:
        writer.close()


@pytest.mark.parametrize("path", GUARDED_INIT_DOCUMENTS, ids=lambda path: path.name)
def test_guarded_init_documentation_rejects_future_wal_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: Path
):
    executable = _instrumented_guarded_init(path)
    root = tmp_path / f"future-{path.stem}"
    database = root / "data/newsroom.db"
    database.parent.mkdir(parents=True)
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        writer.execute("INSERT INTO schema_version VALUES (5, 'future')")
        writer.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        writer.execute("INSERT INTO sentinel VALUES ('preserve-me')")
        writer.commit()
        files = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
        assert all(item.is_file() for item in files)
        before = {item: item.read_bytes() for item in files}

        opened, checkpoint_calls, events = _track_connections(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["guarded-init", str(root)])
        namespace = {
            "assert_connection_closed": _assert_connection_closed,
            "opened": opened,
        }
        with pytest.raises(ValueError, match=r"schema version 5"):
            exec(compile(executable, str(path), "exec"), namespace)

        assert checkpoint_calls == []
        assert events == ["validate"]
        assert {item: item.read_bytes() for item in files} == before
        assert opened
        for connection in opened:
            _assert_connection_closed(connection)
    finally:
        writer.close()
