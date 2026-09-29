from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable


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
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
  article_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(candidate_id),
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
CREATE INDEX IF NOT EXISTS idx_candidates_event ON candidates(company, product, event_type, event_date);
CREATE INDEX IF NOT EXISTS idx_transitions_entity ON state_transitions(entity_id, transition_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self, applied_at: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (1, ?)",
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
