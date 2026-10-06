"""Read-only workspace migration planning and independently verified local backups."""
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
import unicodedata

from .service import Newsroom, WorkflowError
from .workspace import BatchWorkspace, BatchWorkspacePaths, _atomic_write


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


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
        _no_symlinks(path)
        wal = Path(str(path) + "-wal")
        _no_symlinks(wal)
        if wal.exists():
            # A mode=ro WAL reader still writes read marks in canonical -shm.
            # Replay a stable DB/WAL copy only in a disposable external directory.
            with tempfile.TemporaryDirectory(prefix="newsroom-readonly-database-") as temporary:
                copied = Path(temporary) / "newsroom.db"
                originals = (path, wal)
                before = [_digest(source) for source in originals]
                for source, destination in zip(originals, (copied, Path(str(copied) + "-wal"))):
                    shutil.copyfile(source, destination)
                if before != [_digest(source) for source in originals]:
                    raise WorkflowError("database changed while reading WAL snapshot")
                with closing(sqlite3.connect(copied)) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA query_only=ON")
                    yield connection
        else:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
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
        for article_id, article in articles.items():
            memberships = {i["batch_id"] for i in tables["batch_items"] if i["article_id"] == article_id and i["item_status"] not in {"REUSED_COMPLETE", "EXCLUDED"}}
            owner = article["owning_batch_id"]
            if len(memberships) > 1 or (owner and (owner not in batches or memberships - {owner})):
                plan.blocking_errors.append(f"ambiguous ownership: {article_id}")
        seen: set[Path] = set()

        def add(source: Path, target: Path, batch: str | None = None, article: str | None = None, table: str | None = None, db_field: str | None = None, group: str = "claimed", external: bool = False) -> None:
            try:
                path = self._source(source, external)
                _safe_relative(target)
                _no_symlinks(self.room.root / target)
                sha, size = _digest(path)
                entry = MigrationEntry(source, target, sha, size, batch, article, table, db_field)
                getattr(plan, group).append(entry)
                seen.add(source)
            except (OSError, WorkflowError) as error:
                plan.blocking_errors.append(f"{table or 'inventory'}.{db_field or 'file'} {source.as_posix()}: {error}")

        for table, rows in sorted(tables.items()):
            for row in rows:
                for db_field, value in sorted(row.items()):
                    if not db_field.endswith("_path") or not value:
                        continue
                    source = Path(value)
                    article_id = row.get("article_id")
                    article = articles.get(article_id)
                    batch = article["owning_batch_id"] if article else row.get("batch_id")
                    try:
                        target = self._target(source, article, batch, db_field, row if table == "batch_requests" else None)
                        add(source, target, batch, article_id, table, db_field, "legacy" if article and batch is None else "claimed")
                    except WorkflowError as error:
                        plan.blocking_errors.append(f"{table}.{db_field}: {error}")

        # All output subtrees include packages and prompts, including unknown IDs.
        for root in ("research", "articles", "images", "output", "workspace"):
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
                if not any(e.source == source and e.target == target for e in plan.entries):
                    add(source, target, batch, external=True)
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
        parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = Path(tempfile.mkdtemp(prefix=f"workspace-migration-{stamp}-", dir=parent))
        try:
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
            self.verify_backup(backup)
            if self.preflight(selected).to_dict() != plan.to_dict():
                raise WorkflowError("migration sources or database changed during backup")
            return backup
        except Exception as error:
            try:
                (backup / "INCOMPLETE").write_text("Backup failed verification; do not use for migration.\n", encoding="utf-8")
                shutil.rmtree(backup)
            except OSError:
                # Retain the explicit incomplete marker if removal is unavailable.
                pass
            if isinstance(error, WorkflowError):
                raise
            raise WorkflowError(f"backup verification failed: {error}") from error

    def verify_backup(self, backup: Path) -> None:
        """Check database, manifest, safe member inventory and extracted exact bytes."""
        try:
            if (backup / "INCOMPLETE").exists():
                raise WorkflowError("incomplete backup")
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
