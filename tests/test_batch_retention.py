from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from newsroom.batch import BatchWorkflow
from newsroom.batch_release import BatchReleaseWorkflow
from newsroom.delivery import CompletedDelivery
from newsroom.service import WorkflowError
from newsroom.workspace import BatchWorkspace
from test_batch import newsroom, prepared_batch, content_batch, candidate_row, write_batch_csv
from test_batch_release import all_pass_batch, ready_batch, image_plan, write_plan


def staged_delivery(batch):
    release = BatchReleaseWorkflow(batch.room)
    release.review(batch.batch_id, 'editor', 'checked')
    release.approve(batch.batch_id, 'editor', 'checked')
    publish = release.build_packages(batch.batch_id)
    articles = [batch.room.get_article(a) for a in batch.article_ids]
    return CompletedDelivery(batch.room).build(batch.batch_id, articles, publish)


def finalizer(batch):
    from newsroom.retention import BatchRetention
    return BatchRetention(batch.room)


def test_finalization_removes_process_tree_and_preserves_final_bytes(ready_batch):
    batch = ready_batch
    target = staged_delivery(batch)
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    requests = batch.room.db.all('SELECT request_id,result_sha256 FROM batch_requests WHERE batch_id=?', (batch.batch_id,))
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    (root / 'custom-notes.txt').write_text('process notes')
    finalizer(batch).finalize(batch.batch_id, 'editor', 'checked')
    assert not root.exists()
    assert before == {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    for article_id in batch.article_ids:
        article = batch.room.get_article(article_id)
        assert article['draft_path'] == f'workspace/completed/{batch.batch_id}/{article["slug"]}/article.md'
        assert article['research_brief_path'] is None
        assert article['fact_check_path'] is None
        image = batch.room.db.one('SELECT * FROM images WHERE image_id=?', (article['image_id'],))
        assert image['image_path'] == f'workspace/completed/{batch.batch_id}/{article["slug"]}/hero.jpg'
    assert batch.room.db.all('SELECT request_id,result_sha256 FROM batch_requests WHERE batch_id=?', (batch.batch_id,)) == requests
    assert all(row['request_path'] == '' and row['result_path'] is None for row in batch.room.db.all('SELECT * FROM batch_requests'))
    assert finalizer(batch).record(batch.batch_id)['state'] == 'FINAL_ONLY'


def test_no_image_finalization(all_pass_batch, tmp_path):
    batch = all_pass_batch
    release = BatchReleaseWorkflow(batch.room)
    plan = {'schema_version': 1, 'batch_id': batch.batch_id, 'items': [
        {'article_id': a, 'no_image': True, 'reason': 'no image'} for a in batch.article_ids]}
    release.apply_image_plan(batch.batch_id, write_plan(tmp_path / 'plan.json', plan))
    target = staged_delivery(batch)
    finalizer(batch).finalize(batch.batch_id, 'editor', 'checked')
    assert len(list(target.rglob('article.md'))) == 2
    assert not list(target.rglob('hero.*'))
    assert all(batch.room.no_image_selected(batch.room.get_article(a)) for a in batch.article_ids)


@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_cleanup_resumes_after_deletion_failure(ready_batch, monkeypatch, error_type):
    batch = ready_batch
    target = staged_delivery(batch)
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    original = Path.unlink
    def failing(path, *args, **kwargs):
        if path == root / 'input/candidates.csv':
            raise error_type('disk error')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', failing)
        with pytest.raises((WorkflowError, KeyboardInterrupt)):
            finalizer(batch).finalize(batch.batch_id, 'editor', 'checked')
    assert finalizer(batch).record(batch.batch_id)['state'] == 'CLEANING'
    assert all((batch.room.root / batch.room.get_article(a)['draft_path']).is_file() for a in batch.article_ids)
    assert finalizer(batch).finalize(batch.batch_id, 'editor', 'retry') == target
    assert not root.exists()


@pytest.mark.parametrize('mutation', ['symlink', 'foreign_reference', 'changed_delivery', 'pending_request'])
def test_unsafe_cleanup_does_not_switch_paths_or_delete_files(ready_batch, tmp_path, mutation):
    batch = ready_batch
    target = staged_delivery(batch)
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    if mutation == 'symlink':
        external = tmp_path / 'external.txt'
        external.write_text('keep')
        (root / 'unsafe').symlink_to(external)
    elif mutation == 'foreign_reference':
        with batch.room.db.connect() as connection:
            connection.execute("UPDATE articles SET owning_batch_id=NULL WHERE article_id=?", (batch.article_ids[0],))
    elif mutation == 'changed_delivery':
        next(target.rglob('article.md')).write_text('editor changes')
    else:
        with batch.room.db.connect() as connection:
            connection.execute("UPDATE batch_requests SET status='PENDING' WHERE batch_id=? AND request_type='CONTENT'", (batch.batch_id,))
    before = batch.room.db.all('SELECT * FROM articles')
    with pytest.raises(WorkflowError):
        finalizer(batch).finalize(batch.batch_id, 'editor', 'checked')
    assert batch.room.db.all('SELECT * FROM articles') == before
    assert root.is_dir()
    assert finalizer(batch).record(batch.batch_id) is None


def test_unexpected_file_stops_resumed_cleanup(ready_batch, monkeypatch):
    batch = ready_batch
    staged_delivery(batch)
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path == root / 'input/candidates.csv':
            raise OSError('disk error')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(WorkflowError):
            finalizer(batch).finalize(batch.batch_id, 'editor', 'checked')
    unexpected = root / 'new.txt'
    unexpected.write_text('keep new file')
    with pytest.raises(WorkflowError):
        finalizer(batch).finalize(batch.batch_id, 'editor', 'retry')
    assert unexpected.read_text() == 'keep new file'


def test_export_cleanup_refresh_and_retry_are_idempotent(ready_batch):
    batch = ready_batch
    release = BatchReleaseWorkflow(batch.room)
    target = release.export(batch.batch_id, 'editor', 'checked')
    workspace = BatchWorkspace(batch.room)
    assert not workspace.batch_root(batch.batch_id).exists()
    before = batch.room.db.all("SELECT * FROM state_transitions WHERE entity_type='article'")
    state = workspace.refresh(batch.batch_id)
    assert state['next_action'] == 'complete'
    assert state['retention_state'] == 'FINAL_ONLY'
    assert release.export(batch.batch_id, 'editor', 'retry') == target
    assert before == batch.room.db.all("SELECT * FROM state_transitions WHERE entity_type='article'")
    assert not workspace.batch_root(batch.batch_id).exists()
    assert '历史记录' not in (batch.room.root / 'workspace/README.md').read_text()
    assert BatchWorkflow(batch.room).status(batch.batch_id)['content_request_path'] is None


def test_closed_batch_rejects_process_recreation(ready_batch, tmp_path):
    batch = ready_batch
    BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    workspace = BatchWorkspace(batch.room)
    workflow = BatchWorkflow(batch.room)
    for action in (
        lambda: workspace.ensure_layout(batch.batch_id),
        lambda: workflow.import_content(batch.batch_id, tmp_path / 'absent.json'),
        lambda: workflow.import_fact_checks(batch.batch_id, tmp_path / 'absent.json'),
        lambda: workflow.import_revisions(batch.batch_id, tmp_path / 'absent.json'),
        lambda: workflow.create_revision_request(batch.batch_id),
        lambda: BatchReleaseWorkflow(batch.room).build_packages(batch.batch_id),
    ):
        with pytest.raises(WorkflowError, match='清理'):
            action()
    assert not workspace.batch_root(batch.batch_id).exists()


def test_cleanup_preserves_event_metadata_and_new_csv_dedup(ready_batch, tmp_path):
    batch = ready_batch
    room = batch.room
    columns = 'article_id,candidate_id,event_id,event_date,company,product,event_type,core_fact,keywords'
    before = room.db.all(f'SELECT {columns} FROM articles ORDER BY article_id')
    candidates_before = room.db.all('SELECT * FROM candidates ORDER BY candidate_id')
    probe = room.add_candidate(**{
        **{k:v for k,v in room.get_candidate(candidates_before[0]['candidate_id']).items() if k in {'event_date','candidate_title','core_fact','company','product','topic','subtopic','event_type','keywords','primary_source','secondary_sources','discovered_from','priority'}},
        'primary_source': 'https://example.com/new-probe', 'candidate_title': 'same event probe'})
    expected = room.check_duplicate(probe['candidate_id'])
    BatchReleaseWorkflow(room).export(batch.batch_id, 'editor', 'checked')
    assert room.check_duplicate(probe['candidate_id']) == expected
    assert room.db.all(f'SELECT {columns} FROM articles ORDER BY article_id') == before
    assert room.db.all('SELECT * FROM candidates WHERE candidate_id<>? ORDER BY candidate_id', (probe['candidate_id'],)) == candidates_before
    source = tmp_path / 'same.csv'
    # Recreate the original exact bytes from the still-external import source.
    source.write_bytes((tmp_path / 'content.csv').read_bytes())
    assert BatchWorkflow(room).prepare(source)['batch_id'] == batch.batch_id
    rows = list(__import__('csv').DictReader(source.open()))
    rows[0]['priority'] = '1' if rows[0]['priority'] != '1' else '2'
    changed = write_batch_csv(tmp_path / 'new.csv', rows)
    result = BatchWorkflow(room).prepare(changed)
    assert result['counts'].get('REUSED_COMPLETE') == len(batch.article_ids)
    state = BatchWorkspace(room).snapshot(result['batch_id'])
    assert all('completed/' in i['article_path'] for i in state['items'] if i['article_id'])
    assert not BatchWorkspace(room).batch_root(batch.batch_id).exists()


def test_migration_checks_final_only_without_recreating_process_tree(ready_batch):
    from newsroom.migration import WorkspaceMigrator
    batch = ready_batch
    BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    migrator = WorkspaceMigrator(batch.room)
    assert not migrator.preflight().blocking_errors
    migrator._validate_materials(batch.room, terminal=True)
    migrator._validate_materials(batch.room)
    assert not BatchWorkspace(batch.room).batch_root(batch.batch_id).exists()


def test_optional_publication_registration_keeps_final_only_batch(ready_batch, tmp_path):
    from typer.testing import CliRunner
    from newsroom.cli import app
    batch = ready_batch
    release = BatchReleaseWorkflow(batch.room)
    target = release.export(batch.batch_id, 'editor', 'checked')
    result = write_plan(tmp_path / 'urls.json', {'schema_version': 1, 'batch_id': batch.batch_id,
        'items': [{'article_id': a, 'article_url': f'https://news.example/{a}'} for a in batch.article_ids]})
    assert release.register_publications(batch.batch_id, result, 'editor')['published'] == 2
    assert release.export(batch.batch_id, 'editor', 'open again') == target
    cli = CliRunner().invoke(app, ['--root', str(batch.room.root), 'batch', 'export', batch.batch_id,
        '--actor', 'editor', '--reason', 'open'])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output)['status'] == 'PUBLISHED'
    assert not BatchWorkspace(batch.room).batch_root(batch.batch_id).exists()


