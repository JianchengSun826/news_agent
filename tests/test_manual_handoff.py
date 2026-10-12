from __future__ import annotations

import json

from newsroom.macos import MacOSBatchController
from newsroom.workspace import BatchWorkspace
from test_batch import newsroom, prepared_batch, content_batch, write_fact_check_result
from test_macos import FakeDialogs, db_snapshot


def current_request(batch):
    return batch.room.db.one("SELECT * FROM batch_requests WHERE batch_id=? AND status='PENDING'", (batch.batch_id,))


def assert_manual_handoff(batch, kind):
    workspace = BatchWorkspace(batch.room)
    state = workspace.snapshot(batch.batch_id)
    request = current_request(batch)
    assert request['request_type'] == kind
    assert state['next_action'] == 'upload-request'
    readme = (workspace.batch_root(batch.batch_id) / 'README.md').read_text()
    current = readme.split('## 当前 Claude 任务\n', 1)[1].split('\n## ', 1)[0]
    assert request['request_id'] in current
    assert request['expected_result_filename'] in current
    assert 'claude/requests/' in current
    assert 'instructions' in current
    assert '执行附件任务' in current
    if kind == 'FACT_CHECK':
        assert '全新对话' in current
    payload = json.loads((batch.room.root / request['request_path']).read_text())
    assert 'Execute this request' in payload['instructions']
    assert 'output_file' in payload['instructions']
    assert {item['article_id'] for item in payload['items']}


def test_content_handoff_is_self_contained_and_readme_replaces_launcher(prepared_batch):
    batch = prepared_batch
    workspace = BatchWorkspace(batch.room)
    root = workspace.batch_root(batch.batch_id)
    old = root / 'actions/01-打开下一个Claude任务.command'
    old.write_text('old launcher')
    before = db_snapshot(batch)
    archived_request = batch.request_path.read_bytes()
    workspace.refresh(batch.batch_id)
    assert not old.exists()
    assert len(list((root / 'actions').glob('*.command'))) == 4
    assert batch.request_path.read_bytes() == archived_request
    assert db_snapshot(batch) == before
    assert_manual_handoff(batch, 'CONTENT')


def test_fact_check_handoff_requires_fresh_conversation(content_batch):
    assert_manual_handoff(content_batch, 'FACT_CHECK')


def test_revision_handoff_points_to_latest_request(content_batch, tmp_path):
    batch = content_batch
    path = write_fact_check_result(batch, tmp_path / 'failed.json', ['FAIL', 'PASS'])
    batch.workflow.import_fact_checks(batch.batch_id, path)
    batch.workflow.create_revision_request(batch.batch_id)
    assert_manual_handoff(batch, 'REVISION')


def test_missing_revision_readme_explains_how_to_create_request(content_batch, tmp_path):
    batch = content_batch
    path = write_fact_check_result(batch, tmp_path / 'failed.json', ['FAIL', 'PASS'])
    batch.workflow.import_fact_checks(batch.batch_id, path)
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    current = (root / 'README.md').read_text().split('## 当前 Claude 任务\n', 1)[1].split('\n## ', 1)[0]
    assert 'batch revision-request' in current


def test_open_batch_only_opens_folder_and_readme(prepared_batch):
    batch = prepared_batch
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    before = db_snapshot(batch)
    dialogs = FakeDialogs()
    assert MacOSBatchController(batch.room, dialogs).run(batch.batch_id, 'open-batch') == 0
    assert dialogs.opened == [root, root / 'README.md']
    assert not dialogs.revealed
    assert db_snapshot(batch) == before
