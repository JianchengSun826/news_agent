"""Durable final-only retention for explicitly completed local batches."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .delivery import CompletedDelivery, DeliveryReceipt, RECEIPT
from .models import utc_now
from .service import Newsroom, WorkflowError
from .workspace import BatchWorkspace, _identifier


class CleanupManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    batch_id: str
    files: dict[str, str]
    directories: list[str]
    delivery_files: dict[str, str]

    @model_validator(mode='after')
    def safe_paths(self) -> CleanupManifest:
        _identifier(self.batch_id)
        prefix = Path('workspace/batches') / self.batch_id
        for value in (*self.files, *self.directories):
            path = Path(value)
            if path.is_absolute() or '..' in path.parts or path.as_posix() != value or not path.is_relative_to(prefix):
                raise ValueError('invalid cleanup path')
        if prefix.as_posix() not in self.directories or len(set(self.directories)) != len(self.directories):
            raise ValueError('invalid cleanup directory inventory')
        if set(self.files) & set(self.directories):
            raise ValueError('cleanup file/directory overlap')
        directories = set(self.directories)
        for value in (*self.files, *self.directories):
            if value != prefix.as_posix() and Path(value).parent.as_posix() not in directories:
                raise ValueError('cleanup inventory missing parent')
        for value in self.delivery_files:
            if value != RECEIPT and not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*/(?:article\.md|image-guide\.md|hero\.[a-z0-9]+|image-license\.json)', value):
                raise ValueError('invalid retained file')
        if RECEIPT not in self.delivery_files or any(not re.fullmatch(r'[a-f0-9]{64}', h) for h in (*self.files.values(), *self.delivery_files.values())):
            raise ValueError('invalid cleanup digest inventory')
        return self


def _walk_error(error: OSError) -> None:
    raise error


class BatchRetention:
    def __init__(self, room: Newsroom):
        self.room = room
        self.workspace = BatchWorkspace(room)
        self.delivery = CompletedDelivery(room)

    def record(self, batch_id: str) -> dict[str, Any] | None:
        self.workspace._batch(batch_id)
        # Read-only commands remain usable before an explicit v4 -> v5 init.
        if 'batch_retention' not in self.room.db.table_names():
            return None
        return self.room.db.one('SELECT * FROM batch_retention WHERE batch_id=?', (batch_id,))

    def require_process(self, batch_id: str) -> None:
        if self.record(batch_id):
            raise WorkflowError('该批次已导出并清理过程材料，不能重新修订、导入历史任务或重建完整包；最终正文与图片位于 workspace/completed/' + batch_id)

    def _inventory(self, root: Path, *, hash_files: bool = True) -> tuple[dict[str, str], list[str]]:
        self.delivery._safe_path(root)
        files: dict[str, str] = {}
        dirs: list[str] = []
        if not root.exists():
            return files, dirs
        if not root.is_dir():
            raise WorkflowError('cleanup root must be a directory')
        for directory, children, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
            directory = Path(directory)
            dirs.append(directory.relative_to(self.room.root).as_posix())
            for name in children + filenames:
                path = directory / name
                self.delivery._safe_path(path)
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    files[path.relative_to(self.room.root).as_posix()] = self.delivery._digest(path) if hash_files else ""
                elif not stat.S_ISDIR(metadata.st_mode):
                    raise WorkflowError('cleanup contains an unsafe filesystem object')
        return files, sorted(dirs)

    def _retained(self, batch_id: str, *, strict: bool = True) -> tuple[Path, dict[str, str]]:
        target = self.delivery.validate_target(batch_id) if strict else self.workspace.completed_root(batch_id)
        self.delivery._safe_path(target)
        if not target.is_dir():
            raise WorkflowError('final delivery is missing; retain remaining process materials')
        if strict:
            files, _ = self.delivery._inventory(target)
        else:
            inventory, _ = self._inventory(target, hash_files=False)
            files = {Path(name).relative_to(target.relative_to(self.room.root)).as_posix(): digest
                     for name, digest in inventory.items()}
        files[RECEIPT] = self.delivery._digest(target / RECEIPT)
        return target, files

    def _articles(self, batch_id: str) -> list[dict[str, Any]]:
        batch = self.workspace._batch(batch_id)
        if batch['status'] not in {'PACKAGED', 'PUBLISHED'}:
            raise WorkflowError('cleanup requires a completed export')
        rows = self.room.db.all("SELECT a.* FROM articles a JOIN batch_items bi ON bi.article_id=a.article_id WHERE bi.batch_id=? AND bi.item_status NOT IN ('EXCLUDED','REUSED_COMPLETE')", (batch_id,))
        if not rows or any(a['owning_batch_id'] != batch_id or a['status'] not in {'APPROVED', 'PUBLISHED'} or a['fact_check_outcome'] != 'PASS' for a in rows):
            raise WorkflowError('cleanup requires owned, approved articles with current PASS checks')
        if self.room.db.one("SELECT request_id FROM batch_requests WHERE batch_id=? AND status='PENDING'", (batch_id,)):
            raise WorkflowError('cannot clean a batch with pending requests')
        owned = self.room.db.all('SELECT article_id FROM articles WHERE owning_batch_id=?', (batch_id,))
        if {a['article_id'] for a in rows} != {a['article_id'] for a in owned}:
            raise WorkflowError('cleanup would discard owned articles without final delivery')
        return rows

    def _expected_files(self, batch_id: str, articles: list[dict[str, Any]], *, switched: bool) -> set[str]:
        expected = {RECEIPT}
        for article in articles:
            slug = _identifier(article['slug'])
            draft = self.workspace.completed_root(batch_id) / slug / 'article.md'
            expected.add(f'{slug}/article.md')
            guide = draft.parent / 'image-guide.md'
            if guide.is_file():
                expected.add(f'{slug}/image-guide.md')
                if not switched:
                    source_guide = self.workspace.batch_root(batch_id) / 'publish' / article['article_id'] / 'image-guide.md'
                    self.delivery._safe_path(source_guide)
                    if not source_guide.is_file() or source_guide.read_bytes() != guide.read_bytes():
                        raise WorkflowError('final image guide differs from publication package')
            elif self.room.external_images(article):
                raise WorkflowError('external image workflow requires a retained image guide')
            if switched:
                if article['draft_path'] != draft.relative_to(self.room.root).as_posix() or article['research_brief_path'] or article['fact_check_path']:
                    raise WorkflowError('retained article references do not match final delivery')
            else:
                source = self.room.root / (article['draft_path'] or '')
                self.delivery._safe_path(source)
                if not source.is_file() or source.read_bytes() != draft.read_bytes():
                    raise WorkflowError('final article differs from approved current draft')
            if article['image_id']:
                image = self.room.db.one('SELECT * FROM images WHERE image_id=?', (article['image_id'],))
                if not image or image['article_id'] != article['article_id'] or not image['image_path']:
                    raise WorkflowError('retained image identity is invalid')
                source = self.room.root / image['image_path']
                hero = draft.parent / ('hero' + source.suffix.lower())
                self.delivery._safe_path(source)
                expected.update({f'{slug}/{hero.name}', f'{slug}/image-license.json'})
                if switched:
                    if source != hero or image['prompt_path']:
                        raise WorkflowError('retained image reference does not match final delivery')
                elif not source.is_file() or source.read_bytes() != hero.read_bytes():
                    raise WorkflowError('final image differs from selected original')
                license_record = json.loads((draft.parent / 'image-license.json').read_bytes())
                if not isinstance(license_record, dict) or license_record.get('image_id') != article['image_id']:
                    raise WorkflowError('retained license belongs to a different image')
            elif not self.room.no_image_selected(article) and not self.room.external_images(article):
                raise WorkflowError('cleanup requires an explicit image or no-image decision')
        return expected

    def _check_references(self, batch_id: str) -> None:
        prefix = Path('workspace/batches') / batch_id
        for table in ('articles', 'images', 'batch_requests'):
            for row in self.room.db.all(f'SELECT * FROM {table}'):
                for column, value in row.items():
                    if not column.endswith('_path') or not value:
                        continue
                    path = Path(value)
                    if path.is_absolute() or '..' in path.parts:
                        raise WorkflowError('unsafe database file reference')
                    if not path.is_relative_to(prefix):
                        continue
                    if table == 'articles':
                        owner = row['owning_batch_id']
                    elif table == 'images':
                        article = self.room.db.one('SELECT owning_batch_id FROM articles WHERE article_id=?', (row['article_id'],))
                        owner = article['owning_batch_id'] if article else None
                    else:
                        owner = row['batch_id']
                    if owner != batch_id:
                        raise WorkflowError('cleanup directory is referenced by another batch or unowned article')

    def preflight(self, batch_id: str) -> CleanupManifest:
        articles = self._articles(batch_id)
        _, retained = self._retained(batch_id)
        if set(retained) != self._expected_files(batch_id, articles, switched=False):
            raise WorkflowError('final delivery does not exactly cover batch articles and images')
        self._check_references(batch_id)
        files, directories = self._inventory(self.workspace.batch_root(batch_id))
        return CleanupManifest(batch_id=batch_id, files=files, directories=directories, delivery_files=retained)

    def _manifest(self, record: dict[str, Any]) -> CleanupManifest:
        try:
            manifest = CleanupManifest.model_validate_json(record['cleanup_manifest'])
        except (ValueError, ValidationError) as error:
            raise WorkflowError('invalid durable cleanup inventory') from error
        if manifest.batch_id != record['batch_id'] or record['state'] not in {'CLEANING', 'FINAL_ONLY'} or record['delivery_directory'] != self.workspace.completed_root(manifest.batch_id).relative_to(self.room.root).as_posix():
            raise WorkflowError('invalid retention identity')
        return manifest

    def validate(self, batch_id: str, *, strict: bool = True) -> Path:
        record = self.record(batch_id)
        if not record:
            raise WorkflowError('batch has no final-only retention record')
        manifest = self._manifest(record)
        target, retained = self._retained(batch_id, strict=strict)
        expected = self._expected_files(batch_id, self._articles(batch_id), switched=True)
        if set(manifest.delivery_files) != expected or not expected <= set(retained):
            raise WorkflowError('final delivery differs from recorded retained files')
        if strict and retained != manifest.delivery_files:
            raise WorkflowError('final delivery differs from recorded retained files')
        # The machine receipt remains immutable; editorial files may change on reads.
        try:
            receipt = DeliveryReceipt.model_validate_json((target / RECEIPT).read_bytes())
        except (OSError, ValueError, ValidationError) as error:
            raise WorkflowError('invalid retained delivery receipt') from error
        sealed = {name: digest for name, digest in manifest.delivery_files.items() if name != RECEIPT}
        if receipt.batch_id != batch_id or receipt.files != sealed or retained[RECEIPT] != manifest.delivery_files[RECEIPT]:
            raise WorkflowError('retained receipt differs from durable cleanup record')
        requests = self.room.db.all('SELECT * FROM batch_requests WHERE batch_id=?', (batch_id,))
        if any(r['request_path'] or r['result_path'] for r in requests):
            raise WorkflowError('cleaned batch still references process requests')
        for image in self.room.db.all('SELECT i.* FROM images i JOIN articles a ON a.article_id=i.article_id WHERE a.owning_batch_id=?', (batch_id,)):
            article = self.room.get_article(image['article_id'])
            if image['prompt_path'] or (article['image_id'] != image['image_id'] and image['image_path']):
                raise WorkflowError('cleaned batch still references process images')
        files, dirs = self._inventory(self.workspace.batch_root(batch_id), hash_files=strict)
        if strict and record['state'] == 'FINAL_ONLY' and (files or dirs):
            raise WorkflowError('process directory reappeared after final-only cleanup')
        if strict and (any(name not in manifest.files or digest != manifest.files[name] for name, digest in files.items()) or not set(dirs) <= set(manifest.directories)):
            raise WorkflowError('cleanup inventory changed; preserve unexpected files')
        self._check_references(batch_id)
        return target

    def finalize(self, batch_id: str, actor: str, reason: str) -> Path:
        from .coordination import operation_lock
        with operation_lock(self.room.root, exclusive=True):
            from .migration import assert_migration_access
            assert_migration_access(self.room.root)
            return self._finalize(batch_id, actor, reason)

    def _finalize(self, batch_id: str, actor: str, reason: str) -> Path:
        if not actor.strip() or not reason.strip():
            raise WorkflowError('cleanup actor and reason are required')
        if 'batch_retention' not in self.room.db.table_names():
            raise WorkflowError('run newsroom init to upgrade the database before final cleanup')
        record = self.record(batch_id)
        if not record:
            manifest = self.preflight(batch_id)
            target = self.workspace.completed_root(batch_id)
            now = utc_now()
            with self.room.db.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                # Recheck while writing: do not trust an earlier filesystem view.
                if self.preflight(batch_id) != manifest:
                    raise WorkflowError('batch files changed before cleanup')
                for article in self._articles(batch_id):
                    draft = target / article['slug'] / 'article.md'
                    connection.execute('UPDATE articles SET draft_path=?,research_brief_path=NULL,fact_check_path=NULL WHERE article_id=?', (draft.relative_to(self.room.root).as_posix(), article['article_id']))
                    images = connection.execute('SELECT * FROM images WHERE article_id=?', (article['article_id'],)).fetchall()
                    for image in images:
                        final = (draft.parent / ('hero' + Path(image['image_path']).suffix.lower())).relative_to(self.room.root).as_posix() if image['image_id'] == article['image_id'] else None
                        connection.execute('UPDATE images SET image_path=?,prompt_path=NULL WHERE image_id=?', (final, image['image_id']))
                connection.execute("UPDATE batch_requests SET request_path='',result_path=NULL WHERE batch_id=?", (batch_id,))
                connection.execute('INSERT INTO batch_retention VALUES (?,?,?,?,?,?,?,?)', (batch_id, 'CLEANING', target.relative_to(self.room.root).as_posix(), manifest.model_dump_json(), actor, reason, now, now))
                self.room._transition(connection, 'batch_retention', batch_id, None, 'CLEANING', actor, reason)
            record = self.record(batch_id)
        target = self.validate(batch_id)
        if record['state'] == 'FINAL_ONLY':
            return target
        manifest = self._manifest(record)
        try:
            for name, digest in sorted(manifest.files.items()):
                path = self.room.root / name
                self.delivery._safe_path(path)
                if not path.exists():
                    continue
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode) or self.delivery._digest(path) != digest:
                    raise WorkflowError('cleanup file changed; preserve process file')
                after = path.lstat()
                if (before.st_ino, before.st_dev, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_dev, after.st_mtime_ns, after.st_size):
                    raise WorkflowError('cleanup file changed while reading')
                path.unlink()
            for name in sorted(manifest.directories, key=lambda value: len(Path(value).parts), reverse=True):
                path = self.room.root / name
                self.delivery._safe_path(path)
                if path.exists():
                    path.rmdir()
            now = utc_now()
            with self.room.db.connect() as connection:
                connection.execute("UPDATE batch_retention SET state='FINAL_ONLY',updated_at=? WHERE batch_id=?", (now, batch_id))
                self.room._transition(connection, 'batch_retention', batch_id, 'CLEANING', 'FINAL_ONLY', actor, reason)
        except (OSError, WorkflowError) as error:
            raise WorkflowError(f'已导出，清理未完成；保留最终成果和恢复记录，请重试 batch export {batch_id}：{error}') from error
        return target
