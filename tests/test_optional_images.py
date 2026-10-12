from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsroom.batch_release import BatchReleaseWorkflow
from newsroom.cli import app
from newsroom.macos import MacOSBatchController
from newsroom.service import WorkflowError
from newsroom.workspace import BatchWorkspace
from test_batch import newsroom, prepared_batch, content_batch
from test_batch_release import all_pass_batch, image_plan, write_plan, snapshot
from test_macos import FakeDialogs


@pytest.mark.parametrize('mixed', [False, True])
def test_explicit_no_image_plan_packages_and_publishes(all_pass_batch, tmp_path, mixed):
    batch = all_pass_batch
    payload = image_plan(batch, tmp_path)
    for index, item in enumerate(payload['items']):
        if not mixed or index == 0:
            payload['items'][index] = {'article_id': item['article_id'], 'no_image': True, 'reason': '无需配图'}
    release = BatchReleaseWorkflow(batch.room)
    release.apply_image_plan(batch.batch_id, write_plan(tmp_path / 'plan.json', payload))
    state = BatchWorkspace(batch.room).snapshot(batch.batch_id)
    assert state['next_action'] == 'export'
    assert next(item for item in state['items'] if item['article_id'] == batch.article_ids[0])['image_state'] == 'NO_IMAGE'
    release.review(batch.batch_id, 'editor', 'checked')
    release.approve(batch.batch_id, 'editor', 'approved')
    publish = release.build_packages(batch.batch_id)
    package = publish / batch.article_ids[0]
    assert not list(package.glob('hero.*'))
    assert not (package / 'image-license.json').exists()
    assert not (package / 'image-brief.md').exists()
    assert 'no image' in (package / 'publication-checklist.md').read_text().lower()
    assert json.loads((package / 'metadata.json').read_text())['image_id'] is None
    if mixed:
        assert (publish / batch.article_ids[1] / 'hero.jpg').is_file()
    urls = {'schema_version': 1, 'batch_id': batch.batch_id, 'items': [
        {'article_id': article_id, 'article_url': f'https://example.com/{article_id}'} for article_id in batch.article_ids
    ]}
    release.register_publications(batch.batch_id, write_plan(tmp_path / 'urls.json', urls), 'editor')
    assert batch.workflow.status(batch.batch_id)['status'] == 'PUBLISHED'


@pytest.mark.parametrize('selection', [
    {'no_image': False, 'reason': 'none'},
    {'no_image': True, 'reason': ' '},
    {'no_image': 1, 'reason': 'none'},
    {'no_image': True, 'reason': 'none', 'image_candidate_id': 'imgcand-extra'},
])
def test_invalid_no_image_choice_is_atomic(all_pass_batch, tmp_path, selection):
    payload = image_plan(all_pass_batch, tmp_path)
    payload['items'][0] = {'article_id': all_pass_batch.article_ids[0], **selection}
    before = snapshot(all_pass_batch)
    with pytest.raises(WorkflowError, match='invalid image plan'):
        BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / 'plan.json', payload))
    assert snapshot(all_pass_batch) == before
    assert not list((all_pass_batch.room.root / 'workspace').rglob('hero.*'))


def test_unselected_image_is_not_implicitly_optional(all_pass_batch):
    with pytest.raises(WorkflowError, match='IMAGE_READY'):
        BatchReleaseWorkflow(all_pass_batch.room).review(all_pass_batch.batch_id, 'editor', 'checked')


def test_finder_can_choose_no_image_without_candidates(all_pass_batch):
    with all_pass_batch.room.db.connect() as connection:
        connection.execute('DELETE FROM image_candidates')
    dialogs = FakeDialogs()
    controller = MacOSBatchController(all_pass_batch.room, dialogs)
    assert controller.run(all_pass_batch.batch_id, 'register-images') == 0
    assert not dialogs.errors
    assert not dialogs.pickers
    assert all(all_pass_batch.room.get_article(a)['status'] == 'IMAGE_READY' for a in all_pass_batch.article_ids)


