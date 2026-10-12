from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from newsroom.batch_release import BatchReleaseWorkflow
from newsroom.cli import app
from newsroom.macos import MacOSBatchController
from newsroom.service import WorkflowError
from newsroom.workspace import BatchWorkspace
from test_batch import newsroom, prepared_batch, content_batch
from test_batch_release import all_pass_batch, ready_batch, image_plan, write_plan, snapshot
from test_macos import FakeDialogs


@pytest.mark.parametrize('start', ['IMAGE_READY', 'REVIEWED', 'APPROVED', 'PACKAGED'])
def test_export_continues_legacy_states_without_duplicate_approvals(ready_batch, start):
    batch = ready_batch
    release = BatchReleaseWorkflow(batch.room)
    if start != 'IMAGE_READY':
        release.review(batch.batch_id, 'editor', 'checked')
    if start in {'APPROVED', 'PACKAGED'}:
        release.approve(batch.batch_id, 'editor', 'approved')
    if start == 'PACKAGED':
        release.build_packages(batch.batch_id)
    target = release.export(batch.batch_id, 'editor', '确认并导出')
    assert (target / '.newsroom-export.json').is_file()
    assert BatchWorkspace(batch.room).snapshot(batch.batch_id)['next_action'] == 'complete'
    for article_id in batch.article_ids:
        article = batch.room.get_article(article_id)
        assert article['status'] == 'APPROVED'
        assert article['article_url'] is None
        assert (target / article['slug'] / 'article.md').is_file()
        assert (target / article['slug'] / 'hero.jpg').is_file()
        assert not BatchWorkspace(batch.room).batch_root(batch.batch_id).exists()
        transitions = [a for a in batch.room.audit(article_id) if a['to_status'] in {'REVIEWED', 'APPROVED'}]
        assert len(transitions) == 2
    before = snapshot(batch)
    release.export(batch.batch_id, 'editor', '再次导出')
    assert snapshot(batch)['state_transitions'] == before['state_transitions']


@pytest.mark.parametrize('mutation', ['missing_file', 'failed_check', 'mixed_status', 'missing_image', 'symlink'])
def test_export_preflight_leaves_all_states_unchanged(ready_batch, tmp_path, mutation):
    batch = ready_batch
    article_id = batch.article_ids[1]
    if mutation == 'missing_file':
        (batch.room.root / batch.room.get_article(article_id)['draft_path']).unlink()
    elif mutation == 'symlink':
        publish = BatchWorkspace(batch.room).batch_root(batch.batch_id) / 'publish'
        publish.rmdir()
        publish.symlink_to(tmp_path, target_is_directory=True)
    elif mutation == 'missing_image':
        image = batch.room.db.one('SELECT image_path FROM images WHERE article_id=?', (article_id,))
        (batch.room.root / image['image_path']).unlink()
    else:
        with batch.room.db.connect() as connection:
            if mutation == 'failed_check':
                connection.execute("UPDATE articles SET fact_check_outcome='FAIL' WHERE article_id=?", (article_id,))
            else:
                connection.execute("UPDATE articles SET status='REVIEWED' WHERE article_id=?", (article_id,))
    before = snapshot(batch)
    with pytest.raises(WorkflowError):
        BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    assert snapshot(batch) == before


def test_export_supports_explicit_no_image(all_pass_batch, tmp_path):
    batch = all_pass_batch
    payload = {'schema_version': 1, 'batch_id': batch.batch_id, 'items': [
        {'article_id': a, 'no_image': True, 'reason': '无图导出'} for a in batch.article_ids
    ]}
    release = BatchReleaseWorkflow(batch.room)
    release.apply_image_plan(batch.batch_id, write_plan(tmp_path / 'no-images.json', payload))
    target = release.export(batch.batch_id, 'editor', 'checked')
    assert not list(target.rglob('hero.*'))
    assert all((target / batch.room.get_article(a)['slug'] / 'article.md').is_file() for a in batch.article_ids)


def test_finder_export_confirms_once_and_opens_output(ready_batch):
    dialogs = FakeDialogs()
    controller = MacOSBatchController(ready_batch.room, dialogs)
    assert controller.run(ready_batch.batch_id, 'export') == 0
    assert len(dialogs.confirmations) == 1
    assert BatchWorkspace(ready_batch.room).completed_root(ready_batch.batch_id) in dialogs.opened
    assert any('导出完成' in message for _, message in dialogs.alerts)


def test_cancel_export_changes_nothing(ready_batch):
    before = snapshot(ready_batch)
    dialogs = FakeDialogs(confirms=[False])
    assert MacOSBatchController(ready_batch.room, dialogs).run(ready_batch.batch_id, 'export') == 0
    assert snapshot(ready_batch) == before
    assert not dialogs.opened


def test_refresh_replaces_old_launchers_and_preserves_custom_files(ready_batch):
    workspace = BatchWorkspace(ready_batch.room)
    root = workspace.batch_root(ready_batch.batch_id)
    actions = root / 'actions'
    old_names = ['05-完成整批审核.command', '06-批准整个批次.command', '07-生成全部发布包.command', '08-刷新批次视图.command']
    for name in old_names:
        (actions / name).write_text('legacy launcher')
    (actions / 'my-notes.txt').write_text('keep me')
    state = workspace.refresh(ready_batch.batch_id)
    assert state['next_action'] == 'export'
    assert len(list(actions.glob('*.command'))) == 4
    assert (actions / '05-确认并导出.command').is_file()
    assert all(not (actions / name).exists() for name in old_names)
    assert (actions / 'my-notes.txt').read_text() == 'keep me'


def test_export_cli(ready_batch):
    result = CliRunner().invoke(app, ['--root', str(ready_batch.room.root), 'batch', 'export',
        ready_batch.batch_id, '--actor', 'editor', '--reason', '确认并导出'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['status'] == 'PACKAGED'


def test_export_retries_package_failure_without_duplicate_audit(ready_batch, monkeypatch):
    release = BatchReleaseWorkflow(ready_batch.room)
    original = release.room._write_publication_package
    def fail(*args):
        raise OSError('disk error')
    monkeypatch.setattr(release.room, '_write_publication_package', fail)
    with pytest.raises(OSError, match='disk error'):
        release.export(ready_batch.batch_id, 'editor', 'checked')
    assert all(ready_batch.room.get_article(a)['status'] == 'APPROVED' for a in ready_batch.article_ids)
    monkeypatch.setattr(release.room, '_write_publication_package', original)
    release.export(ready_batch.batch_id, 'editor', 'retry')
    for article_id in ready_batch.article_ids:
        assert len([a for a in ready_batch.room.audit(article_id) if a['to_status'] in {'REVIEWED', 'APPROVED'}]) == 2
