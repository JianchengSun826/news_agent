from __future__ import annotations

from contextlib import closing
import hashlib
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .batch_models import candidate_fingerprint


SUPPORTED_SCHEMA_VERSION = 4


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  discovered_at TEXT NOT NULL,
  event_date TEXT NOT NULL,
  candidate_title TEXT NOT NULL,
  core_fact TEXT NOT NULL,
  company TEXT NOT NULL,
  product TEXT NOT NULL,
  topic TEXT NOT NULL,
  subtopic TEXT NOT NULL,
  event_type TEXT NOT NULL,
  keywords TEXT NOT NULL,
  primary_source TEXT NOT NULL,
  secondary_sources TEXT NOT NULL,
  discovered_from TEXT NOT NULL,
  priority INTEGER NOT NULL,
  duplicate_status TEXT,
  matching_event_id TEXT,
  event_id TEXT,
  selected INTEGER NOT NULL DEFAULT 0,
  rejection_reason TEXT,
  candidate_fingerprint TEXT,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
  article_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(candidate_id),
  owning_batch_id TEXT REFERENCES batches(batch_id),
  event_id TEXT NOT NULL,
  status TEXT NOT NULL,
  publish_date TEXT,
  event_date TEXT NOT NULL,
  title_cn TEXT,
  slug TEXT,
  core_fact TEXT NOT NULL,
  topic TEXT NOT NULL,
  subtopic TEXT NOT NULL,
  company TEXT NOT NULL,
  product TEXT NOT NULL,
  event_type TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  keywords TEXT NOT NULL,
  primary_source TEXT NOT NULL,
  secondary_sources TEXT NOT NULL,
  article_url TEXT,
  draft_path TEXT,
  research_brief_path TEXT,
  fact_check_path TEXT,
  fact_check_outcome TEXT,
  revision_number INTEGER NOT NULL DEFAULT 1,
  image_id TEXT,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  published_at TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  source_name TEXT NOT NULL,
  domain TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL,
  reliability_level TEXT NOT NULL,
  usage_policy TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS images (
  image_id TEXT PRIMARY KEY,
  article_id TEXT NOT NULL REFERENCES articles(article_id),
  image_type TEXT NOT NULL,
  source_url TEXT,
  creator TEXT,
  license TEXT,
  license_url TEXT,
  attribution_text TEXT,
  ai_provider TEXT,
  ai_model TEXT,
  prompt_path TEXT,
  image_path TEXT,
  image_candidate_id TEXT,
  rights_basis TEXT,
  rights_statement TEXT,
  generated_at TEXT,
  downloaded_at TEXT,
  notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS state_transitions (
  transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY,
  source_filename TEXT NOT NULL,
  source_sha256 TEXT NOT NULL UNIQUE,
  workspace_version INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  content_request_id TEXT,
  fact_check_request_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_requests (
  request_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  request_type TEXT NOT NULL,
  revision_number INTEGER NOT NULL,
  request_path TEXT NOT NULL,
  expected_result_filename TEXT NOT NULL,
  result_path TEXT,
  result_sha256 TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS batch_items (
  batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  article_id TEXT REFERENCES articles(article_id),
  item_status TEXT NOT NULL,
  duplicate_classification TEXT,
  content_version INTEGER NOT NULL DEFAULT 1,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(batch_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS image_candidates (
  image_candidate_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  article_id TEXT NOT NULL REFERENCES articles(article_id),
  landing_page_url TEXT NOT NULL,
  direct_image_url TEXT,
  creator TEXT,
  rights_basis TEXT NOT NULL,
  license_name TEXT,
  rights_url TEXT NOT NULL,
  rights_statement TEXT NOT NULL,
  attribution_required INTEGER NOT NULL,
  attribution_text TEXT,
  relevance TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'PROPOSED',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_event ON candidates(company, product, event_type, event_date);
CREATE INDEX IF NOT EXISTS idx_transitions_entity ON state_transitions(entity_id, transition_id);
CREATE INDEX IF NOT EXISTS idx_batch_items_status ON batch_items(batch_id, item_status);
CREATE INDEX IF NOT EXISTS idx_batch_requests_batch ON batch_requests(batch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_image_candidates_article ON image_candidates(article_id, review_status);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _digest(path: Path) -> str:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    def _schema_snapshot_signature(self) -> dict[str, tuple[str, int, int]]:
        signature: dict[str, tuple[str, int, int]] = {}
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{self.path}{suffix}")
            if not source.exists():
                continue
            metadata = source.stat()
            signature[suffix] = (
                self._digest(source),
                metadata.st_size,
                metadata.st_mtime_ns,
            )
        return signature

    def _existing_versions(self) -> list[int]:
        if not self.path.exists():
            return []

        sources = {suffix: Path(f"{self.path}{suffix}") for suffix in ("", "-wal", "-shm")}
        before = self._schema_snapshot_signature()
        try:
            with tempfile.TemporaryDirectory(prefix="newsroom-schema-check-") as temporary:
                copied = Path(temporary) / "newsroom.db"
                for suffix, (digest, _, _) in before.items():
                    destination = Path(f"{copied}{suffix}")
                    shutil.copyfile(sources[suffix], destination)
                    if self._digest(destination) != digest:
                        raise ValueError("database changed while checking schema version; stop writers and retry")
                if self._schema_snapshot_signature() != before:
                    raise ValueError("database changed while checking schema version; stop writers and retry")
                with closing(sqlite3.connect(copied)) as connection:
                    table = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
                    ).fetchone()
                    if not table:
                        return []
                    rows = connection.execute(
                        "SELECT version FROM schema_version ORDER BY version"
                    ).fetchall()
        except sqlite3.Error as error:
            raise ValueError(f"cannot read database schema version safely: {error}") from error

        if not rows:
            raise ValueError(
                f"invalid database schema version table; this newsroom supports versions 1 through "
                f"{SUPPORTED_SCHEMA_VERSION}"
            )
        if any(type(row[0]) is not int for row in rows):
            raise ValueError(
                f"invalid database schema version value; this newsroom supports versions 1 through "
                f"{SUPPORTED_SCHEMA_VERSION}"
            )
        return [row[0] for row in rows]

    def _existing_version(self) -> int:
        versions = self._existing_versions()
        unsupported = next(
            (version for version in versions if version < 1 or version > SUPPORTED_SCHEMA_VERSION),
            None,
        )
        if unsupported is not None:
            raise ValueError(
                f"unsupported database schema version {unsupported}; this newsroom supports versions "
                f"1 through {SUPPORTED_SCHEMA_VERSION}. Upgrade the application before running init."
            )
        return max(versions, default=0)

    def _backup_before_upgrade(self, version: int, applied_at: str) -> None:
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = re.sub(r"[^0-9A-Za-z]+", "", applied_at)
        shutil.copy2(self.path, backup_dir / f"newsroom-v{version}-{stamp}.db")

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    @staticmethod
    def _backfill_candidate_fingerprints(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT candidate_id,event_date,company,product,event_type,core_fact,primary_source "
            "FROM candidates ORDER BY candidate_id"
        ).fetchall()
        fingerprints: dict[str, str] = {}
        for row in rows:
            value = candidate_fingerprint(dict(row))
            previous = fingerprints.get(value)
            if previous and previous != row["candidate_id"]:
                raise ValueError(
                    f"candidate fingerprint collision: {previous} and {row['candidate_id']}"
                )
            fingerprints[value] = row["candidate_id"]
            connection.execute(
                "UPDATE candidates SET candidate_fingerprint=? WHERE candidate_id=?",
                (value, row["candidate_id"]),
            )

    def initialize(self, applied_at: str) -> None:
        existing_version = self._existing_version()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if existing_version in {1, 2, 3}:
            self._backup_before_upgrade(existing_version, applied_at)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (1, ?)",
                (applied_at,),
            )
            image_columns = {row["name"] for row in connection.execute("PRAGMA table_info(images)")}
            if "image_path" not in image_columns:
                connection.execute("ALTER TABLE images ADD COLUMN image_path TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (2, ?)",
                (applied_at,),
            )
            self._add_column(connection, "candidates", "candidate_fingerprint TEXT")
            self._add_column(connection, "articles", "fact_check_outcome TEXT")
            self._add_column(connection, "articles", "revision_number INTEGER NOT NULL DEFAULT 1")
            self._add_column(connection, "images", "image_candidate_id TEXT")
            self._add_column(connection, "images", "rights_basis TEXT")
            self._add_column(connection, "images", "rights_statement TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_fingerprint ON candidates(candidate_fingerprint)"
            )
            self._backfill_candidate_fingerprints(connection)
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (3, ?)",
                (applied_at,),
            )

            self._add_column(connection, "articles", "owning_batch_id TEXT REFERENCES batches(batch_id)")
            self._add_column(connection, "batches", "workspace_version INTEGER NOT NULL DEFAULT 0")
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (4, ?)",
                (applied_at,),
            )

    def table_names(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row["name"] for row in rows}

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]