def test_candidate_supplement_preserves_drafts_checks_and_original_result(all_pass_batch, tmp_path):
    batch = all_pass_batch
    articles = [batch.room.get_article(a) for a in batch.article_ids]
    original = batch.room.root / batch.room.db.one("SELECT result_path FROM batch_requests WHERE batch_id=? AND request_type='CONTENT'", (batch.batch_id,))['result_path']
    raw = original.read_bytes()
    candidate = json.loads(raw)['items'][0]['image_candidates'][0]
    candidate['landing_page_url'] = 'https://example.com/new-photo'
    payload = {'schema_version': 1, 'batch_id': batch.batch_id, 'items': [
        {'article_id': batch.article_ids[0], 'image_candidates': [candidate]}
    ]}
    result = CliRunner().invoke(app, ['--root', str(batch.room.root), 'batch', 'image-candidates-import', batch.batch_id,
        str(write_plan(tmp_path / 'candidates.json', payload)), '--actor', 'editor', '--reason', 'rights verified'])
    assert result.exit_code == 0, result.output
    assert [batch.room.get_article(a) for a in batch.article_ids] == articles
    assert original.read_bytes() == raw
    assert any(c['landing_page_url'] == 'https://example.com/new-photo' for c in batch.room.list_image_candidates(batch.article_ids[0]))
    before = snapshot(batch)
    payload['items'].append({'article_id': 'art-foreign', 'image_candidates': [candidate]})
    result = CliRunner().invoke(app, ['--root', str(batch.room.root), 'batch', 'image-candidates-import', batch.batch_id,
        str(write_plan(tmp_path / 'bad.json', payload)), '--actor', 'editor', '--reason', 'checked'])
    assert result.exit_code == 2
    assert snapshot(batch) == before


def test_archive_retains_material_and_unarchive_restores_visibility(prepared_batch):
    batch = prepared_batch
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    runner = CliRunner()
    args = ['--root', str(batch.room.root), 'batch']
    result = runner.invoke(app, [*args, 'archive', batch.batch_id, '--actor', 'editor', '--reason', 'completed test'])
    assert result.exit_code == 0, result.output
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    index = (batch.room.root / 'workspace/README.md').read_text()
    assert '已归档批次' in index
    assert batch.batch_id not in index.split('## 已归档批次')[0]
    result = runner.invoke(app, [*args, 'unarchive', batch.batch_id, '--actor', 'editor', '--reason', 'resume'])
    assert result.exit_code == 0, result.output
    assert batch.batch_id in (batch.room.root / 'workspace/README.md').read_text().split('## 已归档批次')[0]


def test_request_finder_failure_still_provides_upload_path(prepared_batch):
    class UnavailableFinder(FakeDialogs):
        def reveal_file(self, path):
            raise OSError('Finder -1728')
    dialogs = UnavailableFinder()
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'open-next-request') == 0
    assert str(prepared_batch.request_path) in dialogs.alerts[-1][1]


def test_import_prints_success_and_cancel_feedback(prepared_batch, tmp_path, capsys):
    from test_batch import write_content_result
    path = write_content_result(prepared_batch, tmp_path / 'content.json')
    controller = MacOSBatchController(prepared_batch.room, FakeDialogs(files=[path], confirms=[False]))
    assert controller.run(prepared_batch.batch_id, 'import-result') == 0
    assert '取消' in capsys.readouterr().out
    controller = MacOSBatchController(prepared_batch.room, FakeDialogs(files=[path]))
    assert controller.run(prepared_batch.batch_id, 'import-result') == 0
    output = capsys.readouterr().out
    assert '导入成功' in output and '2' in output and 'WAITING_FACT_CHECK' in output


def test_no_image_package_survives_verified_migration(all_pass_batch, tmp_path):
    from newsroom.migration import WorkspaceMigrator, assert_migration_access
    batch = all_pass_batch
    release = BatchReleaseWorkflow(batch.room)
    payload = {'batch_id': batch.batch_id, 'items': [
        {'article_id': a, 'no_image': True, 'reason': 'text news'} for a in batch.article_ids
    ]}
    release.apply_image_plan(batch.batch_id, write_plan(tmp_path / 'plan.json', payload))
    release.review(batch.batch_id, 'editor', 'checked')
    release.approve(batch.batch_id, 'editor', 'approved')
    release.build_packages(batch.batch_id)
    service = WorkspaceMigrator(batch.room)
    plan = service.preflight()
    assert not plan.blocking_errors
    backup = service.backup(plan)
    service.execute(plan, backup)
    assert_migration_access(batch.room.root)
    assert batch.workflow.status(batch.batch_id)['status'] == 'PACKAGED'


def test_missing_material_diagnostic_identifies_batch_and_path(all_pass_batch):
    from newsroom.migration import WorkspaceMigrator
    article = all_pass_batch.room.get_article(all_pass_batch.article_ids[0])
    path = all_pass_batch.room.root / article['research_brief_path']
    path.unlink()
    with pytest.raises(WorkflowError) as captured:
        WorkspaceMigrator(all_pass_batch.room)._validate_materials(all_pass_batch.room, terminal=True)
    assert article['research_brief_path'] in str(captured.value)
    assert 'restore' in str(captured.value).lower()


