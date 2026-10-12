from __future__ import annotations

import fcntl
import os

import pytest

from newsroom.batch_release import BatchReleaseWorkflow
from newsroom.coordination import operation_lock
from newsroom.service import WorkflowError
from test_batch import newsroom, prepared_batch, content_batch
from test_batch_release import all_pass_batch, ready_batch


def test_nested_exclusive_operation_and_shared_reads_are_reentrant(newsroom):
    with operation_lock(newsroom.root, exclusive=True):
        with operation_lock(newsroom.root, exclusive=True):
            with operation_lock(newsroom.root):
                assert newsroom.db.table_names()


def test_export_refuses_other_active_operation_before_mutation(ready_batch):
    batch = ready_batch
    before = batch.room.db.all('SELECT * FROM articles')
    descriptor = os.open(batch.room.root / 'data/workspace-operation.lock', os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(WorkflowError, match='active'):
            BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    finally:
        os.close(descriptor)
    assert batch.room.db.all('SELECT * FROM articles') == before
