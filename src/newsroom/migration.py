"""Verified local migration planning, execution and compensating recovery."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from typing import Any, Iterator

from pydantic import TypeAdapter, ValidationError
import unicodedata
from uuid import uuid4

from .service import Newsroom, WorkflowError
from .coordination import operation_lock
from .workspace import (
    BATCH_ACTIONS, BatchWorkspace, BatchWorkspacePaths, _atomic_write,
    render_action_script, render_migration_script,
)


def assert_migration_access(root: Path | str) -> None:
    """Block normal CLI/Finder access before constructing or using a newsroom."""
    barrier = Path(root).resolve() / 'data/workspace-migration-lock.json'
    if barrier.exists() or barrier.is_symlink():
        raise WorkflowError(
            'migration access barrier active; recovery required: inspect '
            'data/workspace-migration-journals and the verified backup before using normal commands'
        )


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _durable_json(path: Path, value: Any) -> None:
    _atomic_write(path, _json_bytes(value).decode())
    with path.open('rb') as handle:
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _key(path: Path | str) -> str:
    return unicodedata.normalize("NFC", Path(path).as_posix()).casefold()


def _safe_relative(path: Path) -> None:
    if path.is_absolute() or not path.parts or any(part in {"..", ""} for part in path.parts):
        raise WorkflowError("unsafe repository path (must be relative without parent traversal)")


def _no_symlinks(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise WorkflowError("unsafe symlink source or destination")


def _walk_error(error: OSError) -> None:
    raise error


def _database(connection: sqlite3.Connection) -> tuple[dict[str, list[dict[str, Any]]], str]:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise WorkflowError("database integrity check failed")
    schema = [tuple(row) for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
    tables = {}
    for name, _ in schema:
        quoted = '"' + name.replace('"', '""') + '"'
        rows = [dict(row) for row in connection.execute(f"SELECT * FROM {quoted}")]
        tables[name] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=lambda v: v.hex()))
    payload = {"schema": schema, "tables": tables}
    return tables, hashlib.sha256(json.dumps(payload, sort_keys=True, default=lambda v: v.hex()).encode()).hexdigest()


@dataclass(frozen=True)
class MigrationEntry:
    source: Path
    target: Path
    sha256: str
    size: int
    batch_id: str | None = None
    article_id: str | None = None
    database_table: str | None = None
    database_field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "source": self.source.as_posix(), "target": self.target.as_posix()}


@dataclass
class MigrationPlan:
    claimed: list[MigrationEntry] = field(default_factory=list)
    legacy: list[MigrationEntry] = field(default_factory=list)
    unclaimed: list[MigrationEntry] = field(default_factory=list)
    source_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocking_errors: list[str] = field(default_factory=list)
    database_snapshot_sha256: str = ""
    database_size: int = 0
    selected_source_csvs: list[Path] = field(default_factory=list)

    @property
    def entries(self) -> list[MigrationEntry]:
        return sorted(self.claimed + self.legacy + self.unclaimed, key=lambda e: (e.source.as_posix(), e.target.as_posix(), e.database_table or "", e.database_field or ""))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MigrationPlan:
        """Validate a saved, deterministic preflight report at the input boundary."""
        try:
            plan = TypeAdapter(cls).validate_python(value)
            if plan.to_dict() != value:
                raise ValueError("noncanonical migration plan")
            return plan
        except (ValidationError, TypeError, ValueError) as error:
            raise WorkflowError(f"invalid migration plan: {error}") from error

    @property
    def expected_backup_size(self) -> int:
        return self.database_size + sum({e.source: e.size for e in self.entries}.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            **{group: [e.to_dict() for e in sorted(getattr(self, group), key=lambda e: (e.source.as_posix(), e.target.as_posix(), e.database_table or "", e.database_field or ""))] for group in ("claimed", "legacy", "unclaimed")},
            "source_metadata": dict(sorted(self.source_metadata.items())),
            "blocking_errors": sorted(set(self.blocking_errors)),
            "database_snapshot_sha256": self.database_snapshot_sha256,
            "database_size": self.database_size,
            "selected_source_csvs": sorted(p.as_posix() for p in self.selected_source_csvs),
            "expected_backup_size": self.expected_backup_size,
        }


class WorkspaceMigrator:
    def __init__(self, room: Newsroom):
        self.room = room
        self.workspace = BatchWorkspace(room)

    @contextmanager
    def _connect_readonly(self, path: Path | None = None) -> Iterator[sqlite3.Connection]:
        path = path or self.room.db_path
        sources = {suffix: Path(str(path) + suffix) for suffix in ("", "-wal", "-shm")}

        def signature() -> dict[str, tuple[tuple[str, int], int, int]]:
            files = {}
            for suffix, source in sources.items():
                _no_symlinks(source)
                if suffix and not source.exists():
                    continue
                metadata = source.stat()
                files[suffix] = (_digest(source), metadata.st_mode, metadata.st_mtime_ns)
            return files

        # Even a WAL-mode main DB with no sidecars can create canonical WAL/SHM
        # on a mode=ro open. Every SQLite open uses a stable disposable copy.
        before = signature()
        with tempfile.TemporaryDirectory(prefix="newsroom-readonly-database-") as temporary:
            copied = Path(temporary) / "newsroom.db"
            for suffix, (digest, _, _) in before.items():
                destination = Path(str(copied) + suffix)
                shutil.copyfile(sources[suffix], destination)
                if _digest(destination) != digest:
                    raise WorkflowError("database changed while copying snapshot")
            if signature() != before:
                raise WorkflowError("database changed while reading snapshot")
            with closing(sqlite3.connect(copied)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                yield connection

    def _source(self, source: Path, external: bool = False) -> Path:
        if not external:
            _safe_relative(source)
        path = source if source.is_absolute() else self.room.root / source
        _no_symlinks(path)
        if not external and not path.resolve().is_relative_to(self.room.root):
            raise WorkflowError("unsafe source path escape")
        if not path.is_file():
            raise WorkflowError(f"missing referenced file: {source.as_posix()}")
        return path

    def _validate_target_ancestors(self, target: Path) -> None:
        _safe_relative(target)
        for ancestor in target.parents:
            path = self.room.root / ancestor
            if path.is_symlink():
                raise WorkflowError(f"unsafe target ancestor symlink: {ancestor.as_posix()}")
            if path.exists() and not path.is_dir():
                raise WorkflowError(f"target ancestor is not a directory: {ancestor.as_posix()}")
        _no_symlinks(self.room.root / target)

    def preflight(self, source_csvs: list[Path] | None = None) -> MigrationPlan:
        """Inventory exact bytes without creating directories, updating rows, or moving files."""
        plan = MigrationPlan()
        try:
            with self._connect_readonly() as connection:
                connection.execute("BEGIN")
                tables, plan.database_snapshot_sha256 = _database(connection)
                plan.database_size = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
                if not {"articles", "batches", "batch_items", "batch_requests", "images"} <= tables.keys():
                    raise WorkflowError("database must be initialized to schema version 4")
                versions = [row["version"] for row in tables.get("schema_version", [])]
                if not versions or max(versions) != 4:
                    raise WorkflowError("database must be initialized to schema version 4")
        except (OSError, sqlite3.Error, WorkflowError) as error:
            message = str(error).replace(self.room.root.as_posix() + "/", "")
            plan.blocking_errors.append(f"unreadable database: {message}")
            return plan
        articles = {a["article_id"]: a for a in tables["articles"]}
        batches = {b["batch_id"]: b for b in tables["batches"]}
        inventory_roots = ("research", "articles", "images", "output", "workspace")
        expected_roots = {Path(root) for root in (*inventory_roots, "output/batches", "output/prompts", "workspace/batches", "workspace/archive", "workspace/archive/legacy-single-articles", "workspace/archive/unclaimed-files")}
        for article_id, article in articles.items():
            expected_roots.update(Path(root) / article_id for root in ("research", "articles", "images", "output", "workspace/archive/legacy-single-articles"))
            if article["owning_batch_id"]:
                root = Path("workspace/batches") / article["owning_batch_id"]
                expected_roots.update((root / "articles" / article_id, root / "publish" / article_id))
        for batch_id in batches:
            root = Path("workspace/batches") / batch_id
            expected_roots.add(Path("output/batches") / batch_id)
            expected_roots.update(root / suffix for suffix in (".", "input", "articles", "publish", "actions", "claude", "claude/requests", "claude/inbox", "claude/archive"))
        for root in sorted(expected_roots):
            try:
                _safe_relative(root)
                absolute = self.room.root / root
                _no_symlinks(absolute)
                if absolute.exists() and not absolute.is_dir():
                    raise WorkflowError("expected a directory")
            except (OSError, WorkflowError) as error:
                display = "<invalid absolute root>" if root.is_absolute() else root.as_posix()
                plan.blocking_errors.append(f"inventory root {display}: {error}")
        for article_id, article in articles.items():
            memberships = {i["batch_id"] for i in tables["batch_items"] if i["article_id"] == article_id and i["item_status"] not in {"REUSED_COMPLETE", "EXCLUDED"}}
            owner = article["owning_batch_id"]
            if len(memberships) > 1 or (owner and (owner not in batches or memberships - {owner})):
                plan.blocking_errors.append(f"ambiguous ownership: {article_id}")
        seen: set[Path] = set()
        database_sources: set[Path] = set()

        def add(source: Path, target: Path, batch: str | None = None, article: str | None = None, table: str | None = None, db_field: str | None = None, group: str = "claimed", external: bool = False) -> MigrationEntry | None:
            try:
                path = self._source(source, external)
                self._validate_target_ancestors(target)
                sha, size = _digest(path)
                entry = MigrationEntry(source, target, sha, size, batch, article, table, db_field)
                getattr(plan, group).append(entry)
                seen.add(source)
                return entry
            except (OSError, WorkflowError) as error:
                plan.blocking_errors.append(f"{table or 'inventory'}.{db_field or 'file'} {source.as_posix()}: {error}")

        for table, rows in sorted(tables.items()):
            for row in rows:
                for db_field, value in sorted(row.items()):
                    if not db_field.endswith("_path") or not value:
                        continue
                    source = Path(value)
                    database_sources.add(source)
                    article_id = row.get("article_id")
                    article = articles.get(article_id)
                    batch = article["owning_batch_id"] if article else row.get("batch_id")
                    try:
                        target = self._target(source, article, batch, db_field, row if table == "batch_requests" else None)
                        add(source, target, batch, article_id, table, db_field, "legacy" if article and batch is None else "claimed")
                    except WorkflowError as error:
                        plan.blocking_errors.append(f"{table}.{db_field}: {error}")

        # All output subtrees include packages and prompts, including unknown IDs.
        for root in inventory_roots:
            absolute = self.room.root / root
            if not absolute.exists() and not absolute.is_symlink():
                continue
            try:
                _no_symlinks(absolute)
                for directory, dirs, files in os.walk(absolute, followlinks=False, onerror=_walk_error):
                    for name in sorted(dirs + files):
                        path = Path(directory) / name
                        if path.is_symlink():
                            plan.blocking_errors.append(f"unsafe symlink: {path.relative_to(self.room.root).as_posix()}")
                    for name in sorted(files):
                        source = (Path(directory) / name).relative_to(self.room.root)
                        if source in seen:
                            continue
                        try:
                            article_id, batch = self._infer_owner(source, articles, batches)
                            article = articles.get(article_id)
                            target = self._target(source, article, batch)
                            group = "legacy" if article and batch is None else "claimed" if article or batch else "unclaimed"
                            if group == "unclaimed" and source.parts[0] != "workspace":
                                target = Path("workspace/archive/unclaimed-files") / source
                            add(source, target, batch, article_id, group=group)
                        except WorkflowError as error:
                            plan.blocking_errors.append(f"inventory {source.as_posix()}: {error}")
            except (OSError, WorkflowError) as error:
                plan.blocking_errors.append(f"inventory {root}: {error}")

        selected_hashes: set[str] = set()
        for value in source_csvs or []:
            absolute = Path(os.path.abspath(value))
            source = absolute.relative_to(self.room.root) if absolute.is_relative_to(self.room.root) else absolute
            plan.selected_source_csvs.append(source)
            try:
                _no_symlinks(Path(value).absolute())
                sha, _ = _digest(self._source(source, external=True))
                if sha in selected_hashes:
                    raise WorkflowError("multiple selected CSVs for one recorded SHA-256")
                selected_hashes.add(sha)
                matching = [b for b in batches.values() if b["source_sha256"] == sha]
                if len(matching) != 1:
                    raise WorkflowError("selected CSV does not match any recorded batch SHA-256")
                batch = matching[0]["batch_id"]
                target = self.workspace.source_csv_path(batch).relative_to(self.room.root)
                existing = [entry for entry in plan.entries if entry.source == source]
                if source in database_sources:
                    raise WorkflowError("selected CSV source is already claimed by a database artifact")
                if len(existing) == 1 and existing[0].target == target and existing[0].article_id is None:
                    continue  # Already inventoried canonical supporting CSV.
                if existing and (len(existing) != 1 or existing[0] not in plan.unclaimed):
                    raise WorkflowError("selected CSV source is already claimed or has ambiguous destinations")
                promoted = add(source, target, batch, external=True)
                if promoted is not None and existing:
                    plan.unclaimed.remove(existing[0])
            except (OSError, WorkflowError) as error:
                plan.blocking_errors.append(f"selected CSV {source.as_posix()}: {error}")
        for batch_id, batch in sorted(batches.items()):
            target = self.workspace.source_csv_path(batch_id).relative_to(self.room.root)
            entries = [e for e in plan.entries if e.target == target]
            available = any(e.sha256 == batch["source_sha256"] for e in entries)
            if any(e.sha256 != batch["source_sha256"] for e in entries):
                plan.blocking_errors.append(f"source CSV hash mismatch: {target.as_posix()}")
            plan.source_metadata[batch_id] = {"source_filename": Path(batch["source_filename"]).name, "source_sha256": batch["source_sha256"], "available": available}
        self._collisions(plan)
        plan.blocking_errors = sorted({error.replace(self.room.root.as_posix() + "/", "") for error in plan.blocking_errors})
        return plan

    def _infer_owner(self, source: Path, articles: dict, batches: dict) -> tuple[str | None, str | None]:
        parts = source.parts
        if parts[:2] == ("workspace", "batches") and len(parts) > 2:
            batch = parts[2] if parts[2] in batches else None
            article = parts[4] if len(parts) > 4 and parts[3] in {"articles", "publish"} and parts[4] in articles else None
            return article, articles[article]["owning_batch_id"] if article else batch
        article = next((a for a in articles if a in parts or source.stem == a or source.name.startswith(a + "-")), None)
        batch = next((b for b in batches if b in parts), None)
        return article, articles[article]["owning_batch_id"] if article else batch

    def _target(self, source: Path, article: dict | None, batch: str | None, db_field: str | None = None, request: dict | None = None) -> Path:
        _safe_relative(source)
        if source.parts[:2] == ("workspace", "batches"):
            if len(source.parts) < 3 or (batch and source.parts[2] != batch):
                raise WorkflowError(f"ambiguous ownership of canonical source: {source.as_posix()}")
            if article and db_field:
                expected_root = self.workspace.article_root(batch, article["article_id"]).relative_to(self.room.root) if batch else Path("workspace/batches") / source.parts[2] / "articles" / article["article_id"]
                if not source.is_relative_to(expected_root):
                    raise WorkflowError(f"canonical article path does not match database article: {source.as_posix()}")
                relative = source.relative_to(expected_root)
                kind = {"draft_path": "article", "research_brief_path": "research-brief", "fact_check_path": "fact-check"}.get(db_field)
                if kind:
                    current = article["revision_number"]
                    match = re.fullmatch(kind + r"-v([1-9][0-9]*)\.md", relative.as_posix())
                    version = int(match[1]) if match else 0
                    prior_check = db_field == "fact_check_path" and article["fact_check_outcome"] is None and 1 <= version <= current
                    if not match or (version != current and not prior_check):
                        raise WorkflowError(f"canonical filename or revision does not match {db_field}: {source.as_posix()}")
                elif db_field == "image_path" and relative.as_posix() != "hero" + source.suffix.lower():
                    raise WorkflowError(f"canonical filename does not match image_path: {source.as_posix()}")
            if not article or batch:
                return source
        if request:
            return self.workspace.request_artifact_path(batch, request["request_type"], request["revision_number"], result=db_field == "result_path", historical_request_id=request["request_id"] if request["status"] == "SUPERSEDED" and db_field == "request_path" else None).relative_to(self.room.root)
        if article:
            article_id = article["article_id"]
            root = self.workspace.article_root(batch, article_id).relative_to(self.room.root) if batch else Path("workspace/archive/legacy-single-articles") / article_id
            if source.parts[:1] == ("output",) and len(source.parts) > 1 and source.parts[1] == article_id:
                package_root = BatchWorkspacePaths(self.workspace.batch_root(batch)).publish.relative_to(self.room.root) / article_id if batch else root / "publish"
                return package_root.joinpath(*source.parts[2:])
            version_match = re.search(r"-v(\d+)\.md$", source.name, re.IGNORECASE)
            version = int(version_match[1]) if version_match else article["revision_number"] if db_field else 1
            kind = {"draft_path": "article", "research_brief_path": "research-brief", "fact_check_path": "fact-check"}.get(db_field)
            if kind is None:
                kind = next((k for k in ("article", "research-brief", "fact-check") if re.fullmatch(k + r"(?:-v\d+)?\.md", source.name, re.IGNORECASE)), None)
            if kind:
                return root / f"{kind}-v{version}.md"
            if db_field == "image_path":
                return root / ("hero" + source.suffix.lower())
            if source.parts[0] == "research" and source.stem == article_id:
                return root / "research-template.md"
            if source.parts[:2] == ("output", "prompts") or db_field == "prompt_path":
                return root / "prompts" / source.name
            if article_id in source.parts:
                return root.joinpath(*source.parts[source.parts.index(article_id) + 1:])
            return root / source.name
        if batch:
            root = self.workspace.batch_root(batch).relative_to(self.room.root)
            if batch in source.parts:
                return root / "claude" / "archive" / "legacy" / Path(*source.parts[source.parts.index(batch) + 1:])
        return source

    def _collisions(self, plan: MigrationPlan) -> None:
        targets: dict[str, MigrationEntry] = {}
        sources: dict[Path, MigrationEntry] = {}
        for entry in plan.entries:
            key = _key(entry.target)
            previous = targets.get(key)
            if previous and previous.source != entry.source:
                plan.blocking_errors.append(f"target collision: {previous.source.as_posix()} and {entry.source.as_posix()} -> {entry.target.as_posix()}")
            if previous and (previous.batch_id, previous.article_id) != (entry.batch_id, entry.article_id):
                plan.blocking_errors.append(f"ambiguous ownership: {entry.source.as_posix()}")
            prior_source = sources.get(entry.source)
            if prior_source and prior_source.target != entry.target:
                plan.blocking_errors.append(f"conflicting destinations: {entry.source.as_posix()}")
            targets[key] = entry
            sources[entry.source] = entry
        for entry in targets.values():
            if any(_key(parent) in targets for parent in entry.target.parents):
                plan.blocking_errors.append(f"target collision with file ancestor: {entry.target.as_posix()}")
            destination = self.room.root / entry.target
            if destination.exists() and entry.target not in sources:
                plan.blocking_errors.append(f"target collision with existing destination: {entry.target.as_posix()}")

    def save_report(self, plan: MigrationPlan) -> Path:
        """Save only a derived review report outside canonical artifacts."""
        path = self.room.root / "data" / "workspace-migration-preflight.json"
        _no_symlinks(path)
        _atomic_write(path, _json_bytes(plan.to_dict()).decode("utf-8"))
        return path

    @staticmethod
    def _archive_name(entry: MigrationEntry) -> str:
        return f"external-inputs/{entry.sha256}/{entry.source.name}" if entry.source.is_absolute() else "repository/" + entry.source.as_posix()

    def backup(self, plan: MigrationPlan) -> Path:
        """Back up a still-clean plan; never execute or stage the migration."""
        if plan.blocking_errors:
            raise WorkflowError("migration preflight has blocking errors")
        selected = [self.room.root / p if not p.is_absolute() else p for p in plan.selected_source_csvs]
        if self.preflight(selected).to_dict() != plan.to_dict():
            raise WorkflowError("migration sources or database changed after preflight")
        parent = self.room.root / "data" / "backups"
        _no_symlinks(parent)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup: Path | None = None
        marker_created = False
        incomplete_text = "Backup incomplete; do not use for migration.\n"
        try:
            parent.mkdir(parents=True, exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix=f"workspace-migration-{stamp}-", dir=parent))
            (backup / "INCOMPLETE").write_text(incomplete_text, encoding="utf-8")
            marker_created = True
            with self._connect_readonly() as source, closing(sqlite3.connect(backup / "newsroom.db")) as destination:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
            unique = {self._archive_name(e): e for e in plan.entries}
            manifest = {"schema_version": 1, "plan": plan.to_dict(), "database": {}, "report": {}, "artifacts": []}
            db_sha, db_size = _digest(backup / "newsroom.db")
            manifest["database"] = {"sha256": db_sha, "size": db_size}
            report = _json_bytes(plan.to_dict())
            (backup / "report.json").write_bytes(report)
            manifest["report"] = {"sha256": hashlib.sha256(report).hexdigest(), "size": len(report)}
            manifest["artifacts"] = [{"archive_path": name, "source": e.source.as_posix(), "sha256": e.sha256, "size": e.size} for name, e in sorted(unique.items())]
            manifest_bytes = _json_bytes(manifest)
            (backup / "manifest.json").write_bytes(manifest_bytes)
            with (backup / "artifacts.tar.gz").open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed, tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, entry in sorted(unique.items()):
                    path = self._source(entry.source, external=entry.source.is_absolute())
                    with path.open("rb") as handle:
                        info = tarfile.TarInfo(name)
                        info.size = entry.size
                        info.mode = stat.S_IMODE(path.stat().st_mode)
                        archive.addfile(info, handle)
                for name, content in (("manifest.json", manifest_bytes), ("report.json", report)):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mode = 0o644
                    archive.addfile(info, io.BytesIO(content))
            self._verify_backup_contents(backup)
            if self.preflight(selected).to_dict() != plan.to_dict():
                raise WorkflowError("migration sources or database changed during backup")
            (backup / "INCOMPLETE").unlink()
            return backup
        except BaseException as error:
            if backup is None:
                raise
            failures = [error, *self._cleanup_failed_backup(backup, marker_created, incomplete_text)]
            if len(failures) > 1:
                raise BaseExceptionGroup("backup failed; cleanup also failed", failures) from None
            if isinstance(error, WorkflowError) or not isinstance(error, Exception):
                raise
            raise WorkflowError(f"backup verification failed: {error}") from error

    def _cleanup_failed_backup(self, backup: Path, marker_created: bool, incomplete_text: str) -> list[BaseException]:
        """Keep the original marker until every recoverable child is gone."""
        failures: list[BaseException] = []
        marker = backup / "INCOMPLETE"
        if not marker_created:
            # No backup content can precede the initial marker. Remove only its
            # possible partial file and the empty directory, independently.
            try:
                marker.unlink(missing_ok=True)
            except BaseException as error:
                failures.append(error)
            try:
                backup.rmdir()
            except BaseException as error:
                failures.append(error)
            return failures
        try:
            if not marker.is_file():
                _no_symlinks(marker)
                marker.write_text(incomplete_text, encoding="utf-8")
        except BaseException as error:
            failures.append(error)
        try:
            children = sorted(backup.iterdir())
        except BaseException as error:
            return [*failures, error]
        for child in children:
            if child == marker:
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except BaseException as error:
                failures.append(error)
        try:
            remaining = [child for child in backup.iterdir() if child != marker]
        except BaseException as error:
            return [*failures, error]
        if remaining:
            return failures or [WorkflowError("backup cleanup left recoverable contents")]
        # There is no recoverable content now. If rmdir fails after marker
        # removal, the leftover empty directory cannot appear as a valid backup.
        try:
            marker.unlink(missing_ok=True)
        except BaseException as error:
            return [*failures, error]
        try:
            backup.rmdir()
        except BaseException as error:
            failures.append(error)
        return failures

    def verify_backup(self, backup: Path) -> None:
        """Accept only a completed backup, then independently verify its contents."""
        marker = backup / "INCOMPLETE"
        if marker.exists() or marker.is_symlink():
            raise WorkflowError("backup verification failed: incomplete backup")
        self._verify_backup_contents(backup)

    def _verify_backup_contents(self, backup: Path) -> None:
        """Check database, manifest, safe member inventory and extracted exact bytes."""
        try:
            for name in ("newsroom.db", "manifest.json", "report.json", "artifacts.tar.gz"):
                _no_symlinks(backup / name)
            manifest_bytes = (backup / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            plan = manifest["plan"]
            if manifest["schema_version"] != 1 or plan["schema_version"] != 1 or plan["blocking_errors"]:
                raise WorkflowError("invalid manifest plan")
            for name, record in (("newsroom.db", manifest["database"]), ("report.json", manifest["report"])):
                if _digest(backup / name) != (record["sha256"], record["size"]):
                    raise WorkflowError(f"hash mismatch: {name}")
            if (backup / "report.json").read_bytes() != _json_bytes(plan):
                raise WorkflowError("report does not match manifest plan")
            with self._connect_readonly(backup / "newsroom.db") as connection:
                _, fingerprint = _database(connection)
                if fingerprint != plan["database_snapshot_sha256"]:
                    raise WorkflowError("database snapshot does not match manifest plan")
            expected = {}
            for group in ("claimed", "legacy", "unclaimed"):
                for record in plan[group]:
                    _safe_relative(Path(record["target"]))
                    source = Path(record["source"])
                    if not source.is_absolute():
                        _safe_relative(source)
                    elif source.as_posix() not in plan["selected_source_csvs"]:
                        raise WorkflowError("absolute artifact source is not a selected CSV")
                    name = self._archive_name(MigrationEntry(source, Path(record["target"]), record["sha256"], record["size"]))
                    expected[name] = {"archive_path": name, "source": record["source"], "sha256": record["sha256"], "size": record["size"]}
            if manifest["artifacts"] != [record for _, record in sorted(expected.items())]:
                raise WorkflowError("artifact manifest does not match migration entries")
            expected_files = {name: (r["sha256"], r["size"]) for name, r in expected.items()}
            expected_files["report.json"] = (_digest(backup / "report.json"))
            expected_files["manifest.json"] = (hashlib.sha256(manifest_bytes).hexdigest(), len(manifest_bytes))
            with tempfile.TemporaryDirectory(prefix="newsroom-backup-verification-") as temporary, tarfile.open(backup / "artifacts.tar.gz", mode="r:gz") as archive:
                members = archive.getmembers()
                names = [m.name for m in members]
                if len(names) != len(set(names)) or set(names) != set(expected_files):
                    raise WorkflowError("archive member inventory does not match manifest")
                for member in members:
                    safe = PurePosixPath(member.name)
                    if not member.isfile() or safe.is_absolute() or any(p in {"..", "."} for p in safe.parts) or safe.as_posix() != member.name:
                        raise WorkflowError("unsafe archive member")
                    if member.size != expected_files[member.name][1]:
                        raise WorkflowError("archive member size mismatch")
                    target = Path(temporary).joinpath(*safe.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                    if _digest(target) != expected_files[member.name]:
                        raise WorkflowError(f"archive member hash mismatch: {member.name}")
        except (OSError, sqlite3.Error, tarfile.TarError, ValueError, KeyError, TypeError) as error:
            raise WorkflowError(f"backup verification failed: {error}") from error

    @property
    def receipt_path(self) -> Path:
        return self.room.root / 'data/workspace-migration-receipt.json'

    def _pending_journals(self) -> None:
        directory = self.room.root / 'data/workspace-migration-journals'
        assert_migration_access(self.room.root)
        _no_symlinks(directory)
        _no_symlinks(self.workspace.root)
        for path in sorted(directory.glob('*.json')):
            try:
                _no_symlinks(path)
                journal = json.loads(path.read_bytes())
                if journal.get('finalization_status') == 'RECOVERY_REQUIRED' or journal.get('recovery_status') not in {None, 'ROLLED_BACK'} or (journal['stage'] != 'CLEANED' and journal.get('recovery_status') != 'ROLLED_BACK'):
                    raise ValueError('unfinished migration')
            except (ValueError, KeyError, OSError) as error:
                raise WorkflowError(f'migration recovery required; inspect {path.relative_to(self.room.root)} and restore its verified backup before retrying') from error
        if list(self.workspace.root.glob('.migration-staging-*')):
            raise WorkflowError('migration recovery required; retained staging exists; inspect migration journals before retrying')

    def _acquire_barrier(self) -> None:
        state = self._execution
        barrier = self.room.root / 'data/workspace-migration-lock.json'
        _no_symlinks(barrier)
        record = {'schema_version': 1, 'owner': uuid4().hex,
                  'journal_path': state['journal_path'], 'manifest_sha256': state['manifest_sha256']}
        state['barrier_record'] = record
        # Exclusive creation prevents one migration from claiming another's lock.
        try:
            descriptor = os.open(barrier, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise WorkflowError('migration access barrier already owned; recovery required') from error
        state['barrier_acquired'] = True
        with os.fdopen(descriptor, 'wb') as handle:
            metadata = os.fstat(handle.fileno())
            state['barrier_identity'] = [metadata.st_dev, metadata.st_ino]
            handle.write(_json_bytes(record))
            handle.flush()
            os.fsync(handle.fileno())
        self._journal()

    def _release_barrier(self) -> None:
        state = self._execution
        if not state.get('barrier_acquired'):
            return
        barrier = self.room.root / 'data/workspace-migration-lock.json'
        _no_symlinks(barrier)
        try:
            metadata = barrier.stat()
            record = json.loads(barrier.read_bytes())
            if record != state['barrier_record'] or [metadata.st_dev, metadata.st_ino] != state['barrier_identity']:
                raise ValueError('barrier ownership changed')
        except (OSError, ValueError, KeyError) as error:
            raise WorkflowError('migration barrier owner mismatch; recovery required; foreign lock retained') from error
        barrier.unlink()
        state['barrier_acquired'] = False

    def _assert_unchanged(self, plan: MigrationPlan, *, inventory: bool = False) -> None:
        if plan.blocking_errors:
            raise WorkflowError('migration preflight has blocking errors')
        for entry in plan.entries:
            path = self._source(entry.source, external=entry.source.is_absolute())
            if _digest(path) != (entry.sha256, entry.size):
                raise WorkflowError(f'migration source changed after preflight: {entry.source}')
        roots = {'research', 'articles', 'images', 'output', 'workspace'}
        expected = {entry.source for entry in plan.entries if not entry.source.is_absolute() and entry.source.parts[0] in roots}
        actual = set()
        staging = getattr(self, '_execution', {}).get('staging_path')
        for name in roots:
            root = self.room.root / name
            _no_symlinks(root)
            if not root.exists():
                continue
            for directory, dirs, files in os.walk(root, onerror=_walk_error):
                dirs[:] = [name for name in dirs if str(Path(directory) / name) != staging]
                for name in dirs + files:
                    _no_symlinks(Path(directory) / name)
                actual.update((Path(directory) / name).relative_to(self.room.root) for name in files)
        if actual != expected:
            raise WorkflowError('migration inventory changed after preflight')
        with self._connect_readonly() as connection:
            if _database(connection)[1] != plan.database_snapshot_sha256:
                raise WorkflowError('migration database changed after preflight')
        if inventory:
            selected = [source if source.is_absolute() else self.room.root / source for source in plan.selected_source_csvs]
            if self.preflight(selected).to_dict() != plan.to_dict():
                raise WorkflowError('migration inventory changed after preflight')

    @staticmethod
    def _quote_identifier(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    def _switch_database(self, plan: MigrationPlan, room: Newsroom | None = None) -> None:
        target = room or self.room
        with closing(target.db.connect()) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                if _database(connection)[1] != plan.database_snapshot_sha256:
                    raise WorkflowError('migration database changed after preflight')
                # Hashes are checked again while holding the DB write lock.
                if room is None:
                    backup = Path(self._execution['backup_path'])
                    self.verify_backup(backup)
                    if _digest(backup / 'manifest.json')[0] != self._execution['manifest_sha256']:
                        raise WorkflowError('verified backup manifest changed before database switch')
                    self._assert_unchanged(plan)
                changes = {(e.database_table, e.database_field, e.source.as_posix(), e.target.as_posix())
                           for e in plan.entries if e.database_table and e.database_field}
                for table, column, source, destination in sorted(changes):
                    quoted_table, quoted_column = self._quote_identifier(table), self._quote_identifier(column)
                    cursor = connection.execute(
                        f'UPDATE {quoted_table} SET {quoted_column}=? WHERE {quoted_column}=?',
                        (destination, source),
                    )
                    if not cursor.rowcount:
                        raise WorkflowError('migration database source no longer matches plan')
                connection.execute('UPDATE batches SET workspace_version=?', (1,))
                if room is None:
                    self._execution['database_update_attempted'] = True
                    self._journal()  # Commit may succeed even when its caller is interrupted.
                connection.commit()
                if room is None:
                    self._execution['database_updated'] = True
            except BaseException as original:
                try:
                    connection.rollback()
                except BaseException as rollback_error:
                    raise BaseExceptionGroup('database switch and connection rollback failed', [original, rollback_error]) from None
                raise

    def _verify_hashes(self, plan: MigrationPlan, root: Path, *, current_views: bool = False) -> None:
        for entry in plan.entries:
            # Receipt verification permits derived views to evolve with current
            # SQLite state. Their identities/content are validated separately;
            # canonical source artifacts remain bound to original manifest bytes.
            parts = entry.target.parts
            view = entry.target == Path('workspace/README.md') or entry.target == Path('workspace/Migrate Newsroom.command')
            if parts[:2] == ('workspace', 'batches') and len(parts) >= 4:
                view = view or parts[3:] in {('README.md',), ('batch-state.json',), ('OPEN_BATCH.command',), ('input', 'source-metadata.json')}
                view = view or (len(parts) == 6 and parts[3] == 'articles' and parts[5] == 'image-candidates.json')
                view = view or (len(parts) == 5 and parts[3] == 'actions' and parts[4] in {filename for filename, _ in BATCH_ACTIONS})
            if current_views and entry.database_field is None and view:
                continue
            _safe_relative(entry.target)
            path = root / entry.target
            _no_symlinks(path)
            if not path.resolve().is_relative_to(root) or not path.is_file():
                raise WorkflowError(f'missing or unsafe migrated artifact: {entry.target}')
            if _digest(path) != (entry.sha256, entry.size):
                raise WorkflowError(f'migrated artifact hash mismatch: {entry.target}')

    def _stage_workspace(self, plan: MigrationPlan, backup: Path, stage: Path) -> None:
        """Project paths and rebuild views against a disposable database, never live DB."""
        staged_room = Newsroom(stage)
        staged_room.db_path.parent.mkdir(parents=True)
        shutil.copy2(backup / 'newsroom.db', staged_room.db_path)
        for name in ('batches', 'archive'):
            (stage / 'workspace' / name).mkdir(parents=True)
        for entry in plan.entries:
            if entry.target.parts[0] != 'workspace':
                raise WorkflowError(f'migration target must be beneath workspace: {entry.target}')
            target = stage / entry.target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._source(entry.source, external=entry.source.is_absolute()), target)
        self._verify_hashes(plan, stage)
        self._switch_database(plan, staged_room)
        workspace = BatchWorkspace(staged_room)
        for batch_id, metadata in plan.source_metadata.items():
            path = workspace.batch_root(batch_id) / 'input/source-metadata.json'
            # Existing canonical metadata is retained byte for byte.
            if not path.exists():
                _atomic_write(path, _json_bytes(metadata).decode())
            if not (workspace.batch_root(batch_id) / 'batch-state.json').exists():
                workspace.refresh(batch_id)
        workspace.install_root_actions()
        self._verify_hashes(plan, stage)
        self._validate_materials(staged_room)
        self._execution['expected_database'] = self._database_fingerprint(staged_room)
        self._execution['promoted_files'] = {
            path.relative_to(stage / 'workspace').as_posix(): _digest(path)[0]
            for path in (stage / 'workspace').rglob('*') if path.is_file()
        }
        self._execution['promoted_directories'] = [
            path.relative_to(stage / 'workspace').as_posix()
            for path in (stage / 'workspace').rglob('*') if path.is_dir()
        ]

    @staticmethod
    def _database_fingerprint(room: Newsroom) -> str:
        with closing(room.db.connect()) as connection:
            return _database(connection)[1]

    def _validate_materials(self, room: Newsroom) -> None:
        """Reopen paths, typed handoffs, images, and packages without workflow writes."""
        from . import batch_models as models
        from .batch_release import BatchReleaseWorkflow
        with closing(room.db.connect()) as connection:
            tables, _ = _database(connection)
        articles = {row['article_id']: row for row in tables['articles']}
        if max((row['version'] for row in tables['schema_version']), default=0) != 4:
            raise WorkflowError('migration requires database schema version 4')
        for table, rows in tables.items():
            for row in rows:
                for column, value in row.items():
                    if not column.endswith('_path') or not value:
                        continue
                    relative = Path(value)
                    _safe_relative(relative)
                    if relative.parts[:2] not in {('workspace', 'batches'), ('workspace', 'archive')}:
                        raise WorkflowError(f'noncanonical database reference: {table}.{column}')
                    path = room.root / relative
                    _no_symlinks(path)
                    if not path.resolve().is_relative_to(room.root) or not path.is_file():
                        raise WorkflowError(f'missing database reference: {table}.{column}')
                    _digest(path)  # Reopen every path, including prompts and images.
                    article = articles.get(row.get('article_id'))
                    batch = article['owning_batch_id'] if article else row.get('batch_id')
                    kind = {'draft_path': 'article', 'research_brief_path': 'research-brief', 'fact_check_path': 'fact-check'}.get(column)
                    if article and kind:
                        match = re.fullmatch(kind + r'-v([1-9][0-9]*)\.md', relative.name)
                        version = int(match[1]) if match else 0
                        prior_check = column == 'fact_check_path' and article['fact_check_outcome'] is None and 1 <= version <= article['revision_number']
                        if not match or (version != article['revision_number'] and not prior_check):
                            raise WorkflowError(f'database artifact current version mismatch: {table}.{column}')
                    if table == 'batch_requests':
                        expected_request = BatchWorkspace(room).request_artifact_path(
                            row['batch_id'], row['request_type'], row['revision_number'], result=column == 'result_path',
                            historical_request_id=row['request_id'] if row['status'] == 'SUPERSEDED' and column == 'request_path' else None,
                        ).relative_to(room.root)
                        if relative != expected_request:
                            raise WorkflowError('database request filename/version identity mismatch')
                    expected = WorkspaceMigrator(room)._target(relative, article, batch, column, row if table == 'batch_requests' else None)
                    if relative != expected:
                        raise WorkflowError(f'database artifact identity mismatch: {table}.{column}')
        if any(row['workspace_version'] != 1 for row in tables['batches']):
            raise WorkflowError('workspace migration version mismatch')
        images = {image['image_id']: image for image in tables['images']}
        for article in articles.values():
            if article['image_id']:
                selected = images.get(article['image_id'])
                if selected is None or selected['article_id'] != article['article_id']:
                    raise WorkflowError('current migrated image belongs to a different article')
        launcher = room.root / 'workspace/Migrate Newsroom.command'
        try:
            _no_symlinks(launcher)
            if launcher.read_text() != render_migration_script() or not launcher.stat().st_mode & 0o111:
                raise ValueError('root launcher differs from bootstrap dispatcher')
        except (OSError, ValueError) as error:
            raise WorkflowError('missing or invalid migration launcher') from error
        for batch in tables['batches']:
            workspace = BatchWorkspace(room)
            root = workspace.batch_root(batch['batch_id'])
            for filename, action in (('OPEN_BATCH.command', 'open-batch'), *BATCH_ACTIONS):
                launcher = root / filename if filename == 'OPEN_BATCH.command' else root / 'actions' / filename
                try:
                    _no_symlinks(launcher)
                    if launcher.read_text() != render_action_script(batch['batch_id'], action) or not launcher.stat().st_mode & 0o111:
                        raise ValueError('batch launcher differs from bootstrap dispatcher')
                except (OSError, ValueError) as error:
                    raise WorkflowError('missing or invalid batch launcher') from error
            try:
                metadata = json.loads((root / 'input/source-metadata.json').read_bytes())
                source = workspace.source_csv_path(batch['batch_id'])
                expected = {'source_filename': Path(batch['source_filename']).name,
                            'source_sha256': batch['source_sha256'], 'available': source.is_file()}
                if metadata != expected or (source.is_file() and _digest(source)[0] != batch['source_sha256']):
                    raise ValueError('source metadata does not match batch')
                state = json.loads((root / 'batch-state.json').read_bytes())
                expected_state = workspace.snapshot(batch['batch_id'])
                if {key: value for key, value in state.items() if key != 'workspace_version'} != {key: value for key, value in expected_state.items() if key != 'workspace_version'} or batch['batch_id'] not in (root / 'README.md').read_text():
                    raise ValueError('dashboard batch identity mismatch')
            except (OSError, ValueError, TypeError) as error:
                raise WorkflowError(f'migrated source metadata or dashboard identity mismatch: {batch["batch_id"]}') from error
            for article in room.db.all('SELECT article_id FROM articles WHERE owning_batch_id=?', (batch['batch_id'],)):
                candidate_view = workspace.article_root(batch['batch_id'], article['article_id']) / 'image-candidates.json'
                if json.loads(candidate_view.read_bytes()) != room.list_image_candidates(article['article_id']):
                    raise WorkflowError('migrated image candidate view differs from current database')
        for request in tables['batch_requests']:
            kind = {'CONTENT': 'Content', 'FACT_CHECK': 'FactCheck', 'REVISION': 'Revision'}.get(request['request_type'])
            if kind is None:
                raise WorkflowError('unknown migrated request type')
            documents = {}
            for column, suffix in (('request_path', 'Request'), ('result_path', 'Result')):
                if not request[column]:
                    continue
                raw = (room.root / request[column]).read_bytes()
                try:
                    document = getattr(models, 'Batch' + kind + suffix).model_validate_json(raw)
                except ValidationError as error:
                    raise WorkflowError(f'invalid migrated handoff: {column}') from error
                if (document.batch_id, document.request_id, document.output_file) != (
                    request['batch_id'], request['request_id'], request['expected_result_filename'],
                ):
                    raise WorkflowError('migrated request/result identity mismatch')
                ids = {item.article_id for item in document.items}
                if any(article_id not in articles or articles[article_id]['owning_batch_id'] != request['batch_id'] for article_id in ids):
                    raise WorkflowError('migrated handoff article identity mismatch')
                documents[column] = ids
                if column == 'result_path' and request['result_sha256'] != hashlib.sha256(raw).hexdigest():
                    raise WorkflowError('migrated result hash mismatch')
            if len(documents) == 2 and documents['request_path'] != documents['result_path']:
                raise WorkflowError('migrated request/result article sets differ')
        for image in tables['images']:
            article = articles.get(image['article_id'])
            if article is None:
                raise WorkflowError('migrated image article identity mismatch')
            if image['image_candidate_id']:
                candidate = next((row for row in tables['image_candidates'] if row['image_candidate_id'] == image['image_candidate_id']), None)
                if candidate is None or candidate['article_id'] != image['article_id'] or candidate['batch_id'] != article['owning_batch_id']:
                    raise WorkflowError('migrated image candidate identity mismatch')
        # Generate/read dashboards only in a disposable clone. Existing canonical
        # views remain byte-for-byte intact, and no states or publish trees advance.
        with tempfile.TemporaryDirectory(prefix='newsroom-migration-validation-') as temporary:
            clone = Newsroom(temporary)
            clone.db_path.parent.mkdir()
            with closing(room.db.connect()) as source, closing(clone.db.connect()) as destination:
                source.backup(destination)
            shutil.copytree(room.root / 'workspace', clone.root / 'workspace', ignore=shutil.ignore_patterns('.migration-staging-*'))
            for batch in tables['batches']:
                state = BatchWorkspace(clone).refresh(batch['batch_id'])
                root = BatchWorkspace(clone).batch_root(batch['batch_id'])
                if json.loads((root / 'batch-state.json').read_bytes()) != state or not (root / 'README.md').read_text().strip():
                    raise WorkflowError('migrated dashboard validation failed')
                actual_root = BatchWorkspace(room).batch_root(batch['batch_id'])
                def dashboard_content(path: Path) -> str:
                    return '\n'.join(line for line in path.read_text().splitlines() if not line.startswith('来源 CSV 副本：'))
                if dashboard_content(actual_root / 'README.md') != dashboard_content(root / 'README.md'):
                    raise WorkflowError('migrated dashboard differs from current database')
            if (room.root / 'workspace/README.md').read_bytes() != (clone.root / 'workspace/README.md').read_bytes():
                raise WorkflowError('migration root dashboard differs from current database')
            for original in tables['articles']:
                article = room.get_article(original['article_id'])
                if article['status'] not in {'APPROVED', 'PUBLISHED'}:
                    continue
                if article['fact_check_outcome'] != 'PASS':
                    raise WorkflowError('publication material requires current PASS fact-check')
                package_article = {**article, 'status': 'APPROVED'}
                image, sources = room._publication_materials(package_article)
                package = Path(temporary) / 'packages' / article['article_id']
                package.mkdir(parents=True)
                room._write_publication_package(article, image, sources, package)
                BatchReleaseWorkflow._validate_staged_package(package, article, image, sources)
                owner = article['owning_batch_id']
                existing = (BatchWorkspace(room).batch_root(owner) / 'publish' / article['article_id']) if owner else room.root / 'workspace/archive/legacy-single-articles' / article['article_id'] / 'publish'
                if existing.is_dir():
                    metadata = json.loads((existing / 'metadata.json').read_bytes())
                    if metadata.get('article_id') != article['article_id'] or metadata.get('revision_number') != article['revision_number']:
                        raise WorkflowError('existing package article/version identity mismatch')
                    # Old package metadata paths legitimately precede migration;
                    # content and identity must still represent the current version.
                    for output, source in sources.items():
                        filename = 'hero' + source.suffix.lower() if output == 'hero' else output
                        if (existing / filename).read_bytes() != source.read_bytes():
                            raise WorkflowError('existing package content differs from current material')
                    license_metadata = json.loads((existing / 'image-license.json').read_bytes())
                    if license_metadata.get('image_id') != image['image_id'] or license_metadata.get('article_id') != article['article_id']:
                        raise WorkflowError('existing package image identity mismatch')
                    if {key: value for key, value in license_metadata.items() if key not in {'image_path', 'prompt_path'}} != {key: value for key, value in image.items() if key not in {'image_path', 'prompt_path'}}:
                        raise WorkflowError('existing package image rights differ from registered provenance')
                    for filename in ('sources.md', 'publication-checklist.md'):
                        if not (existing / filename).read_text().strip():
                            raise WorkflowError('existing package missing required material')

    def _journal(self, stage: str | None = None) -> None:
        state = self._execution
        if stage:
            state['stage'] = stage
            state['history'].append({'stage': stage, 'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds')})
        _durable_json(Path(state['journal_path']), state)

    def _verify_retained(self, plan: MigrationPlan, stage: Path) -> None:
        """Detect additions, symlinks and changed original bytes before retirement."""
        for operation in self._execution['promotions']:
            if not operation['had_original']:
                continue
            name = operation['name']
            previous = stage / 'retained' / name
            relative = Path('workspace') / name
            expected = {entry.source.relative_to(relative): (entry.sha256, entry.size)
                        for entry in plan.entries if not entry.source.is_absolute() and entry.source.is_relative_to(relative)}
            expected_directories = {Path(record['path']).relative_to(relative)
                                    for record in self._execution['directories']
                                    if Path(record['path']).is_relative_to(relative)}
            try:
                _no_symlinks(previous)
                paths = [previous]
                if previous.is_dir():
                    paths.extend(previous.rglob('*'))
                actual = {}
                actual_directories = set()
                for path in paths:
                    if path.is_symlink():
                        raise WorkflowError('unexpected symlink in retained originals')
                    if path.is_file():
                        actual[path.relative_to(previous)] = _digest(path)
                    elif path.is_dir():
                        actual_directories.add(path.relative_to(previous))
                    else:
                        raise WorkflowError('unexpected filesystem object in retained originals')
                if not previous.exists() or actual != expected or actual_directories != expected_directories:
                    raise WorkflowError('retained originals changed after final inventory; additions preserved; recovery required')
            except BaseException:
                self._execution.setdefault('retained_changed', []).append(name)
                raise

    def _promote_workspace(self, stage: Path) -> None:
        retained = stage / 'retained'
        retained.mkdir()
        for source in sorted((stage / 'workspace').iterdir()):
            destination = self.workspace.root / source.name
            operation = {'name': source.name, 'had_original': destination.exists()}
            self._execution['promotions'].append(operation)
            self._journal()  # Persist intent before either rename, including interruptions.
            if operation['had_original']:
                os.replace(destination, retained / source.name)
                self._verify_retained(self._active_plan, stage)
            os.replace(source, destination)
            self._journal()

    def _verify_workspace(self, plan: MigrationPlan) -> None:
        self._verify_hashes(plan, self.room.root)
        for relative, expected_sha in self._execution['promoted_files'].items():
            path = self.workspace.root / relative
            _no_symlinks(path)
            if not path.is_file() or _digest(path)[0] != expected_sha:
                raise WorkflowError(f'promoted workspace hash mismatch: {relative}')
        if self._database_fingerprint(self.room) != self._execution['expected_database']:
            raise WorkflowError('migrated database does not match projected records')
        self._validate_materials(self.room)

    def _cleanup_originals(self, plan: MigrationPlan) -> None:
        if self._execution['stage'] != 'VERIFIED':
            raise WorkflowError('cleanup requires verified migration')
        for entry in {entry.source: entry for entry in plan.entries}.values():
            if entry.source.is_absolute() or entry.source == entry.target or entry.source.parts[0] not in {'research', 'articles', 'images', 'output'}:
                continue
            path = self._source(entry.source)
            if _digest(path) != (entry.sha256, entry.size):
                raise WorkflowError('migration original changed before cleanup')
            self._execution['removed_sources'].append(entry.source.as_posix())
            self._journal()
            path.unlink()
        for record in sorted(self._execution['directories'], key=lambda item: len(Path(item['path']).parts), reverse=True):
            path = self.room.root / record['path']
            if Path(record['path']).parts[0] in {'research', 'articles', 'images', 'output'} and path.is_dir() and not any(path.iterdir()):
                path.rmdir()

    def _remove_promoted(self, path: Path) -> None:
        relative = path.relative_to(self.workspace.root)
        expected_files = {name: digest for name, digest in self._execution['promoted_files'].items()
                          if Path(name).is_relative_to(relative)}
        expected_directories = {name for name in self._execution['promoted_directories']
                                if Path(name).is_relative_to(relative)}
        actual_files, actual_directories = {}, set()
        changed = False
        paths = [path, *path.rglob('*')] if path.is_dir() and not path.is_symlink() else [path]
        for file in paths:
            key = file.relative_to(self.workspace.root).as_posix()
            mode = file.lstat().st_mode
            if stat.S_ISREG(mode):
                actual_files[key] = _digest(file)[0]
            elif stat.S_ISDIR(mode):
                actual_directories.add(key)
            else:
                changed = True
        if changed or actual_files != expected_files or actual_directories != expected_directories:
            recovery = self.room.root / 'data/workspace-migration-recovery' / Path(self._execution['journal_path']).stem
            _no_symlinks(recovery)
            recovery.mkdir(parents=True, exist_ok=True)
            destination = recovery / path.name
            if destination.exists() or destination.is_symlink():
                raise WorkflowError('recovery quarantine already exists; originals and changed content retained')
            record = {'source': str(path), 'path': str(destination), 'status': 'PLANNED'}
            self._execution.setdefault('quarantined_promotions', []).append(record)
            self._journal()  # Bind the atomic move to its recovery journal first.
            os.replace(path, destination)
            record['status'] = 'PRESERVED'
            self._journal()
            return
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def _restore_database(self, backup: Path) -> None:
        self.verify_backup(backup)
        temporary = self.room.db_path.with_suffix('.migration-restore')
        shutil.copy2(backup / 'newsroom.db', temporary)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(self.room.db_path) + suffix).unlink(missing_ok=True)
        os.replace(temporary, self.room.db_path)

    def _restore_artifacts(self, plan: MigrationPlan, backup: Path, stage: Path) -> None:
        for operation in reversed(self._execution['promotions']):
            name = operation['name']
            destination, previous = self.workspace.root / name, stage / 'retained' / name
            # An intent can exist before the first rename. In that case the
            # original destination is still intact and must not be replaced.
            if (stage / 'workspace' / name).exists() and not previous.exists() and destination.exists():
                continue
            if operation['had_original']:
                relative = Path('workspace') / name
                if previous.exists() or previous.is_symlink():
                    originals = {entry.source: entry for entry in plan.entries if not entry.source.is_absolute() and entry.source.is_relative_to(relative)}
                    with tarfile.open(backup / 'artifacts.tar.gz', 'r:gz') as archive:
                        for source, entry in originals.items():
                            path = previous / source.relative_to(relative) if previous.is_dir() else previous
                            if path.exists() or path.is_symlink():
                                continue  # Keep editor changes and additions byte for byte.
                            _no_symlinks(path)
                            path.parent.mkdir(parents=True, exist_ok=True)
                            with archive.extractfile(self._archive_name(entry)) as archived, path.open('wb') as output:
                                shutil.copyfileobj(archived, output)
                            metadata = self._execution['source_stats'][source.as_posix()]
                            path.chmod(metadata['mode'])
                            os.utime(path, ns=(metadata['atime_ns'], metadata['mtime_ns']))
                            if _digest(path) != (entry.sha256, entry.size):
                                raise WorkflowError('recovery archive artifact hash mismatch')
                    if destination.exists() or destination.is_symlink():
                        self._remove_promoted(destination)
                    os.replace(previous, destination)
                    continue

                with tempfile.TemporaryDirectory(prefix='newsroom-artifact-recovery-', dir=self.room.root / 'data') as temporary:
                    restored = Path(temporary) / name
                    records = [record for record in self._execution['directories'] if Path(record['path']).is_relative_to(relative)]
                    for record in records:
                        path = Path(temporary) / Path(record['path']).relative_to('workspace')
                        path.mkdir(parents=True, exist_ok=True)
                        path.chmod(record['mode'])
                    with tarfile.open(backup / 'artifacts.tar.gz', 'r:gz') as archive:
                        originals = {entry.source: entry for entry in plan.entries if not entry.source.is_absolute() and entry.source.is_relative_to(relative)}
                        for source, entry in originals.items():
                            path = Path(temporary) / source.relative_to('workspace')
                            path.parent.mkdir(parents=True, exist_ok=True)
                            with archive.extractfile(self._archive_name(entry)) as archived, path.open('wb') as output:
                                shutil.copyfileobj(archived, output)
                            metadata = self._execution['source_stats'][source.as_posix()]
                            path.chmod(metadata['mode'])
                            os.utime(path, ns=(metadata['atime_ns'], metadata['mtime_ns']))
                            if _digest(path) != (entry.sha256, entry.size):
                                raise WorkflowError('recovery archive artifact hash mismatch')
                    if not restored.exists():
                        raise WorkflowError('recovery archive lacks original promoted tree')
                    if destination.exists() or destination.is_symlink():
                        self._remove_promoted(destination)
                    os.replace(restored, destination)
            elif destination.exists() or destination.is_symlink():
                self._remove_promoted(destination)
        removed = set(self._execution['removed_sources'])
        with tarfile.open(backup / 'artifacts.tar.gz', 'r:gz') as archive:
            for entry in {entry.source: entry for entry in plan.entries}.values():
                if entry.source.as_posix() not in removed:
                    continue
                destination = self.room.root / entry.source
                _no_symlinks(destination)
                if destination.exists():
                    if _digest(destination) != (entry.sha256, entry.size):
                        raise WorkflowError('recovery refuses to overwrite changed original')
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(self._archive_name(entry)) as source, destination.open('wb') as output:
                    shutil.copyfileobj(source, output)
                metadata = self._execution['source_stats'][entry.source.as_posix()]
                destination.chmod(metadata['mode'])
                os.utime(destination, ns=(metadata['atime_ns'], metadata['mtime_ns']))
        for record in self._execution['directories']:
            path = self.room.root / record['path']
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(record['mode'])
        self._verify_originals(plan)

    def _verify_originals(self, plan: MigrationPlan) -> None:
        for entry in plan.entries:
            if _digest(self._source(entry.source, external=entry.source.is_absolute())) != (entry.sha256, entry.size):
                raise WorkflowError('recovery source hash mismatch')
        with self._connect_readonly() as connection:
            if _database(connection)[1] != plan.database_snapshot_sha256:
                raise WorkflowError('recovery database does not match original snapshot')

    def execute(self, plan: MigrationPlan, backup_dir: Path) -> dict[str, Any]:
        with operation_lock(self.room.root, exclusive=True):
            return self._execute_locked(plan, backup_dir)

    def _execute_locked(self, plan: MigrationPlan, backup_dir: Path) -> dict[str, Any]:
        """Execute only the exact verified plan; compensate all post-commit failures."""
        self._pending_journals()
        self._assert_unchanged(plan, inventory=True)
        _no_symlinks(Path(backup_dir).absolute())
        backup = Path(backup_dir).resolve()
        self.verify_backup(backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        if manifest['plan'] != plan.to_dict():
            raise WorkflowError('verified backup does not match migration plan')
        manifest_sha = _digest(backup / 'manifest.json')[0]
        if self.receipt_path.exists():
            return self.execute_from_current_state()
        directories = []
        for name in ('research', 'articles', 'images', 'output', 'workspace'):
            root = self.room.root / name
            for path in ([root, *root.rglob('*')] if root.is_dir() else []):
                if path.is_dir():
                    metadata = path.stat()
                    directories.append({'path': path.relative_to(self.room.root).as_posix(), 'mode': stat.S_IMODE(metadata.st_mode), 'mtime_ns': metadata.st_mtime_ns, 'atime_ns': metadata.st_atime_ns})
        stats = {}
        for entry in plan.entries:
            metadata = self._source(entry.source, external=entry.source.is_absolute()).stat()
            stats[entry.source.as_posix()] = {'mode': stat.S_IMODE(metadata.st_mode), 'mtime_ns': metadata.st_mtime_ns, 'atime_ns': metadata.st_atime_ns}
        workspace_existed = self.workspace.root.exists()
        self.workspace.root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        stage = Path(tempfile.mkdtemp(prefix=f'.migration-staging-{stamp}-', dir=self.workspace.root))
        journal = self.room.root / 'data/workspace-migration-journals' / (stage.name + '.json')
        self._execution = {'schema_version': 1, 'stage': None, 'history': [], 'manifest_sha256': manifest_sha,
                           'backup_path': str(backup), 'journal_path': str(journal), 'staging_path': str(stage),
                           'database_updated': False, 'database_update_attempted': False,
                           'barrier_acquired': False, 'promotions': [], 'removed_sources': [],
                           'directories': directories, 'source_stats': stats, 'promoted_files': {}}
        self._active_plan = plan
        try:
            self._journal('BACKED_UP')
            self._assert_unchanged(plan)  # Immediately before copying any sources.
            self._stage_workspace(plan, backup, stage)
            self._journal('STAGED')
            # Full inventory cannot include our own staging/journal. Sources and
            # DB are rehashed again immediately before the locked switch.
            self._assert_unchanged(plan)
            self._acquire_barrier()
            self._switch_database(plan)
            self._journal('DATABASE_UPDATED')
            self._promote_workspace(stage)
            self._journal('PROMOTED')
            self._verify_workspace(plan)
            self._journal('VERIFIED')
            self._cleanup_originals(plan)
            receipt = {'schema_version': 1, 'manifest_sha256': manifest_sha, 'backup_path': str(backup), 'journal_path': str(journal)}
            _durable_json(self.receipt_path, receipt)
            self._verify_retained(plan, stage)
            shutil.rmtree(stage)
            self._journal('CLEANED')
            self._release_barrier()
        except BaseException as original:
            if self._execution['stage'] == 'CLEANED':
                # Receipt and cleanup are the commit point. Finalization cannot
                # revert them, even if unlink completed before raising.
                return self._finalize_committed(plan, receipt, original)
            failures = [original]
            self._execution['failure'] = {'type': type(original).__name__, 'message': str(original)}
            if self._execution['database_updated'] or self._execution['database_update_attempted']:
                for recover in (lambda: self._restore_database(backup), lambda: self._restore_artifacts(plan, backup, stage), lambda: self.receipt_path.unlink(missing_ok=True)):
                    try:
                        recover()
                    except BaseException as error:
                        failures.append(error)
            if len(failures) == 1:
                try:
                    if stage.exists():
                        shutil.rmtree(stage)
                    if not workspace_existed and not any(self.workspace.root.iterdir()):
                        self.workspace.root.rmdir()
                    for record in reversed(directories):
                        path = self.room.root / record['path']
                        if any(Path(record['path']).is_relative_to(Path('workspace') / name) for name in self._execution.get('retained_changed', [])):
                            continue
                        _no_symlinks(path)
                        path.chmod(record['mode'])
                        os.utime(path, ns=(record['atime_ns'], record['mtime_ns']))
                except BaseException as error:
                    failures.append(error)
            if self._execution.get('quarantined_promotions'):
                failures.append(WorkflowError('changed promoted content preserved in migration recovery quarantine; recovery required'))
            self._execution['recovery_status'] = 'ROLLED_BACK' if len(failures) == 1 else 'RECOVERY_REQUIRED'
            self._execution['recovery_failures'] = [{'type': type(error).__name__, 'message': str(error)} for error in failures[1:]]
            try:
                self._journal()
                if len(failures) == 1:
                    self._release_barrier()  # Only after a recorded completed rollback.
            except BaseException as error:
                failures.append(error)
                self._execution['recovery_status'] = 'RECOVERY_REQUIRED'
                self._execution['recovery_failures'].append({'type': type(error).__name__, 'message': str(error)})
                try:
                    self._journal()
                except BaseException as journal_error:
                    failures.append(journal_error)
            if len(failures) > 1:
                raise BaseExceptionGroup('migration failed; recovery also failed; inspect migration journal', failures) from None
            raise
        return {'status': 'PASS', 'manifest_sha256': manifest_sha, 'journal_path': str(journal), 'backup_path': str(backup)}

    def _finalize_committed(self, plan: MigrationPlan, receipt: dict[str, Any], original: BaseException) -> dict[str, Any]:
        state = self._execution
        state['finalization_error'] = {'type': type(original).__name__, 'message': str(original)}
        diagnostics = []
        verified = False
        try:
            _no_symlinks(self.receipt_path)
            _no_symlinks(Path(state['journal_path']))
            current = json.loads(Path(state['journal_path']).read_bytes())
            if json.loads(self.receipt_path.read_bytes()) != receipt or current['stage'] != 'CLEANED' or current['manifest_sha256'] != state['manifest_sha256']:
                raise WorkflowError('committed receipt or journal cannot be verified')
            self._verify_workspace(plan)
            verified = True
        except BaseException as error:
            diagnostics.append(error)
        barrier = self.room.root / 'data/workspace-migration-lock.json'
        absent = False
        try:
            barrier.lstat()
        except FileNotFoundError:
            absent = True
        except BaseException as error:
            diagnostics.append(error)
        if absent and not verified:
            try:
                self._acquire_barrier()
                absent = False
            except BaseException as error:
                diagnostics.append(error)
        state['finalization_status'] = 'COMMITTED' if verified and absent else 'RECOVERY_REQUIRED'
        state['barrier_acquired'] = not absent
        state['finalization_failures'] = [{'type': type(error).__name__, 'message': str(error)} for error in diagnostics]
        try:
            self._journal()
        except BaseException as error:
            diagnostics.append(error)
        if verified and absent and not diagnostics:
            return {'status': 'PASS', 'finalization_warning': state['finalization_error'], **receipt}
        cause = BaseExceptionGroup('committed migration finalization errors', [original, *diagnostics]) if diagnostics else original
        raise WorkflowError('migration committed; finalization recovery required; inspect migration journal and persistent barrier') from cause

    def execute_from_current_state(self) -> dict[str, Any]:
        """Never infer completion from paths/version alone or an unfinished journal."""
        self._pending_journals()
        if not self.receipt_path.exists():
            raise WorkflowError('verified migration receipt required; run explicit preflight and execute with a verified backup')
        try:
            _no_symlinks(self.receipt_path)
            receipt = json.loads(self.receipt_path.read_bytes())
            backup = Path(receipt['backup_path'])
            self.verify_backup(backup)
            if receipt['schema_version'] != 1 or _digest(backup / 'manifest.json')[0] != receipt['manifest_sha256']:
                raise ValueError('manifest mismatch')
            journal_path = Path(receipt['journal_path'])
            if not journal_path.resolve().is_relative_to(self.room.root / 'data/workspace-migration-journals'):
                raise ValueError('journal escape')
            _no_symlinks(journal_path)
            journal = json.loads(journal_path.read_bytes())
            if journal['stage'] != 'CLEANED' or journal['manifest_sha256'] != receipt['manifest_sha256'] or journal.get('recovery_status'):
                raise ValueError('unfinished journal')
            plan = MigrationPlan.from_dict(json.loads((backup / 'manifest.json').read_bytes())['plan'])
        except (KeyError, ValueError, OSError) as error:
            raise WorkflowError(f'invalid migration receipt; recovery required: {error}') from error
        self._verify_hashes(plan, self.room.root, current_views=True)
        with self._connect_readonly() as connection:
            if any(row['workspace_version'] != 1 for row in _database(connection)[0]['batches']):
                raise WorkflowError('migration receipt workspace version mismatch')
        for entry in plan.entries:
            if not entry.source.is_absolute() and entry.source != entry.target and entry.source.parts[0] in {'research', 'articles', 'images', 'output'} and (self.room.root / entry.source).exists():
                raise WorkflowError('migration recovery required; original cleanup remains pending')
        self._validate_materials(self.room)
        return {'status': 'ALREADY_MIGRATED', 'manifest_sha256': receipt['manifest_sha256'], 'journal_path': str(journal_path), 'backup_path': str(backup)}