def test_export_requires_schema_upgrade_before_approving(ready_batch):
    batch = ready_batch
    with batch.room.db.connect() as connection:
        connection.execute('DROP TABLE batch_retention')
        connection.execute('DELETE FROM schema_version WHERE version=5')
    before = batch.room.db.all('SELECT * FROM articles')
    with pytest.raises(WorkflowError, match='init'):
        BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    assert batch.room.db.all('SELECT * FROM articles') == before


def test_export_cleanup_retry_does_not_duplicate_approvals(ready_batch, monkeypatch):
    batch = ready_batch
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path == root / 'input/candidates.csv':
            raise OSError('disk error')
        return original(path, *args, **kwargs)
    release = BatchReleaseWorkflow(batch.room)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(WorkflowError, match='清理未完成'):
            release.export(batch.batch_id, 'editor', 'checked')
    assert BatchWorkspace(batch.room).snapshot(batch.batch_id)['retention_state'] == 'CLEANING'
    from newsroom.migration import WorkspaceMigrator
    WorkspaceMigrator(batch.room)._validate_materials(batch.room, terminal=True)
    approvals = batch.room.db.all("SELECT * FROM state_transitions WHERE entity_type='article'")
    release.export(batch.batch_id, 'editor', 'retry')
    assert batch.room.db.all("SELECT * FROM state_transitions WHERE entity_type='article'") == approvals
    assert not root.exists()


