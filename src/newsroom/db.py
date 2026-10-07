from __future__ import annotations

from contextlib import closing
from functools import lru_cache
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


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalized_default(value: str | None) -> str | None:
    if value is None:
        return None
    result = " ".join(value.strip().split())
    while result.startswith("(") and result.endswith(")"):
        candidate = result[1:-1].strip()
        if candidate.count("(") != candidate.count(")"):
            break
        result = candidate
    return result


def _column_contract(connection: sqlite3.Connection, table: str) -> dict[str, tuple[str, int, str | None, int]]:
    return {
        row[1]: (
            " ".join(str(row[2]).upper().split()),
            int(row[3]),
            _normalized_default(row[4]),
            int(row[5]),
        )
        for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    }


def _foreign_key_contract(connection: sqlite3.Connection, table: str) -> set[tuple[str, str, str, str, str, str]]:
    return {
        (str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6]), str(row[7]))
        for row in connection.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})")
    }


def _index_contract(
    connection: sqlite3.Connection, table: str
) -> tuple[dict[str, tuple[int, int, tuple[tuple[str | None, int, str], ...]]], list[tuple[int, str, int, tuple[tuple[str | None, int, str], ...]]]]:
    explicit: dict[str, tuple[int, int, tuple[tuple[str | None, int, str], ...]]] = {}
    constraints: list[tuple[int, str, int, tuple[tuple[str | None, int, str], ...]]] = []
    for row in connection.execute(f"PRAGMA index_list({_quote_identifier(table)})"):
        name, unique, origin, partial = str(row[1]), int(row[2]), str(row[3]), int(row[4])
        columns = tuple(
            (detail[2], int(detail[3]), str(detail[4]).upper())
            for detail in connection.execute(f"PRAGMA index_xinfo({_quote_identifier(name)})")
            if int(detail[5]) == 1
        )
        value = (unique, partial, columns)
        if origin == "c":
            explicit[name] = value
        else:
            constraints.append((unique, origin, partial, columns))
    return explicit, sorted(constraints, key=repr)


def _semantic_schema(connection: sqlite3.Connection) -> dict[str, object]:
    objects = {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT type,name,tbl_name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index','view','trigger')"
        )
    }
    tables = {name for object_type, name, _ in objects if object_type == "table"}
    autoincrement_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND sql IS NOT NULL AND sql LIKE '%AUTOINCREMENT%'"
        )
    }
    table_flags = {
        str(row[1]): (
            int(row[4]),
            int(row[5]),
            str(row[1]) in autoincrement_tables,
        )
        for row in connection.execute("PRAGMA table_list")
        if row[2] == "table" and not str(row[1]).startswith("sqlite_")
    }
    return {
        "objects": objects,
        "tables": tables,
        "table_flags": table_flags,
        "columns": {table: _column_contract(connection, table) for table in tables},
        "foreign_keys": {table: _foreign_key_contract(connection, table) for table in tables},
        "indexes": {table: _index_contract(connection, table) for table in tables},
    }


@lru_cache(maxsize=1)
def _v4_schema_contract() -> dict[str, object]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "CREATE INDEX idx_candidates_fingerprint ON candidates(candidate_fingerprint)"
        )
        return _semantic_schema(connection)


def _validate_v4_schema(connection: sqlite3.Connection, versions: list[int]) -> None:
    if versions != list(range(1, SUPPORTED_SCHEMA_VERSION + 1)):
        raise ValueError(
            "incompatible schema v4 version history; expected versions 1, 2, 3, and 4"
        )
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise ValueError(f"incompatible schema v4 integrity check failed: {integrity}")

    expected = _v4_schema_contract()
    actual = _semantic_schema(connection)
    expected_tables = expected["tables"]
    actual_tables = actual["tables"]
    missing_tables = sorted(expected_tables - actual_tables)
    if missing_tables:
        raise ValueError(
            f"incompatible schema v4 missing table(s): {', '.join(missing_tables)}"
        )
    extra_tables = sorted(actual_tables - expected_tables)
    if extra_tables:
        raise ValueError(
            f"incompatible schema v4 unexpected schema object(s): {', '.join(extra_tables)}"
        )

    expected_objects = expected["objects"]
    actual_objects = actual["objects"]
    unexpected_objects = sorted(actual_objects - expected_objects)
    if unexpected_objects:
        names = ", ".join(f"{kind}:{name}" for kind, name, _ in unexpected_objects)
        raise ValueError(f"incompatible schema v4 unexpected schema object(s): {names}")
    missing_objects = sorted(expected_objects - actual_objects)
    if missing_objects:
        names = ", ".join(f"{kind}:{name}" for kind, name, _ in missing_objects)
        raise ValueError(f"incompatible schema v4 index mismatch or missing object(s): {names}")

    for table in sorted(expected_tables):
        if actual["table_flags"][table] != expected["table_flags"][table]:
            raise ValueError(f"incompatible schema v4 table option mismatch: {table}")
        if actual["columns"][table] != expected["columns"][table]:
            raise ValueError(f"incompatible schema v4 column mismatch: {table}")
        if actual["foreign_keys"][table] != expected["foreign_keys"][table]:
            raise ValueError(f"incompatible schema v4 foreign key mismatch: {table}")
        if actual["indexes"][table] != expected["indexes"][table]:
            raise ValueError(f"incompatible schema v4 index mismatch: {table}")


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
                    user_tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    if "schema_version" not in user_tables:
                        if user_tables:
                            raise ValueError(
                                "existing database contains user tables but no schema_version; "
                                "restore a supported v1-v4 database or use a new empty database"
                            )
                        return []
                    rows = connection.execute(
                        "SELECT version FROM schema_version ORDER BY version"
                    ).fetchall()
                    snapshot_versions = [row[0] for row in rows]
                    if (
                        snapshot_versions
                        and all(type(version) is int for version in snapshot_versions)
                        and max(snapshot_versions) == SUPPORTED_SCHEMA_VERSION
                    ):
                        _validate_v4_schema(connection, snapshot_versions)
        except sqlite3.Error as error:
            raise ValueError(
                f"invalid or unreadable database schema version; this newsroom supports versions "
                f"1 through {SUPPORTED_SCHEMA_VERSION}: {error}"
            ) from error

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

    def validate_existing_schema(self) -> int:
        """Return schema version 0-4 after a read-only snapshot validation."""
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

    def _require_clear_sidecars(self) -> None:
        wal = Path(f"{self.path}-wal")
        shm = Path(f"{self.path}-shm")
        if (wal.exists() and wal.stat().st_size != 0) or shm.exists():
            raise ValueError(
                "WAL/SHM sidecars remain after checkpoint and close; stop writers and retry "
                "without deleting sidecars"
            )

    def _prepare_existing_database(self, version: int) -> None:
        if version == 0:
            self._require_clear_sidecars()
            return
        uri = self.path.resolve().as_uri() + "?mode=rw"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(journal_mode).lower() == "wal":
                busy, log_frames, checkpointed = connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if busy != 0 or log_frames != checkpointed:
                    raise ValueError(
                        "WAL checkpoint was busy or incomplete; stop external writers and retry"
                    )
        self._require_clear_sidecars()
        if self.validate_existing_schema() != version:
            raise ValueError("database schema changed during initialization; stop writers and retry")

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
        existing_version = self.validate_existing_schema()
        self._prepare_existing_database(existing_version)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if existing_version in {1, 2, 3}:
            self._backup_before_upgrade(existing_version, applied_at)
        with closing(self.connect()) as connection:
            with connection:
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
