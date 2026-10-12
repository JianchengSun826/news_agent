from __future__ import annotations


import pytest

from newsroom.batch_release import BatchReleaseWorkflow
from newsroom.service import WorkflowError
from newsroom.workspace import BatchWorkspace
from test_batch import newsroom, prepared_batch, content_batch
from test_batch_release import all_pass_batch, ready_batch, image_plan, write_plan, snapshot


def downloaded_result(batch, name='downloaded.json'):
    request = batch.room.db.one("SELECT * FROM batch_requests WHERE batch_id=? AND request_type='CONTENT'", (batch.batch_id,))
    archive = batch.room.root / request['result_path']
    inbox = BatchWorkspace(batch.room).batch_root(batch.batch_id) / 'claude/inbox'
    downloaded = inbox / name
    downloaded.write_bytes(archive.read_bytes())
    return downloaded, archive


@pytest.mark.parametrize('images', [True, False])
def test_export_creates_clean_completed_folder_and_removes_process_materials(all_pass_batch, tmp_path, images):
    batch = all_pass_batch
    plan = image_plan(batch, tmp_path)
    if not images:
        plan['items'] = [{'article_id': a, 'no_image': True, 'reason': '无需图片'} for a in batch.article_ids]
    release = BatchReleaseWorkflow(batch.room)
    release.apply_image_plan(batch.batch_id, write_plan(tmp_path / 'images.json', plan))
    downloaded, archive = downloaded_result(batch)
    expected_bytes = {}
    for article_id in batch.article_ids:
        article = batch.room.get_article(article_id)
        expected_bytes[(article_id, 'article.md')] = (batch.room.root / article['draft_path']).read_bytes()
        if images:
            image = batch.room.db.one('SELECT image_path FROM images WHERE image_id=?', (article['image_id'],))
            expected_bytes[(article_id, 'hero.jpg')] = (batch.room.root / image['image_path']).read_bytes()
    target = release.export(batch.batch_id, 'editor', 'checked')
    assert target == batch.room.root / 'workspace/completed' / batch.batch_id
    for article_id in batch.article_ids:
        article = batch.room.get_article(article_id)
        delivery = target / article['slug']
        package = BatchWorkspace(batch.room).batch_root(batch.batch_id) / 'publish' / article_id
        expected = {'article.md', 'hero.jpg', 'image-license.json'} if images else {'article.md'}
        assert {p.name for p in delivery.iterdir()} == expected
        for name in expected - {'image-license.json'}:
            assert (delivery / name).read_bytes() == expected_bytes[(article_id, name)]
        assert (batch.room.root / article['draft_path']).is_file()
        assert article['research_brief_path'] is None
        assert article['fact_check_path'] is None
    assert not downloaded.exists()
    assert not archive.exists()
    assert (target / '.newsroom-export.json').is_file()
    root_readme = (batch.room.root / 'workspace/README.md').read_text()
    completed_section = root_readme.split('## 已完成', 1)[1]
    assert f'completed/{batch.batch_id}/' in completed_section
    assert batch.batch_id not in root_readme.split('## 已完成', 1)[0]
    assert not BatchWorkspace(batch.room).batch_root(batch.batch_id).exists()


@pytest.mark.parametrize('kind', ['unknown', 'changed', 'symlink', 'unverified_archive'])
def test_cleanup_removes_process_files_and_refuses_symlinks(ready_batch, tmp_path, kind):
    batch = ready_batch
    downloaded, archive = downloaded_result(batch)
    if kind == 'unknown':
        downloaded.write_text('{"my_notes": "keep"}')
    elif kind == 'changed':
        downloaded.write_bytes(downloaded.read_bytes() + b'\n')
    elif kind == 'symlink':
        external = tmp_path / 'external.json'
        external.write_bytes(archive.read_bytes())
        downloaded.unlink()
        downloaded.symlink_to(external)
    else:
        with batch.room.db.connect() as connection:
            connection.execute("UPDATE batch_requests SET result_sha256=? WHERE batch_id=? AND request_type='CONTENT'",
                ('0' * 64, batch.batch_id))
    before = downloaded.read_bytes()
    if kind == 'symlink':
        with pytest.raises(WorkflowError, match='symlink'):
            BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
        assert downloaded.read_bytes() == before
        assert external.read_bytes() == before
    else:
        BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
        assert not downloaded.exists()


