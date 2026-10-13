"""Clean final deliverables and conservative cleanup of archived duplicates."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .service import Newsroom, WorkflowError
from .workspace import BatchWorkspace, _identifier


RECEIPT = '.newsroom-export.json'


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    batch_id: str
    files: dict[str, str]

    @field_validator('batch_id')
    @classmethod
    def valid_batch(cls, value: str) -> str:
        return _identifier(value)

    @field_validator('files')
    @classmethod
    def valid_files(cls, values: dict[str, str]) -> dict[str, str]:
        import re
        if not values or any(
            not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*/(?:article\.md|image-guide\.md|hero\.[a-z0-9]+|image-license\.json)', name)
            or not re.fullmatch(r'[a-f0-9]{64}', digest)
            for name, digest in values.items()
        ):
            raise ValueError('invalid delivery file inventory')
        return values


class CompletedDelivery:
    def __init__(self, room: Newsroom):
        self.room = room
        self.workspace = BatchWorkspace(room)

    def _safe_path(self, path: Path) -> None:
        if not path.is_relative_to(self.room.root):
            raise WorkflowError('delivery path must remain within the newsroom')
        for part in (path, *path.parents):
            if part.is_symlink():
                raise WorkflowError('delivery path cannot contain symlinks')
            if part != path and part.exists() and not part.is_dir():
                raise WorkflowError('delivery parent path must be a directory')
            if part == self.room.root:
                break

    @staticmethod
    def _digest(path: Path) -> str:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def _inventory(self, target: Path) -> tuple[dict[str, str], set[str]]:
        files: dict[str, str] = {}
        directories: set[str] = set()
        for path in target.rglob('*'):
            self._safe_path(path)
            name = path.relative_to(target).as_posix()
            if path.is_dir():
                directories.add(name)
            elif stat.S_ISREG(path.stat().st_mode):
                if name != RECEIPT:
                    files[name] = self._digest(path)
            else:
                raise WorkflowError('delivery contains a nonregular file')
        return files, directories

    def validate_target(self, batch_id: str) -> Path:
        target = self.workspace.completed_root(batch_id)
        self._safe_path(target)
        if not target.exists():
            return target
        try:
            if not target.is_dir():
                raise ValueError('not a directory')
            marker = target / RECEIPT
            self._safe_path(marker)
            receipt = DeliveryReceipt.model_validate_json(marker.read_bytes())
            files, directories = self._inventory(target)
            if receipt.batch_id != batch_id or files != receipt.files or directories != {name.split('/')[0] for name in files}:
                raise ValueError('delivery was changed')
        except (OSError, ValueError, ValidationError) as error:
            raise WorkflowError(f'完成目录有人工修改或缺少有效导出记录，已保留原文件；请另存或移走该目录后重试：{target}') from error
        return target

    def build(self, batch_id: str, articles: list[dict[str, Any]], publish: Path) -> Path:
        target = self.validate_target(batch_id)
        sources: dict[str, Path] = {}
        for article in articles:
            slug = _identifier(article['slug'])
            package = publish / _identifier(article['article_id'])
            names = ['article.md']
            if (package / 'image-guide.md').is_file():
                names.append('image-guide.md')
            elif article['owning_batch_id']:
                raise WorkflowError('delivery source is missing image-guide.md')
            if article['image_id']:
                image = self.room.db.one('SELECT image_path FROM images WHERE image_id=?', (article['image_id'],))
                if image is None:
                    raise WorkflowError('registered delivery image is missing')
                names += ['hero' + Path(image['image_path']).suffix.lower(), 'image-license.json']
            for name in names:
                destination = f'{slug}/{name}'
                if destination in sources:
                    raise WorkflowError('delivery slugs must be unique')
                source = package / name
                self._safe_path(source)
                if not source.is_file():
                    raise WorkflowError(f'delivery source is missing: {source}')
                sources[destination] = source
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.staging-completed-', dir=target.parent))
        previous: Path | None = None
        promoted = False
        try:
            for name, source in sources.items():
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                if destination.read_bytes() != source.read_bytes():
                    raise WorkflowError('incomplete staged delivery')
            files, _ = self._inventory(staging)
            receipt = DeliveryReceipt(batch_id=batch_id, files=files)
            (staging / RECEIPT).write_text(receipt.model_dump_json(indent=2) + '\n', encoding='utf-8')
            self.validate_target(batch_id)
            if target.exists():
                previous = Path(tempfile.mkdtemp(prefix='.previous-completed-', dir=target.parent)) / batch_id
                os.replace(target, previous)
            os.replace(staging, target)
            promoted = True
        except BaseException:
            # A rename can succeed then raise: inspect paths before restoration.
            if not staging.exists() and target.exists():
                shutil.rmtree(target)
            if previous is not None and previous.exists():
                os.replace(previous, target)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if previous is not None and previous.parent.exists():
                if promoted:
                    shutil.rmtree(previous.parent)
                elif not previous.exists():
                    previous.parent.rmdir()
        return target

    def clean_inbox(self, batch_id: str) -> int:
        """Delete only byte-identical copies of hash-verified archived results."""
        inbox = self.workspace.batch_root(batch_id) / 'claude/inbox'
        self._safe_path(inbox)
        verified: set[str] = set()
        for request in self.room.db.all("SELECT result_path,result_sha256 FROM batch_requests WHERE batch_id=? AND status='COMPLETED'", (batch_id,)):
            if not request['result_path'] or not request['result_sha256']:
                continue
            archive = self.room.root / request['result_path']
            try:
                self._safe_path(archive)
                if archive.is_file() and self._digest(archive) == request['result_sha256']:
                    verified.add(request['result_sha256'])
            except (OSError, WorkflowError):
                continue
        removed = 0
        for path in inbox.glob('*.json'):
            if path.is_symlink() or not path.is_file():
                continue
            before = path.stat()
            digest = self._digest(path)
            after = path.stat()
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)
            if digest in verified and identity(after) == identity(before):
                path.unlink()
                removed += 1
        return removed
