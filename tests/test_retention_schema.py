from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from newsroom.db import Database
from test_batch import newsroom


def test_new_database_has_empty_retention_table(newsroom):
    assert newsroom.db.validate_existing_schema() == 5
    assert newsroom.db.all('SELECT * FROM batch_retention') == []


def test_v4_upgrade_keeps_rows_and_creates_verified_original_backup(newsroom):
    database = newsroom.db
    with database.connect() as connection:
        connection.execute('DROP TABLE IF EXISTS batch_retention')
        connection.execute('DELETE FROM schema_version WHERE version>4')
        before = connection.execute('SELECT * FROM sources ORDER BY source_id').fetchall()
    assert database.validate_existing_schema() == 4
    database.initialize('2026-10-12T12:00:00+00:00')
    assert database.validate_existing_schema() == 5
    with database.connect() as connection:
        assert connection.execute('SELECT * FROM sources ORDER BY source_id').fetchall() == before
    backups = list((database.path.parent / 'backups').glob('newsroom-v4-*.db'))
    assert len(backups) == 1
    assert Database(backups[0]).validate_existing_schema() == 4
    database.initialize('2026-10-12T13:00:00+00:00')
    assert len(list((database.path.parent / 'backups').glob('newsroom-v4-*.db'))) == 1


def test_retention_state_and_batch_identity_are_constrained(newsroom):
    with newsroom.db.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO batch_retention VALUES ('absent','FINAL_ONLY','workspace/completed/absent','{}','editor','checked','now','now')")