@pytest.mark.parametrize('mutation', ['draft_missing', 'hero_missing', 'fake_reference', 'fake_receipt'])
def test_final_only_validation_detects_damaged_delivery(ready_batch, mutation):
    batch = ready_batch
    target = BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    article = batch.room.get_article(batch.article_ids[0])
    if mutation == 'draft_missing':
        (target / article['slug'] / 'article.md').unlink()
    elif mutation == 'hero_missing':
        (target / article['slug'] / 'hero.jpg').unlink()
    elif mutation == 'fake_reference':
        with batch.room.db.connect() as connection:
            connection.execute('UPDATE articles SET draft_path=? WHERE article_id=?', ('workspace/completed/foreign/article.md', article['article_id']))
    else:
        file = target / article['slug'] / 'article.md'
        file.write_text('tampered')
        receipt = json.loads((target / '.newsroom-export.json').read_text())
        receipt['files'][f'{article["slug"]}/article.md'] = hashlib.sha256(file.read_bytes()).hexdigest()
        (target / '.newsroom-export.json').write_text(json.dumps(receipt))
    from newsroom.migration import WorkspaceMigrator
    with pytest.raises(WorkflowError):
        WorkspaceMigrator(batch.room)._validate_materials(batch.room, terminal=True)
    assert not BatchWorkspace(batch.room).batch_root(batch.batch_id).exists()


