"""Local operation lifetime locks; the migration JSON barrier survives crashes."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path
from typing import Iterator

from .service import WorkflowError


_exclusive_roots: ContextVar[frozenset[Path]] = ContextVar('newsroom_exclusive_locks', default=frozenset())


_shared_roots: ContextVar[frozenset[Path]] = ContextVar('newsroom_shared_locks', default=frozenset())


def _open_lock(root: Path) -> int:
    path = root / 'data/workspace-operation.lock'
    if path.parent.is_symlink():
        raise WorkflowError('operation lock directory must not be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)


def initialize_operation_lock(root: Path) -> None:
    """Install a stable lock inode during init, before read-only operations."""
    os.close(_open_lock(root))


@contextmanager
def operation_lock(root: Path | str, *, exclusive: bool = False) -> Iterator[None]:
    root = Path(root).resolve()
    if root in _exclusive_roots.get():
        yield
        return
    if not exclusive:
        from .migration import assert_migration_access
        assert_migration_access(root)
        if root in _shared_roots.get():
            yield  # Hidden CLI dispatch reuses its operation's shared lock.
            return
    elif root in _shared_roots.get():
        raise WorkflowError('active newsroom operation; finish or cancel it and retry migration')
    descriptor = _open_lock(root)
    token = None
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if exclusive:
                message = 'active CLI or Finder operation; finish or cancel it and retry migration'
            else:
                message = 'migration operation active; retry when complete; if interrupted inspect migration recovery journal'
            raise WorkflowError(message) from error
        if not exclusive:
            # Check under the lock as well as before creating/opening its file.
            assert_migration_access(root)
            token = _shared_roots.set(_shared_roots.get() | {root})
        else:
            token = _exclusive_roots.set(_exclusive_roots.get() | {root})
        yield
    finally:
        if token is not None:
            (_exclusive_roots if exclusive else _shared_roots).reset(token)
        os.close(descriptor)  # Closing releases flock even on interruption.
