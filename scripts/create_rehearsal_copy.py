#!/usr/bin/env python3
"""Create a coherent, symlink-preserving rehearsal copy of a newsroom root."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

from newsroom.coordination import operation_lock
from newsroom.migration import assert_migration_access


RUNTIME_TREES = ("data", "workspace", "research", "articles", "images", "output")
DATABASE_FILES = {"newsroom.db", "newsroom.db-wal", "newsroom.db-shm"}


def create_rehearsal_copy(source_root: Path, target_root: Path) -> dict[str, object]:
    source = source_root.resolve()
    target = target_root.resolve()
    source_data = source / "data"
    source_database = source_data / "newsroom.db"

    with operation_lock(source, exclusive=True):
        # The exclusive flock coordinates live operations. Persistent migration
        # state is a separate fail-closed gate and must be checked under it.
        assert_migration_access(source)
        if source_data.is_symlink() or source_database.is_symlink():
            raise ValueError("source data directory and database must not be symlinks")
        if not source_database.is_file():
            raise ValueError(f"missing source database: {source_database}")
        if target == source or target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError("rehearsal target must be separate from the source root")
        if target.is_symlink():
            raise ValueError("rehearsal target must not be a symlink")
        if target.exists() and any(target.iterdir()):
            raise ValueError("rehearsal target must be absent or empty")
        target.mkdir(parents=True, exist_ok=True)

        source_uri = source_database.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_connection:
            journal_mode = str(
                source_connection.execute("PRAGMA journal_mode").fetchone()[0]
            )
            wal = Path(f"{source_database}-wal")
            shm = Path(f"{source_database}-shm")

            for name in RUNTIME_TREES:
                source_path = source / name
                target_path = target / name
                if not source_path.exists() and not source_path.is_symlink():
                    continue
                if source_path.is_symlink():
                    os.symlink(os.readlink(source_path), target_path)
                    continue

                def ignore_database(directory: str, names: list[str]) -> set[str]:
                    if Path(directory) == source_data:
                        return set(names) & DATABASE_FILES
                    return set()

                shutil.copytree(
                    source_path,
                    target_path,
                    symlinks=True,
                    ignore=ignore_database if name == "data" else None,
                )

            target_database = target / "data/newsroom.db"
            target_database.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(target_database) as target_connection:
                source_connection.backup(target_connection)
                integrity = str(
                    target_connection.execute("PRAGMA integrity_check").fetchone()[0]
                )
            if integrity != "ok":
                raise ValueError(
                    f"rehearsal database backup failed integrity check: {integrity}"
                )

        return {
            "source_root": str(source),
            "target_root": str(target),
            "journal_mode": journal_mode,
            "wal_exists": wal.exists(),
            "wal_size": wal.stat().st_size if wal.exists() else 0,
            "shm_exists": shm.exists(),
            "database_integrity": integrity,
        }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print("usage: create_rehearsal_copy.py SOURCE_ROOT TARGET_ROOT", file=sys.stderr)
        return 2
    try:
        report = create_rehearsal_copy(Path(arguments[0]), Path(arguments[1]))
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