@pytest.mark.parametrize('cleanup_first', [True, False])
def test_verified_migration_and_cleanup_work_in_either_order(ready_batch, cleanup_first):
    from newsroom.migration import WorkspaceMigrator, assert_migration_access
    batch = ready_batch
    release = BatchReleaseWorkflow(batch.room)
    if cleanup_first:
        release.export(batch.batch_id, 'editor', 'checked')
    migrator = WorkspaceMigrator(batch.room)
    plan = migrator.preflight()
    assert not plan.blocking_errors
    backup = migrator.backup(plan)
    migrator.execute(plan, backup)
    if not cleanup_first:
        release.export(batch.batch_id, 'editor', 'checked')
    assert_migration_access(batch.room.root)
    assert release.export(batch.batch_id, 'editor', 'open').is_dir()
    assert not BatchWorkspace(batch.room).batch_root(batch.batch_id).exists()


def test_transaction_failure_keeps_process_paths_and_materials(ready_batch, monkeypatch):
    batch = ready_batch
    staged_delivery(batch)
    before = batch.room.db.all('SELECT * FROM articles')
    original = batch.room._transition
    def fail(connection, entity_type, *args, **kwargs):
        if entity_type == 'batch_retention':
            raise OSError('audit write failed')
        return original(connection, entity_type, *args, **kwargs)
    monkeypatch.setattr(batch.room, '_transition', fail)
    with pytest.raises(OSError, match='audit write failed'):
        finalizer(batch).finalize(batch.batch_id, 'editor', 'checked')
    assert batch.room.db.all('SELECT * FROM articles') == before
    assert finalizer(batch).record(batch.batch_id) is None
    assert BatchWorkspace(batch.room).batch_root(batch.batch_id).is_dir()
    assert all((batch.room.root / row['draft_path']).is_file() for row in before)


def test_internal_artifact_helpers_cannot_recreate_cleaned_materials(ready_batch, tmp_path):
    batch = ready_batch
    BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    source = tmp_path / 'manual.md'
    source.write_text('new draft')
    article_id = batch.article_ids[0]
    for action in (
        lambda: batch.room.register_research(article_id, source),
        lambda: batch.room.register_article(article_id, source, 'title', 'slug'),
        lambda: batch.room.register_fact_check(article_id, source),
        lambda: batch.room.create_publication_package(article_id),
    ):
        with pytest.raises(WorkflowError, match='清理'):
            action()
    assert not any((batch.room.root / name).exists() for name in ('research', 'articles', 'output'))


