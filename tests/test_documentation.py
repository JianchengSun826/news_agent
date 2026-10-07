from __future__ import annotations

from contextlib import closing
import re
import sqlite3
import sys
from pathlib import Path

import pytest


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


@pytest.mark.parametrize("path", GUARDED_INIT_DOCUMENTS, ids=lambda path: path.name)
def test_guarded_init_documentation_closes_wal_connection_before_sidecar_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: Path
):
    source = _guarded_init_block(path)
    assert "from contextlib import closing" in source
    assert re.search(r"with closing\(\s*sqlite3\.connect\(", source)
    close_check = "    assert_connection_closed(opened[-1])\n"
    sidecar_check = "    if (wal.exists() and wal.stat().st_size != 0) or shm.exists():"
    assert sidecar_check in source
    executable = source.replace(sidecar_check, close_check + sidecar_check)
    executable = executable.replace("    Newsroom(root).init()", "    pass")

    root = tmp_path / path.stem
    database = root / "data/newsroom.db"
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('wal-data')")
        connection.commit()

    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    def assert_connection_closed(connection: sqlite3.Connection) -> None:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(sys, "argv", ["guarded-init", str(root)])
    namespace = {
        "assert_connection_closed": assert_connection_closed,
        "opened": opened,
    }
    try:
        exec(compile(executable, str(path), "exec"), namespace)
    except SystemExit as error:
        assert "WAL/SHM sidecars remain" in str(error)
    assert opened