def test_cleanup_runs_only_after_successful_delivery(ready_batch, monkeypatch):
    batch = ready_batch
    downloaded, archive = downloaded_result(batch)
    from newsroom.delivery import CompletedDelivery
    monkeypatch.setattr(CompletedDelivery, 'build', lambda *args: (_ for _ in ()).throw(OSError('delivery disk error')))
    with pytest.raises(OSError, match='delivery disk error'):
        BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    assert downloaded.exists()
    assert archive.exists()


@pytest.mark.parametrize('mutation', ['edit', 'extra_file', 'extra_directory', 'symlink', 'bad_receipt'])
def test_reexport_preserves_user_changes(ready_batch, mutation, tmp_path):
    batch = ready_batch
    release = BatchReleaseWorkflow(batch.room)
    target = release.export(batch.batch_id, 'editor', 'checked')
    article = batch.room.get_article(batch.article_ids[0])
    file = target / article['slug'] / 'article.md'
    if mutation == 'edit':
        file.write_text('my final edits')
    elif mutation == 'extra_file':
        (target / 'notes.txt').write_text('keep')
    elif mutation == 'extra_directory':
        (target / 'my-empty-folder').mkdir()
    elif mutation == 'symlink':
        file.unlink()
        file.symlink_to(tmp_path / 'external')
    else:
        (target / '.newsroom-export.json').write_text('{}')
    before = snapshot(batch)
    with pytest.raises(WorkflowError):
        release.export(batch.batch_id, 'editor', 'retry')
    assert snapshot(batch) == before
    if mutation == 'edit':
        assert file.read_text() == 'my final edits'


@pytest.mark.parametrize('parent', ['completed', 'batch'])
def test_delivery_rejects_symlink_destinations_before_approval(ready_batch, tmp_path, parent):
    batch = ready_batch
    completed = batch.room.root / 'workspace/completed'
    if parent == 'completed':
        completed.symlink_to(tmp_path, target_is_directory=True)
    else:
        completed.mkdir()
        (completed / batch.batch_id).symlink_to(tmp_path, target_is_directory=True)
    before = snapshot(batch)
    with pytest.raises(WorkflowError):
        BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    assert snapshot(batch) == before


def test_migration_preflight_preserves_completed_delivery_paths(ready_batch):
    from newsroom.migration import WorkspaceMigrator
    batch = ready_batch
    target = BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    plan = WorkspaceMigrator(batch.room).preflight()
    assert not plan.blocking_errors
    relative = target.relative_to(batch.room.root)
    entries = [entry for entry in plan.entries if entry.source.is_relative_to(relative)]
    assert entries
    assert all(entry.target == entry.source for entry in entries)


@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_delivery_promotion_failure_preserves_previous_files(ready_batch, monkeypatch, error_type):
    from newsroom import delivery
    batch = ready_batch
    release = BatchReleaseWorkflow(batch.room)
    from newsroom.delivery import CompletedDelivery
    release.review(batch.batch_id, 'editor', 'checked')
    release.approve(batch.batch_id, 'editor', 'checked')
    publish = release.build_packages(batch.batch_id)
    articles = [batch.room.get_article(a) for a in batch.article_ids]
    target = CompletedDelivery(batch.room).build(batch.batch_id, articles, publish)
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    original = delivery.os.replace
    def fail_after_move(source, destination):
        original(source, destination)
        if '.staging-completed-' in str(source) and destination == target:
            raise error_type('delivery promotion interrupted')
    monkeypatch.setattr(delivery.os, 'replace', fail_after_move)
    with pytest.raises(error_type, match='delivery promotion interrupted'):
        CompletedDelivery(batch.room).build(batch.batch_id, articles, publish)
    assert {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()} == before


def test_delivery_rejects_file_parent_before_approval(ready_batch):
    batch = ready_batch
    (batch.room.root / 'workspace/completed').write_text('user file')
    before = snapshot(batch)
    with pytest.raises(WorkflowError):
        BatchReleaseWorkflow(batch.room).export(batch.batch_id, 'editor', 'checked')
    assert snapshot(batch) == before