def test_no_image_plan_database_failure_rolls_back_all_choices(all_pass_batch, tmp_path, monkeypatch):
    batch = all_pass_batch
    payload = {'batch_id': batch.batch_id, 'items': [
        {'article_id': a, 'no_image': True, 'reason': 'no photo'} for a in batch.article_ids
    ]}
    before = snapshot(batch)
    original = batch.room._transition
    def fail_second(connection, entity_type, entity_id, *args):
        if entity_type == 'image_choice' and entity_id == batch.article_ids[1]:
            raise RuntimeError('interrupted transaction')
        original(connection, entity_type, entity_id, *args)
    monkeypatch.setattr(batch.room, '_transition', fail_second)
    with pytest.raises(RuntimeError, match='interrupted'):
        BatchReleaseWorkflow(batch.room).apply_image_plan(batch.batch_id, write_plan(tmp_path / 'plan.json', payload))
    assert snapshot(batch) == before


def test_no_image_choice_cannot_authorize_new_revision(all_pass_batch, tmp_path):
    batch = all_pass_batch
    payload = {'batch_id': batch.batch_id, 'items': [
        {'article_id': a, 'no_image': True, 'reason': 'no photo'} for a in batch.article_ids
    ]}
    release = BatchReleaseWorkflow(batch.room)
    release.apply_image_plan(batch.batch_id, write_plan(tmp_path / 'plan.json', payload))
    with batch.room.db.connect() as connection:
        connection.execute('UPDATE articles SET revision_number=2 WHERE article_id=?', (batch.article_ids[0],))
    with pytest.raises(WorkflowError, match='current-version no-image'):
        release.review(batch.batch_id, 'editor', 'checked')


@pytest.fixture
def shared_pending_batch(prepared_batch, tmp_path):
    from test_batch import write_content_result, write_fact_check_result
    batch = prepared_batch
    second_csv = tmp_path / 'second.csv'
    source = BatchWorkspace(batch.room).source_csv_path(batch.batch_id)
    second_csv.write_bytes(source.read_bytes() + b'\n')
    second = batch.workflow.prepare(second_csv)['batch_id']
    result = batch.workflow.import_content(batch.batch_id, write_content_result(batch, tmp_path / 'content.json'))
    batch.fact_request = json.loads(Path(result['fact_check_request_path']).read_bytes())
    batch.workflow.import_fact_checks(batch.batch_id, write_fact_check_result(batch, tmp_path / 'checks.json', ['PASS', 'PASS']))
    return batch, second


def test_no_image_plan_refreshes_owner_when_requested_by_shared_batch(shared_pending_batch, tmp_path):
    batch, second = shared_pending_batch
    payload = {'batch_id': second, 'items': [
        {'article_id': a, 'no_image': True, 'reason': 'no photo'} for a in batch.article_ids
    ]}
    BatchReleaseWorkflow(batch.room).apply_image_plan(second, write_plan(tmp_path / 'plan.json', payload))
    owner_state = json.loads((BatchWorkspace(batch.room).batch_root(batch.batch_id) / 'batch-state.json').read_bytes())
    assert all(item['image_state'] == 'NO_IMAGE' for item in owner_state['items'])
    assert owner_state['next_action'] == 'export'


def test_supplement_candidate_records_canonical_owner(shared_pending_batch, tmp_path):
    batch, second = shared_pending_batch
    candidate = json.loads((batch.room.root / batch.room.db.one("SELECT result_path FROM batch_requests WHERE batch_id=? AND request_type='CONTENT'", (batch.batch_id,))['result_path']).read_bytes())['items'][0]['image_candidates'][0]
    candidate['landing_page_url'] = 'https://example.com/supplement'
    payload = {'batch_id': second, 'items': [{'article_id': batch.article_ids[0], 'image_candidates': [candidate]}]}
    BatchReleaseWorkflow(batch.room).import_image_candidates(second, write_plan(tmp_path / 'supplement.json', payload), 'editor', 'rights checked')
    stored = next(c for c in batch.room.list_image_candidates(batch.article_ids[0]) if c['landing_page_url'] == 'https://example.com/supplement')
    assert stored['batch_id'] == batch.batch_id


def test_approval_rejects_stale_no_image_decision_atomically(all_pass_batch, tmp_path):
    batch = all_pass_batch
    release = BatchReleaseWorkflow(batch.room)
    payload = {'batch_id': batch.batch_id, 'items': [
        {'article_id': a, 'no_image': True, 'reason': 'text only'} for a in batch.article_ids
    ]}
    release.apply_image_plan(batch.batch_id, write_plan(tmp_path / 'plan.json', payload))
    release.review(batch.batch_id, 'editor', 'checked')
    with batch.room.db.connect() as connection:
        connection.execute('UPDATE articles SET revision_number=2 WHERE article_id=?', (batch.article_ids[0],))
    before = snapshot(batch)
    with pytest.raises(WorkflowError, match='current-version no-image'):
        release.approve(batch.batch_id, 'editor', 'approved')
    assert snapshot(batch) == before