def test_cleanup_preserves_active_other_batch_and_external_originals(ready_batch, tmp_path):
    batch = ready_batch
    second = BatchWorkflow(batch.room).prepare(write_batch_csv(tmp_path / 'next.csv', [candidate_row(
        company='Third', product='Future', core_fact='Third launched Future on 2026-10-05.',
        primary_source='https://third.example/future', keywords='third|future')]))
    other_root = BatchWorkspace(batch.room).batch_root(second['batch_id'])
    other_bytes = {p.relative_to(other_root): p.read_bytes() for p in other_root.rglob('*') if p.is_file()}
    external_sources = {p: p.read_bytes() for p in tmp_path.glob('download-*.JPG')}
    external_sources[tmp_path / 'content.csv'] = (tmp_path / 'content.csv').read_bytes()
    BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    assert other_bytes == {p.relative_to(other_root): p.read_bytes() for p in other_root.rglob('*') if p.is_file()}
    assert all(p.read_bytes() == expected for p, expected in external_sources.items())


def test_unreadable_process_directory_blocks_cleanup_inventory(ready_batch, monkeypatch):
    import os
    batch = ready_batch
    staged_delivery(batch)
    blocked = BatchWorkspace(batch.room).batch_root(batch.batch_id) / 'claude/inbox'
    original = os.scandir
    def unreadable(path):
        if Path(path) == blocked:
            raise PermissionError('cannot read inbox')
        return original(path)
    monkeypatch.setattr(os, 'scandir', unreadable)
    with pytest.raises((OSError, WorkflowError), match='cannot read inbox'):
        finalizer(batch).preflight(batch.batch_id)


@pytest.mark.parametrize("migrated", [False, True])
def test_manual_final_edits_do_not_block_normal_access_after_migration(ready_batch, tmp_path, migrated):
    from newsroom.migration import WorkspaceMigrator
    from newsroom.cli import app
    from typer.testing import CliRunner
    batch = ready_batch
    if migrated:
        migrator = WorkspaceMigrator(batch.room)
        plan = migrator.preflight()
        migrator.execute(plan, migrator.backup(plan))
    release = BatchReleaseWorkflow(batch.room)
    target = release.export(batch.batch_id, 'editor', 'checked')
    article = batch.room.get_article(batch.article_ids[0])
    final_article = target / article['slug'] / 'article.md'
    final_article.write_text('editor final changes')
    (target / 'publishing-notes.txt').write_text('manual publishing notes')
    runner = CliRunner()
    for args in (['stats'], ['batch','status',batch.batch_id], ['batch','refresh-workspace',batch.batch_id]):
        result = runner.invoke(app, ['--root', str(batch.room.root), *args])
        assert result.exit_code == 0, result.output
    urls = write_plan(tmp_path / 'urls.json', {'schema_version': 1, 'batch_id': batch.batch_id,
        'items': [{'article_id': a, 'article_url': f'https://news.example/{a}'} for a in batch.article_ids]})
    result = runner.invoke(app, ['--root', str(batch.room.root), 'batch','publish-register',batch.batch_id,str(urls),'--actor','editor'])
    assert result.exit_code == 0, result.output
    with pytest.raises(WorkflowError):
        release.export(batch.batch_id, 'editor', 'retry must not overwrite edits')
    assert final_article.read_text() == 'editor final changes'
    assert not BatchWorkspace(batch.room).batch_root(batch.batch_id).exists()


def test_added_recovery_notes_do_not_block_status_of_interrupted_cleanup(ready_batch, monkeypatch):
    from newsroom.migration import WorkspaceMigrator
    from newsroom.cli import app
    from typer.testing import CliRunner
    batch = ready_batch
    migrator = WorkspaceMigrator(batch.room)
    plan = migrator.preflight()
    migrator.execute(plan, migrator.backup(plan))
    root = BatchWorkspace(batch.room).batch_root(batch.batch_id)
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path == root / 'input/candidates.csv':
            raise OSError('disk error')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(WorkflowError, match='清理未完成'):
            BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    note = root / 'recovery-note.txt'
    note.write_text('keep this new file')
    result = CliRunner().invoke(app, ['--root',str(batch.room.root),'batch','status',batch.batch_id])
    assert result.exit_code == 0, result.output
    with pytest.raises(WorkflowError):
        BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'retry')
    assert note.read_text() == 'keep this new file'
