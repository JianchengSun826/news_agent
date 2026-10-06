from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from newsroom.cli import app
from newsroom.migration import WorkspaceMigrator
from newsroom.service import Newsroom, WorkflowError
from test_batch import candidate_row, write_batch_csv, newsroom, prepared_batch, content_batch
from test_batch_release import all_pass_batch, ready_batch, approved_batch


def migrator(room):
    return WorkspaceMigrator(room)


@pytest.fixture
def legacy_workspace(tmp_path):
    room = Newsroom(tmp_path)
    room.init()
    from newsroom.batch import BatchWorkflow
    csv = write_batch_csv(tmp_path / 'original.csv', [candidate_row()])
    batch_id = BatchWorkflow(room).prepare(csv)['batch_id']
    article = room.db.one('SELECT * FROM articles WHERE owning_batch_id=?', (batch_id,))
    article_id = article['article_id']
    request = room.db.one('SELECT * FROM batch_requests WHERE batch_id=?', (batch_id,))
    request_path = Path('output/batches') / batch_id / f"{request['request_id']}.json"
    (tmp_path / request_path).parent.mkdir(parents=True)
    shutil.copyfile(tmp_path / request['request_path'], tmp_path / request_path)
    draft = Path('articles') / article_id / 'article-v2.md'
    (tmp_path / draft).parent.mkdir(parents=True)
    (tmp_path / draft).write_bytes(b'draft\r\nexact bytes\n')
    with room.db.connect() as connection:
        connection.execute('UPDATE articles SET draft_path=?,revision_number=2 WHERE article_id=?', (draft.as_posix(), article_id))
        connection.execute('UPDATE batch_requests SET request_path=? WHERE request_id=?', (request_path.as_posix(), request['request_id']))
    shutil.rmtree(tmp_path / 'workspace')
    (tmp_path / 'research/orphan-notes.md').write_text('unclaimed')
    candidate = candidate_row(company='Legacy', product='Old', core_fact='Legacy introduced Old.', primary_source='https://legacy.example/old')
    candidate['keywords'] = ['legacy']
    candidate['secondary_sources'] = []
    candidate['priority'] = 3
    candidate = room.add_candidate(**candidate)
    room.check_duplicate(candidate['candidate_id'])
    room.select_candidate(candidate['candidate_id'], 'editor', 'legacy')
    single = room.create_brief(candidate['candidate_id'])
    csv.unlink()
    return SimpleNamespace(room=room, batch_id=batch_id, article_id=article_id, single_id=single['article_id'], source_sha256=hashlib.sha256(write_batch_csv(tmp_path / 'selected.csv', [candidate_row()]).read_bytes()).hexdigest(), draft=draft, request=request)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_preflight_maps_claimed_unclaimed_and_legacy_files(legacy_workspace):
    f = legacy_workspace
    before = snapshot(f.room.root)
    plan = migrator(f.room).preflight()
    assert plan.blocking_errors == []
    draft = next(e for e in plan.claimed if e.database_field == 'draft_path')
    assert draft.source == f.draft
    assert draft.target == Path(f'workspace/batches/{f.batch_id}/articles/{f.article_id}/article-v2.md')
    assert draft.size == 19
    assert draft.sha256 == hashlib.sha256(b'draft\r\nexact bytes\n').hexdigest()
    assert any(e.source.name == 'orphan-notes.md' for e in plan.unclaimed)
    assert any(e.target.parts[-3] == 'legacy-single-articles' for e in plan.legacy)
    assert next(e for e in plan.claimed if e.database_field == 'request_path').target.name == 'content-request.json'
    assert snapshot(f.room.root) == before
    assert plan.to_dict() == migrator(f.room).preflight().to_dict()
    assert str(f.room.root) not in json.dumps(plan.to_dict())


def test_missing_original_csv_preserves_metadata_without_blocking(legacy_workspace):
    f = legacy_workspace
    plan = migrator(f.room).preflight()
    assert plan.blocking_errors == []
    assert plan.source_metadata[f.batch_id]['available'] is False
    assert plan.source_metadata[f.batch_id]['source_sha256'] == f.source_sha256


def test_selected_csv_must_match_recorded_hash(legacy_workspace, tmp_path):
    wrong = tmp_path / 'wrong.csv'
    wrong.write_text('different')
    plan = migrator(legacy_workspace.room).preflight([wrong])
    assert any('does not match any recorded batch SHA-256' in e for e in plan.blocking_errors)


def test_selected_csv_exact_bytes_and_duplicate_hash_block(legacy_workspace, tmp_path):
    f = legacy_workspace
    csv = tmp_path / 'selected.csv'
    plan = migrator(f.room).preflight([csv])
    assert plan.blocking_errors == []
    assert plan.source_metadata[f.batch_id]['available'] is True
    entry = next(e for e in plan.claimed if e.target.name == 'candidates.csv')
    assert entry.sha256 == f.source_sha256
    other = tmp_path / 'also.csv'
    other.write_bytes(csv.read_bytes())
    assert any('multiple selected CSVs' in e for e in migrator(f.room).preflight([csv, other]).blocking_errors)


def test_preflight_missing_database_reference_blocks(legacy_workspace):
    f = legacy_workspace
    (f.room.root / f.draft).unlink()
    plan = migrator(f.room).preflight()
    assert any('missing referenced file' in e and 'draft_path' in e for e in plan.blocking_errors)
    with pytest.raises(WorkflowError, match='blocking'):
        migrator(f.room).backup(plan)
    assert not (f.room.root / 'data/backups').exists()


@pytest.mark.parametrize('unsafe', ['../outside.md', '/outside.md'])
def test_preflight_path_escape_blocks(legacy_workspace, unsafe):
    f = legacy_workspace
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET draft_path=? WHERE article_id=?', (unsafe, f.article_id))
    assert any('unsafe' in e for e in migrator(f.room).preflight().blocking_errors)


def test_preflight_symlink_blocks_even_inside_repository(legacy_workspace):
    f = legacy_workspace
    path = f.room.root / f.draft
    path.unlink()
    path.symlink_to(f.room.root / 'research/orphan-notes.md')
    assert any('symlink' in e for e in migrator(f.room).preflight().blocking_errors)


def test_preflight_canonical_source_equals_target_is_valid(legacy_workspace):
    f = legacy_workspace
    target = Path(f'workspace/batches/{f.batch_id}/articles/{f.article_id}/article-v2.md')
    (f.room.root / target).parent.mkdir(parents=True)
    shutil.move(f.room.root / f.draft, f.room.root / target)
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET draft_path=? WHERE article_id=?', (target.as_posix(), f.article_id))
    plan = migrator(f.room).preflight()
    assert plan.blocking_errors == []
    entry = next(e for e in plan.claimed if e.database_field == 'draft_path')
    assert entry.source == entry.target


def test_preflight_conflicting_canonical_destination_blocks(legacy_workspace):
    f = legacy_workspace
    target = f.room.root / f'workspace/batches/{f.batch_id}/articles/{f.article_id}/article-v2.md'
    target.parent.mkdir(parents=True)
    target.write_text('different')
    assert any('collision' in e for e in migrator(f.room).preflight().blocking_errors)


def test_preflight_casefold_destinations_block(legacy_workspace):
    f = legacy_workspace
    # Different legacy roots also exercise collisions on case-insensitive macOS.
    path = f.room.root / f'research/{f.article_id}/ARTICLE-v2.md'
    path.parent.mkdir(parents=True)
    path.write_text('case collision')
    assert any('collision' in e for e in migrator(f.room).preflight().blocking_errors)


def test_preflight_inventories_images_prompts_packages_and_history(legacy_workspace):
    f = legacy_workspace
    files = {
        f'images/{f.article_id}/hero.JPG': b'image',
        f'output/prompts/{f.article_id}-image.md': b'prompt',
        f'output/{f.article_id}/article.md': b'package',
        f'research/{f.article_id}/research-brief-v1.md': b'history',
        'output/unknown-package/metadata.json': b'orphan package',
        f'output/batches/{f.batch_id}/extra.json': b'extra',
    }
    for relative, content in files.items():
        path = f.room.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    with f.room.db.connect() as connection:
        connection.execute('INSERT INTO images(image_id,article_id,image_type,image_path,prompt_path) VALUES (?,?,?,?,?)', ('img-test', f.article_id, 'AI', f'images/{f.article_id}/hero.JPG', f'output/prompts/{f.article_id}-image.md'))
    plan = migrator(f.room).preflight()
    assert plan.blocking_errors == []
    entries = plan.claimed + plan.legacy + plan.unclaimed
    assert set(files) <= {e.source.as_posix() for e in entries}
    assert next(e for e in entries if e.database_field == 'image_path').target.name == 'hero.jpg'
    assert next(e for e in entries if e.source.name == 'research-brief-v1.md').target.name == 'research-brief-v1.md'
    assert next(e for e in entries if e.source.as_posix() == f'output/{f.article_id}/article.md').target == Path(f'workspace/batches/{f.batch_id}/publish/{f.article_id}/article.md')


def test_preflight_reused_complete_does_not_duplicate_article(legacy_workspace):
    f = legacy_workspace
    with f.room.db.connect() as connection:
        connection.execute("INSERT INTO batches(batch_id,source_filename,source_sha256,status,created_at,updated_at) VALUES ('batch-later','later.csv','later','COMPLETE','now','now')")
        connection.execute("INSERT INTO batch_items SELECT 'batch-later',candidate_id,article_id,'REUSED_COMPLETE',duplicate_classification,content_version,error_message,created_at,updated_at FROM batch_items WHERE batch_id=?", (f.batch_id,))
    plan = migrator(f.room).preflight()
    assert plan.blocking_errors == []
    assert not any('batch-later/articles' in e.target.as_posix() for e in plan.claimed)


def test_preflight_ambiguous_article_membership_blocks(legacy_workspace):
    f = legacy_workspace
    with f.room.db.connect() as connection:
        connection.execute("INSERT INTO batches(batch_id,source_filename,source_sha256,status,created_at,updated_at) VALUES ('batch-later','later.csv','later','COMPLETE','now','now')")
        connection.execute("INSERT INTO batch_items SELECT 'batch-later',candidate_id,article_id,'READY_FOR_CONTENT',duplicate_classification,content_version,error_message,created_at,updated_at FROM batch_items WHERE batch_id=?", (f.batch_id,))
    assert any('ambiguous ownership' in e for e in migrator(f.room).preflight().blocking_errors)


def test_preflight_unreadable_database_is_blocking_and_does_not_initialize(tmp_path):
    room = Newsroom(tmp_path)
    plan = migrator(room).preflight()
    assert plan.blocking_errors
    assert not room.db_path.exists()
    room.db_path.parent.mkdir()
    room.db_path.write_bytes(b'not sqlite')
    assert any('database' in e for e in migrator(room).preflight().blocking_errors)


def test_backup_restores_exact_files_and_sqlite(legacy_workspace):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    before = snapshot(f.room.root)
    backup = service.backup(plan)
    with sqlite3.connect(backup / 'newsroom.db') as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('SELECT draft_path FROM articles WHERE article_id=?', (f.article_id,)).fetchone()[0] == f.draft.as_posix()
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['plan'] == plan.to_dict()
    with tarfile.open(backup / 'artifacts.tar.gz') as archive:
        member = archive.extractfile(f'repository/{f.draft.as_posix()}')
        assert member.read() == b'draft\r\nexact bytes\n'
    service.verify_backup(backup)
    after = snapshot(f.room.root)
    assert all(after[p] == content for p, content in before.items())
    assert not (f.room.root / 'workspace').exists()


def test_backup_corruption_is_removed_before_return(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    verify = service._verify_backup_contents
    def corrupt_then_verify(path):
        (path / 'artifacts.tar.gz').write_bytes(b'corrupt archive')
        verify(path)
    monkeypatch.setattr(service, '_verify_backup_contents', corrupt_then_verify)
    with pytest.raises(WorkflowError, match='backup verification'):
        service.backup(service.preflight())
    assert not list((f.room.root / 'data/backups').glob('workspace-migration-*'))
    assert not (f.room.root / 'workspace').exists()


def test_backup_rejects_changed_source(legacy_workspace):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    (f.room.root / f.draft).write_text('changed after review')
    with pytest.raises(WorkflowError, match='changed'):
        service.backup(plan)
    assert not list((f.room.root / 'data/backups').glob('workspace-migration-*'))


def test_cli_preflight_saves_deterministic_report_without_workflow_mutation(legacy_workspace):
    f = legacy_workspace
    before = snapshot(f.room.root)
    result = CliRunner().invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--preflight'])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    report = f.room.root / payload['report_path']
    assert json.loads(report.read_text()) == {k: v for k, v in payload.items() if k != 'report_path'}
    assert all(snapshot(f.room.root)[p] == content for p, content in before.items())
    assert not (f.room.root / 'workspace').exists()
    second = CliRunner().invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--preflight'])
    assert second.output == result.output


def test_backup_selected_csv_survives_relative_serialization(legacy_workspace):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight([f.room.root / 'selected.csv'])
    backup = service.backup(plan)
    assert json.loads((backup / 'report.json').read_text())['source_metadata'][f.batch_id]['available'] is True
    with tarfile.open(backup / 'artifacts.tar.gz') as archive:
        assert archive.extractfile('repository/selected.csv').read() == (f.room.root / 'selected.csv').read_bytes()


def test_backup_external_csv_is_normalized_and_archived(legacy_workspace, tmp_path):
    f = legacy_workspace
    external = tmp_path.parent / f'{tmp_path.name}-external.csv'
    external.write_bytes((f.room.root / 'selected.csv').read_bytes())
    try:
        service = migrator(f.room)
        plan = service.preflight([external])
        assert plan.blocking_errors == []
        assert plan.selected_source_csvs == [external.absolute()]
        backup = service.backup(plan)
        with tarfile.open(backup / 'artifacts.tar.gz') as archive:
            assert archive.extractfile(f'external-inputs/{f.source_sha256}/{external.name}').read() == external.read_bytes()
        service.verify_backup(backup)
    finally:
        external.unlink()


def test_preflight_unreadable_file_blocks(legacy_workspace):
    f = legacy_workspace
    path = f.room.root / f.draft
    path.chmod(0)
    try:
        assert any('draft_path' in e and 'Permission denied' in e for e in migrator(f.room).preflight().blocking_errors)
    finally:
        path.chmod(0o644)


def test_preflight_directory_symlink_blocks_without_following(legacy_workspace, tmp_path):
    f = legacy_workspace
    outside = tmp_path.parent / f'{tmp_path.name}-outside'
    outside.mkdir()
    (outside / 'private.md').write_text('private')
    (f.room.root / 'research/linked').symlink_to(outside, target_is_directory=True)
    try:
        plan = migrator(f.room).preflight()
        assert any('symlink' in e for e in plan.blocking_errors)
        assert not any(e.source.name == 'private.md' for e in plan.entries)
    finally:
        shutil.rmtree(outside)


def test_preflight_result_paths_map_version_and_superseded_history(legacy_workspace):
    f = legacy_workspace
    paths = {
        'result_path': f'output/batches/{f.batch_id}/old-result.json',
        'request_path': f'output/batches/{f.batch_id}/fact-request.json',
    }
    for path in paths.values():
        (f.room.root / path).write_bytes(b'{}')
    with f.room.db.connect() as connection:
        connection.execute('UPDATE batch_requests SET result_path=?,status=? WHERE request_id=?', (paths['result_path'], 'SUPERSEDED', f.request['request_id']))
        connection.execute("INSERT INTO batch_requests(request_id,batch_id,request_type,revision_number,request_path,expected_result_filename,status,created_at) VALUES ('req-fact',?,'FACT_CHECK',2,?,'fact-result.json','PENDING','now')", (f.batch_id, paths['request_path']))
    plan = migrator(f.room).preflight()
    assert plan.blocking_errors == []
    by_source = {e.source.as_posix(): e for e in plan.claimed}
    historical = next(e for e in plan.claimed if e.source.name == f"{f.request['request_id']}.json")
    assert historical.target == Path(f'workspace/batches/{f.batch_id}/claude/requests/{f.request["request_id"]}/content-request.json')
    assert by_source[paths['request_path']].target.name == 'fact-check-request-v2.json'
    assert by_source[paths['result_path']].target.name == 'content-result.json'


def test_backup_rejects_changed_database(legacy_workspace):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    with f.room.db.connect() as connection:
        connection.execute("UPDATE batches SET status='CHANGED' WHERE batch_id=?", (f.batch_id,))
    with pytest.raises(WorkflowError, match='changed'):
        service.backup(plan)
    assert not (f.room.root / 'data/backups').exists()


@pytest.mark.parametrize('corruption', ['sqlite', 'manifest', 'report', 'artifact', 'traversal', 'symlink', 'duplicate'])
def test_backup_verification_rejects_corruption_and_unsafe_members(legacy_workspace, corruption):
    f = legacy_workspace
    service = migrator(f.room)
    backup = service.backup(service.preflight())
    if corruption == 'sqlite':
        (backup / 'newsroom.db').write_bytes(b'bad sqlite')
    elif corruption == 'manifest':
        manifest = json.loads((backup / 'manifest.json').read_text())
        manifest['artifacts'] = []
        (backup / 'manifest.json').write_text(json.dumps(manifest))
    elif corruption == 'report':
        (backup / 'report.json').write_text('{}')
    else:
        with tarfile.open(backup / 'artifacts.tar.gz') as archive:
            files = [(m, archive.extractfile(m).read()) for m in archive.getmembers()]
        with tarfile.open(backup / 'artifacts.tar.gz', 'w:gz') as archive:
            for index, (member, content) in enumerate(files):
                if index == 0 and corruption == 'artifact':
                    content = b'x' * len(content)
                if index == 0 and corruption == 'traversal':
                    member.name = '../escaped.txt'
                if index == 0 and corruption == 'symlink':
                    member.type = tarfile.SYMTYPE
                    member.linkname = '../escaped.txt'
                    member.size = 0
                import io
                archive.addfile(member, io.BytesIO(content))
            if corruption == 'duplicate':
                archive.addfile(files[0][0], io.BytesIO(files[0][1]))
    with pytest.raises(WorkflowError, match='backup verification'):
        service.verify_backup(backup)
    assert not (backup.parent / 'escaped.txt').exists()
    assert not (f.room.root / 'workspace').exists()


def test_cli_preflight_blocking_report_returns_failure_without_mutation(legacy_workspace):
    f = legacy_workspace
    (f.room.root / f.draft).unlink()
    before = f.room.db_path.read_bytes()
    result = CliRunner().invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--preflight'])
    assert result.exit_code == 2
    assert json.loads(result.output)['blocking_errors']
    assert f.room.db_path.read_bytes() == before
    assert not (f.room.root / 'workspace').exists()


def test_cli_requires_preflight_and_does_not_execute(legacy_workspace):
    f = legacy_workspace
    before = snapshot(f.room.root)
    result = CliRunner().invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate'])
    assert result.exit_code == 2
    assert snapshot(f.room.root) == before


def test_preflight_backup_size_includes_database(legacy_workspace):
    f = legacy_workspace
    plan = migrator(f.room).preflight()
    artifact_size = sum({e.source: e.size for e in plan.entries}.values())
    assert plan.expected_backup_size >= artifact_size + f.room.db_path.stat().st_size


def test_backup_preserves_executable_canonical_actions(legacy_workspace):
    f = legacy_workspace
    action = f.room.root / f'workspace/batches/{f.batch_id}/actions/action.command'
    action.parent.mkdir(parents=True)
    action.write_bytes(b'#!/bin/zsh\n')
    action.chmod(0o755)
    service = migrator(f.room)
    backup = service.backup(service.preflight())
    with tarfile.open(backup / 'artifacts.tar.gz') as archive:
        assert archive.getmember(f'repository/workspace/batches/{f.batch_id}/actions/action.command').mode == 0o755


def test_preflight_does_not_stop_inventory_after_ambiguous_canonical_owner(legacy_workspace):
    f = legacy_workspace
    wrong = f.room.root / f'workspace/batches/batch-wrong/articles/{f.article_id}/article-v1.md'
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(b'wrong owner')
    valid = f.room.root / f'workspace/batches/{f.batch_id}/README.md'
    valid.parent.mkdir(parents=True)
    valid.write_text('dashboard')
    plan = migrator(f.room).preflight()
    assert plan.blocking_errors
    assert valid.relative_to(f.room.root) in {e.source for e in plan.entries}


def test_preflight_path_errors_do_not_leak_absolute_database_paths(legacy_workspace):
    f = legacy_workspace
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET draft_path=? WHERE article_id=?', (str(f.room.root / f.draft), f.article_id))
    plan = migrator(f.room).preflight()
    assert plan.blocking_errors
    assert str(f.room.root) not in json.dumps(plan.to_dict())


def test_backup_plan_includes_uncheckpointed_sqlite_wal(legacy_workspace):
    f = legacy_workspace
    with sqlite3.connect(f.room.db_path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE batches SET status='WAL-STATE' WHERE batch_id=?", (f.batch_id,))
        writer.commit()
        before = snapshot(f.room.root)
        service = migrator(f.room)
        plan = service.preflight()
        assert snapshot(f.room.root) == before
        backup = service.backup(plan)
        with sqlite3.connect(backup / 'newsroom.db') as restored:
            assert restored.execute('SELECT status FROM batches WHERE batch_id=?', (f.batch_id,)).fetchone()[0] == 'WAL-STATE'


def test_preflight_selected_csv_symlink_is_not_hidden_by_normalization(legacy_workspace):
    f = legacy_workspace
    other = f.room.root / 'other'
    other.mkdir()
    (f.room.root / 'csv-alias').symlink_to(other, target_is_directory=True)
    plan = migrator(f.room).preflight([f.room.root / 'csv-alias/../selected.csv'])
    assert any('symlink' in e for e in plan.blocking_errors)


def test_backup_cleanup_failure_marks_incomplete(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    real_remove = Path.rmdir
    def reject_backup_removal(path, *args, **kwargs):
        if Path(path).name == 'blocked-child':
            raise OSError('backup removal unavailable')
        return real_remove(path, *args, **kwargs)
    def fail_verification(path):
        (path / 'blocked-child').mkdir()
        raise WorkflowError('backup verification failed: injected corruption')
    monkeypatch.setattr(Path, 'rmdir', reject_backup_removal)
    monkeypatch.setattr(service, '_verify_backup_contents', fail_verification)
    with pytest.raises(ExceptionGroup) as captured:
        service.backup(service.preflight())
    assert any(isinstance(error, WorkflowError) and 'backup verification' in str(error) for error in exception_tree(captured.value))
    incomplete = list((f.room.root / 'data/backups').glob('workspace-migration-*'))
    assert len(incomplete) == 1
    assert (incomplete[0] / 'INCOMPLETE').is_file()
    assert not (f.room.root / 'workspace').exists()


def test_preflight_canonical_database_path_cannot_belong_to_another_article(legacy_workspace):
    f = legacy_workspace
    source = Path(f'workspace/batches/{f.batch_id}/articles/{f.single_id}/article-v2.md')
    (f.room.root / source).parent.mkdir(parents=True)
    (f.room.root / source).write_bytes(b'B file with no B database reference')
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET owning_batch_id=? WHERE article_id=?', (f.batch_id, f.single_id))
        connection.execute('UPDATE articles SET draft_path=? WHERE article_id=?', (source.as_posix(), f.article_id))
    plan = migrator(f.room).preflight()
    assert any('canonical article' in error and source.as_posix() in error for error in plan.blocking_errors)
    assert str(f.room.root) not in json.dumps(plan.to_dict())


@pytest.mark.parametrize('filename', ['article.md', 'article-v1.md', 'research-brief-v2.md'])
def test_preflight_canonical_draft_filename_and_revision_must_match(legacy_workspace, filename):
    f = legacy_workspace
    source = Path(f'workspace/batches/{f.batch_id}/articles/{f.article_id}/{filename}')
    (f.room.root / source).parent.mkdir(parents=True)
    (f.room.root / source).write_bytes(b'wrong current draft name')
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET draft_path=? WHERE article_id=?', (source.as_posix(), f.article_id))
    plan = migrator(f.room).preflight()
    assert any('canonical filename' in error for error in plan.blocking_errors)
    assert str(f.room.root) not in json.dumps(plan.to_dict())


def test_preflight_retains_previous_fact_check_during_revision(legacy_workspace):
    f = legacy_workspace
    source = Path(f'workspace/batches/{f.batch_id}/articles/{f.article_id}/fact-check-v1.md')
    (f.room.root / source).parent.mkdir(parents=True)
    (f.room.root / source).write_bytes(b'previous failed check')
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET fact_check_path=?,fact_check_outcome=NULL WHERE article_id=?', (source.as_posix(), f.article_id))
    assert migrator(f.room).preflight().blocking_errors == []


def test_preflight_promotes_selected_csv_from_unclaimed_inventory(legacy_workspace):
    f = legacy_workspace
    source = Path('research/original.csv')
    (f.room.root / source).write_bytes((f.room.root / 'selected.csv').read_bytes())
    service = migrator(f.room)
    plan = service.preflight([f.room.root / source])
    assert plan.blocking_errors == []
    assert not any(entry.source == source for entry in plan.unclaimed)
    entries = [entry for entry in plan.entries if entry.source == source]
    assert len(entries) == 1
    assert entries[0].target == Path(f'workspace/batches/{f.batch_id}/input/candidates.csv')
    backup = service.backup(plan)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert len([record for record in manifest['artifacts'] if record['source'] == source.as_posix()]) == 1
    assert len([record for group in ('claimed', 'legacy', 'unclaimed') for record in manifest['plan'][group] if record['source'] == source.as_posix()]) == 1
    with tarfile.open(backup / 'artifacts.tar.gz') as archive:
        assert len([member for member in archive if member.name == 'repository/research/original.csv']) == 1
    service.verify_backup(backup)


@pytest.mark.parametrize('article_kind', ['owned', 'legacy'])
def test_preflight_csv_selection_cannot_repurpose_article_claim(legacy_workspace, article_kind):
    f = legacy_workspace
    source = Path('research/original.csv')
    (f.room.root / source).write_bytes((f.room.root / 'selected.csv').read_bytes())
    article_id = f.article_id if article_kind == 'owned' else f.single_id
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET research_brief_path=? WHERE article_id=?', (source.as_posix(), article_id))
    plan = migrator(f.room).preflight([f.room.root / source])
    assert any('already claimed' in error for error in plan.blocking_errors)
    assert not any(entry.source == source and entry.target.name == 'candidates.csv' for entry in plan.entries)


def filesystem_details(root):
    details = {}
    for path in [root, *root.rglob('*')]:
        metadata = path.stat()
        details[path.relative_to(root).as_posix()] = (
            path.read_bytes() if path.is_file() else None,
            metadata.st_mode,
            metadata.st_mtime_ns,
        )
    return details


@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
def test_preflight_database_isolation_preserves_closed_repository(journal_mode, legacy_workspace):
    f = legacy_workspace
    connection = sqlite3.connect(f.room.db_path)
    connection.execute(f'PRAGMA journal_mode={journal_mode}')
    connection.close()
    assert not Path(str(f.room.db_path) + '-wal').exists()
    assert not Path(str(f.room.db_path) + '-shm').exists()
    before = filesystem_details(f.room.root)
    assert migrator(f.room).preflight().blocking_errors == []
    assert filesystem_details(f.room.root) == before


def test_preflight_database_isolation_preserves_live_sidecars(legacy_workspace):
    f = legacy_workspace
    with sqlite3.connect(f.room.db_path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE batches SET status='WAL-STATE' WHERE batch_id=?", (f.batch_id,))
        writer.commit()
        before = filesystem_details(f.room.root)
        plan = migrator(f.room).preflight()
        assert plan.blocking_errors == []
        assert filesystem_details(f.room.root) == before


@pytest.mark.parametrize('inventory_root', ['workspace', 'images', 'output/prompts', 'output/batches', 'workspace/batches/{batch_id}', 'articles/{article_id}', 'workspace/batches/{batch_id}/claude/requests'])
def test_preflight_inventory_root_must_be_directory(legacy_workspace, inventory_root):
    f = legacy_workspace
    inventory_root = inventory_root.format(batch_id=f.batch_id, article_id=f.article_id)
    path = f.room.root / inventory_root
    if path.is_dir():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'not a directory')
    before = filesystem_details(f.room.root)
    service = migrator(f.room)
    plan = service.preflight()
    assert any('inventory root' in error and inventory_root in error for error in plan.blocking_errors)
    assert str(f.room.root) not in json.dumps(plan.to_dict())
    with pytest.raises(WorkflowError, match='blocking'):
        service.backup(plan)
    assert filesystem_details(f.room.root) == before
    assert not (f.room.root / 'data/backups').exists()


def test_preflight_target_ancestor_file_outside_inventory_blocks(legacy_workspace, monkeypatch):
    f = legacy_workspace
    ancestor = f.room.root / 'other-destinations/blocked'
    ancestor.parent.mkdir()
    ancestor.write_bytes(b'not a directory')
    service = migrator(f.room)
    real_target = service._target
    def target_with_uninventoried_ancestor(source, *args, **kwargs):
        if source == f.draft:
            return Path('other-destinations/blocked/article-v2.md')
        return real_target(source, *args, **kwargs)
    monkeypatch.setattr(service, '_target', target_with_uninventoried_ancestor)
    before = filesystem_details(f.room.root)
    plan = service.preflight()
    assert any('target ancestor' in error and 'other-destinations/blocked' in error for error in plan.blocking_errors)
    assert str(f.room.root) not in json.dumps(plan.to_dict())
    with pytest.raises(WorkflowError, match='blocking'):
        service.backup(plan)
    assert filesystem_details(f.room.root) == before
    assert not (f.room.root / 'data/backups').exists()


def exception_tree(error):
    yield error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            yield from exception_tree(nested)
    if error.__cause__ is not None:
        yield from exception_tree(error.__cause__)


def assert_no_unmarked_backup(room):
    backups = list((room.root / 'data/backups').glob('workspace-migration-*'))
    assert all((path / 'INCOMPLETE').is_file() for path in backups)
    assert not (room.root / 'workspace').exists()
    assert not list(room.root.glob('.workspace-migration-*'))


def test_backup_initial_marker_write_failure_removes_directory(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    original = OSError('initial incomplete marker unavailable')
    real_write = Path.write_text
    writes_before_marker = []
    def fail_marker(path, *args, **kwargs):
        if path.name == 'INCOMPLETE':
            writes_before_marker.extend(path.parent.iterdir())
            raise original
        return real_write(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail_marker)
    with pytest.raises(BaseException) as captured:
        service.backup(plan)
    assert original in list(exception_tree(captured.value))
    assert writes_before_marker == []
    assert not list((f.room.root / 'data/backups').glob('workspace-migration-*'))
    assert_no_unmarked_backup(f.room)


@pytest.mark.parametrize('stage', ['archive', 'verification'])
def test_backup_keyboard_interrupt_preserves_incomplete_lifecycle(legacy_workspace, monkeypatch, stage):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    original = KeyboardInterrupt(f'interrupted {stage}')
    marker_seen = []
    def interrupt(*args, **kwargs):
        backup = next((f.room.root / 'data/backups').glob('workspace-migration-*'))
        marker_seen.append((backup / 'INCOMPLETE').is_file())
        raise original
    if stage == 'archive':
        monkeypatch.setattr(tarfile.TarFile, 'addfile', interrupt)
    else:
        monkeypatch.setattr(service, '_verify_backup_contents', interrupt)
    with pytest.raises(BaseException) as captured:
        service.backup(plan)
    assert original in list(exception_tree(captured.value))
    assert marker_seen == [True]
    assert_no_unmarked_backup(f.room)


def test_backup_preserves_interrupt_cleanup_without_rewriting_marker(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    original = KeyboardInterrupt('original verification interruption')
    cleanup = OSError('backup directory removal unavailable')
    marker = OSError('incomplete marker update unavailable')
    real_remove = Path.rmdir
    real_write = Path.write_text
    marker_writes = []
    def interrupt(path):
        (path / 'blocked-child').mkdir()
        raise original
    def fail_removal(path, *args, **kwargs):
        if Path(path).name == 'blocked-child':
            raise cleanup
        return real_remove(path, *args, **kwargs)
    def fail_marker_update(path, *args, **kwargs):
        if path.name == 'INCOMPLETE':
            marker_writes.append(path)
            if len(marker_writes) > 1:
                raise marker
        return real_write(path, *args, **kwargs)
    monkeypatch.setattr(service, '_verify_backup_contents', interrupt)
    monkeypatch.setattr(Path, 'rmdir', fail_removal)
    monkeypatch.setattr(Path, 'write_text', fail_marker_update)
    with pytest.raises(BaseException) as captured:
        service.backup(plan)
    assert isinstance(captured.value, BaseExceptionGroup)
    errors = list(exception_tree(captured.value))
    assert original in errors and cleanup in errors
    assert marker not in errors
    assert len(marker_writes) == 1
    assert_no_unmarked_backup(f.room)


def test_backup_partial_cleanup_retains_original_marker_without_recreation(legacy_workspace, monkeypatch):
    import os
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    original = KeyboardInterrupt('verification interrupted before cleanup')
    cleanup = OSError('later report removal failed')
    marker_error = OSError('marker recreation prohibited')
    real_unlink = os.unlink
    real_remove = shutil.rmtree
    real_write = Path.write_text
    marker_writes = []
    original_marker = []
    removed_artifacts = []
    def interrupt(path):
        marker = path / 'INCOMPLETE'
        original_marker.append((marker.read_bytes(), marker.stat().st_ino))
        raise original
    def partial_removal(path, *args, **kwargs):
        name = Path(path).name
        if name == 'report.json':
            raise cleanup
        result = real_unlink(path, *args, **kwargs)
        if name in {'manifest.json', 'artifacts.tar.gz'}:
            removed_artifacts.append(name)
        return result
    def prohibit_marker_recreation(path, *args, **kwargs):
        if path.name == 'INCOMPLETE':
            marker_writes.append(path)
            if len(marker_writes) > 1:
                raise marker_error
        return real_write(path, *args, **kwargs)
    def whole_directory_marker_first(path, *args, **kwargs):
        if Path(path).name.startswith('workspace-migration-'):
            # Model a valid rmtree order that deletes the marker before failing.
            (Path(path) / 'INCOMPLETE').unlink()
            (Path(path) / 'artifacts.tar.gz').unlink()
        return real_remove(path, *args, **kwargs)
    monkeypatch.setattr(service, '_verify_backup_contents', interrupt)
    monkeypatch.setattr('newsroom.migration.os.unlink', partial_removal)
    monkeypatch.setattr('newsroom.migration.shutil.rmtree', whole_directory_marker_first)
    monkeypatch.setattr(Path, 'write_text', prohibit_marker_recreation)
    with pytest.raises(BaseException) as captured:
        service.backup(plan)
    errors = list(exception_tree(captured.value))
    assert original in errors and cleanup in errors
    assert removed_artifacts
    backup = next((f.room.root / 'data/backups').glob('workspace-migration-*'))
    assert (backup / 'report.json').is_file()
    marker = backup / 'INCOMPLETE'
    assert marker.is_file()
    assert (marker.read_bytes(), marker.stat().st_ino) == original_marker[0]
    assert len(marker_writes) == 1
    with pytest.raises(WorkflowError, match='incomplete'):
        service.verify_backup(backup)
    assert_no_unmarked_backup(f.room)


def test_backup_successful_failure_cleanup_deletes_content_before_marker(legacy_workspace, monkeypatch):
    import os
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    original = WorkflowError('injected verification failure')
    real_unlink = os.unlink
    other_children_when_marker_removed = []
    def fail_verification(path):
        raise original
    def inspect_marker_removal(path, *args, **kwargs):
        if Path(path).name == 'INCOMPLETE':
            backup = next((f.room.root / 'data/backups').glob('workspace-migration-*'))
            other_children_when_marker_removed.append([child.name for child in backup.iterdir() if child.name != 'INCOMPLETE'])
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(service, '_verify_backup_contents', fail_verification)
    monkeypatch.setattr('newsroom.migration.os.unlink', inspect_marker_removal)
    with pytest.raises(WorkflowError) as captured:
        service.backup(plan)
    assert captured.value is original
    assert other_children_when_marker_removed == [[]]
    assert not list((f.room.root / 'data/backups').glob('workspace-migration-*'))
    assert_no_unmarked_backup(f.room)


def test_backup_final_empty_directory_removal_failure_has_no_recoverable_content(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    real_verify = service._verify_backup_contents
    original = WorkflowError('injected verification failure')
    cleanup = OSError('empty backup directory removal unavailable')
    real_rmdir = Path.rmdir
    def fail_verification(path):
        raise original
    def fail_empty_directory(path):
        if path.name.startswith('workspace-migration-'):
            raise cleanup
        return real_rmdir(path)
    monkeypatch.setattr(service, '_verify_backup_contents', fail_verification)
    monkeypatch.setattr(Path, 'rmdir', fail_empty_directory)
    with pytest.raises(BaseException) as captured:
        service.backup(plan)
    errors = list(exception_tree(captured.value))
    assert original in errors and cleanup in errors
    backup = next((f.room.root / 'data/backups').glob('workspace-migration-*'))
    assert list(backup.iterdir()) == []
    monkeypatch.setattr(service, '_verify_backup_contents', real_verify)
    with pytest.raises(WorkflowError, match='backup verification'):
        service.verify_backup(backup)
    assert not (f.room.root / 'workspace').exists()


def execute_clean_migration(f):
    service = migrator(f.room)
    plan = service.preflight([f.room.root / 'selected.csv'])
    return service.execute(plan, service.backup(plan))


def artifact_snapshot(room):
    return {name: data for name, data in snapshot(room.root).items()
            if name.startswith(('research/', 'articles/', 'images/', 'output/', 'workspace/'))
            and '/.migration-staging-' not in name}


def test_execute_archives_and_preserves_exact_artifacts(legacy_workspace):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight([f.room.root / 'selected.csv'])
    before_states = f.room.db.all('SELECT * FROM state_transitions')
    result = service.execute(plan, service.backup(plan))
    assert result['status'] == 'PASS'
    for entry in plan.entries:
        assert hashlib.sha256((f.room.root / entry.target).read_bytes()).hexdigest() == entry.sha256
    assert f.room.get_article(f.article_id)['draft_path'].startswith('workspace/batches/')
    assert f.room.get_article(f.single_id)['research_brief_path'].startswith('workspace/archive/legacy-single-articles/')
    assert f.room.db.all('SELECT * FROM state_transitions') == before_states
    assert f.room.db.one('SELECT workspace_version FROM batches')['workspace_version'] == 1
    assert (f.room.root / f'workspace/batches/{f.batch_id}/input/source-metadata.json').is_file()
    assert (f.room.root / 'selected.csv').is_file()
    assert not any((f.room.root / name).exists() for name in ('research', 'articles', 'images', 'output'))
    journal = json.loads(Path(result['journal_path']).read_text())
    assert [record['stage'] for record in journal['history']] == ['BACKED_UP', 'STAGED', 'DATABASE_UPDATED', 'PROMOTED', 'VERIFIED', 'CLEANED']


@pytest.mark.parametrize('race', ['before_execute', 'after_staging'])
def test_execute_hash_race_stops_before_database_update(legacy_workspace, monkeypatch, race):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = f.room.get_article(f.article_id)
    if race == 'before_execute':
        (f.room.root / f.draft).write_bytes(b'changed')
    else:
        real_stage = service._stage_workspace
        def change_after_staging(*args):
            result = real_stage(*args)
            (f.room.root / f.draft).write_bytes(b'changed')
            return result
        monkeypatch.setattr(service, '_stage_workspace', change_after_staging)
    with pytest.raises(WorkflowError, match='changed after preflight'):
        service.execute(plan, backup)
    assert f.room.get_article(f.article_id) == before
    assert not (f.room.root / 'workspace/batches').exists()


@pytest.mark.parametrize('stage', ['database', 'promotion', 'verification', 'cleanup'])
@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt, SystemExit])
def test_execute_failure_restores_database_and_artifact_tree(legacy_workspace, monkeypatch, stage, error_type):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before_tree = artifact_snapshot(f.room)
    before_db = (backup / 'newsroom.db').read_bytes()
    original = error_type(f'interrupted {stage}')
    method = {'database': '_switch_database', 'promotion': '_promote_workspace', 'verification': '_verify_workspace', 'cleanup': '_cleanup_originals'}[stage]
    real = getattr(service, method)
    def interrupt(*args, **kwargs):
        result = real(*args, **kwargs)
        if stage == 'database' and len(args) > 1:
            return result  # The disposable projected DB is not the canonical commit.
        raise original
    monkeypatch.setattr(service, method, interrupt)
    with pytest.raises(BaseException) as captured:
        service.execute(plan, backup)
    assert original in list(exception_tree(captured.value))
    assert f.room.db_path.read_bytes() == before_db
    assert artifact_snapshot(f.room) == before_tree
    journals = list((f.room.root / 'data/workspace-migration-journals').glob('*.json'))
    assert json.loads(journals[0].read_text())['recovery_status'] == 'ROLLED_BACK'
    assert not list((f.room.root / 'workspace').glob('.migration-staging-*'))


def test_execute_rejects_a_different_verified_plan(legacy_workspace):
    f = legacy_workspace
    service = migrator(f.room)
    backup = service.backup(service.preflight())
    plan = service.preflight([f.room.root / 'selected.csv'])
    before = artifact_snapshot(f.room)
    with pytest.raises(WorkflowError, match='backup.*plan'):
        service.execute(plan, backup)
    assert artifact_snapshot(f.room) == before


def test_execute_mixed_canonical_files_remain_exact(legacy_workspace):
    f = legacy_workspace
    from newsroom.batch import BatchWorkflow
    csv = write_batch_csv(f.room.root / 'canonical.csv', [candidate_row(company='Canonical', product='New', primary_source='https://canonical.example/new')])
    batch = BatchWorkflow(f.room).prepare(csv)['batch_id']
    canonical = f.room.root / 'workspace/batches' / batch
    before = snapshot(canonical)
    execute_clean_migration(f)
    after = snapshot(canonical)
    assert {name: after[name] for name in before} == before
    assert migrator(f.room).execute_from_current_state()['status'] == 'ALREADY_MIGRATED'


def test_completed_migration_is_idempotent_and_checks_receipt(legacy_workspace):
    f = legacy_workspace
    first = execute_clean_migration(f)
    before = snapshot(f.room.root)
    second = migrator(f.room).execute_from_current_state()
    assert second['status'] == 'ALREADY_MIGRATED'
    assert second['manifest_sha256'] == first['manifest_sha256']
    assert snapshot(f.room.root) == before
    receipt = f.room.root / 'data/workspace-migration-receipt.json'
    payload = json.loads(receipt.read_text())
    payload['manifest_sha256'] = 'corrupt'
    receipt.write_text(json.dumps(payload))
    with pytest.raises(WorkflowError, match='receipt'):
        migrator(f.room).execute_from_current_state()


def test_idempotency_refuses_corrupt_workspace_and_partial_journal(legacy_workspace):
    f = legacy_workspace
    result = execute_clean_migration(f)
    (f.room.root / f.room.get_article(f.article_id)['draft_path']).write_text('corrupt')
    with pytest.raises(WorkflowError, match='hash'):
        migrator(f.room).execute_from_current_state()
    journal = Path(result['journal_path'])
    data = json.loads(journal.read_text())
    data['stage'] = 'VERIFIED'
    journal.write_text(json.dumps(data))
    with pytest.raises(WorkflowError, match='recovery'):
        migrator(f.room).execute_from_current_state()


@pytest.mark.parametrize('answer', ['cancel', 'block', 'success'])
def test_migration_controller_saves_report_and_honors_confirmation(legacy_workspace, answer):
    from newsroom.macos import MacOSMigrationController
    from test_macos import FakeDialogs
    f = legacy_workspace
    if answer == 'block':
        (f.room.root / f.draft).unlink()
    before = artifact_snapshot(f.room)
    dialogs = FakeDialogs(confirms=[answer == 'success'])
    code = MacOSMigrationController(f.room, dialogs).run()
    report = f.room.root / 'data/workspace-migration-preflight.json'
    assert report in dialogs.opened
    assert code == (2 if answer == 'block' else 0)
    if answer != 'success':
        assert artifact_snapshot(f.room) == before
        assert not (f.room.root / 'data/backups').exists()
    else:
        assert f.room.get_article(f.article_id)['draft_path'].startswith('workspace/batches/')


def test_cli_explicit_execute_requires_plan_and_backup(legacy_workspace):
    f = legacy_workspace
    runner = CliRunner()
    assert runner.invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--execute']).exit_code == 2
    service = migrator(f.room)
    plan = service.preflight()
    report = service.save_report(plan)
    backup = service.backup(plan)
    result = runner.invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--execute', '--plan', str(report), '--backup', str(backup)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['status'] == 'PASS'


def test_execute_new_untracked_file_before_switch_stops_without_deleting_it(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_stage = service._stage_workspace
    extra = f.room.root / 'research/new-untracked.md'
    def add_after_staging(*args):
        real_stage(*args)
        extra.write_bytes(b'editor added this')
    monkeypatch.setattr(service, '_stage_workspace', add_after_staging)
    with pytest.raises(WorkflowError, match='changed after preflight'):
        service.execute(plan, backup)
    assert extra.read_bytes() == b'editor added this'
    assert f.room.get_article(f.article_id)['draft_path'] == f.draft.as_posix()


@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_execute_retained_tree_retirement_failure_restores_from_archive(legacy_workspace, monkeypatch, error_type):
    f = legacy_workspace
    from newsroom.batch import BatchWorkflow
    batch = BatchWorkflow(f.room).prepare(write_batch_csv(f.room.root / 'other.csv', [candidate_row(company='Other', product='Tool', primary_source='https://other.example/tool')]))['batch_id']
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_snapshot(f.room)
    real_replace = os.replace
    original = error_type('retirement interrupted after deletion')
    seen = []
    def partial_retirement(source, destination):
        real_replace(source, destination)
        path = Path(destination)
        if Path(source).name.startswith('.migration-staging-') and path.is_relative_to(f.room.root / 'data/workspace-migration-recovery') and not seen:
            seen.append(path)
            shutil.rmtree(path / 'retained')
            raise original
    monkeypatch.setattr('newsroom.migration.os.replace', partial_retirement)
    with pytest.raises(BaseException) as captured:
        service.execute(plan, backup)
    assert original in list(exception_tree(captured.value))
    assert artifact_snapshot(f.room) == before
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    assert not service.receipt_path.exists()


def test_execute_preserves_original_and_recovery_errors(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    original = KeyboardInterrupt('verification interrupted')
    recovery = OSError('database restoration failed')
    def interrupt(*args):
        raise original
    def fail_restore(*args):
        raise recovery
    monkeypatch.setattr(service, '_verify_workspace', interrupt)
    monkeypatch.setattr(service, '_restore_database', fail_restore)
    with pytest.raises(BaseExceptionGroup) as captured:
        service.execute(plan, backup)
    assert original in list(exception_tree(captured.value)) and recovery in list(exception_tree(captured.value))
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['recovery_status'] == 'RECOVERY_REQUIRED'
    with pytest.raises(WorkflowError, match='recovery'):
        migrator(f.room).execute_from_current_state()


@pytest.mark.parametrize('mutation', ['handoff_identity', 'source_metadata', 'current_version'])
def test_execute_rejects_invalid_staged_material_before_switch(legacy_workspace, monkeypatch, mutation):
    f = legacy_workspace
    service = migrator(f.room)
    if mutation == 'handoff_identity':
        request_path = f.room.root / f'output/batches/{f.batch_id}/{f.request["request_id"]}.json'
        document = json.loads(request_path.read_bytes())
        document['batch_id'] = 'batch-other'
        request_path.write_text(json.dumps(document))
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_snapshot(f.room)
    database = f.room.db_path.read_bytes()
    if mutation != 'handoff_identity':
        real_validate = service._validate_materials
        def corrupt_then_validate(room):
            if mutation == 'source_metadata':
                path = room.root / f'workspace/batches/{f.batch_id}/input/source-metadata.json'
                document = json.loads(path.read_bytes())
                document['source_sha256'] = 'wrong'
                path.write_text(json.dumps(document))
            else:
                with room.db.connect() as connection:
                    connection.execute('UPDATE articles SET revision_number=3 WHERE article_id=?', (f.article_id,))
            return real_validate(room)
        monkeypatch.setattr(service, '_validate_materials', corrupt_then_validate)
    with pytest.raises(WorkflowError):
        service.execute(plan, backup)
    assert f.room.db_path.read_bytes() == database
    assert artifact_snapshot(f.room) == before


def test_execute_interruption_during_individual_promotion_restores_mixed_tree(legacy_workspace, monkeypatch):
    f = legacy_workspace
    from newsroom.batch import BatchWorkflow
    BatchWorkflow(f.room).prepare(write_batch_csv(f.room.root / 'canonical.csv', [candidate_row(company='Existing', product='Tool', primary_source='https://existing.example/tool')]))
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_snapshot(f.room)
    real_replace = __import__('os').replace
    original = KeyboardInterrupt('rename returned then interrupted')
    seen = []
    def interrupt(source, destination):
        result = real_replace(source, destination)
        if Path(destination) == f.room.root / 'workspace/batches' and not seen:
            seen.append(destination)
            raise original
        return result
    monkeypatch.setattr('newsroom.migration.os.replace', interrupt)
    with pytest.raises(BaseException) as captured:
        service.execute(plan, backup)
    assert original in list(exception_tree(captured.value))
    assert artifact_snapshot(f.room) == before
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()


def test_execute_rechecks_verified_backup_before_database_switch(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_stage = service._stage_workspace
    database = f.room.db_path.read_bytes()
    def corrupt_backup(*args):
        real_stage(*args)
        (backup / 'newsroom.db').write_bytes(b'corrupt backup')
    monkeypatch.setattr(service, '_stage_workspace', corrupt_backup)
    with pytest.raises(WorkflowError, match='backup'):
        service.execute(plan, backup)
    assert f.room.db_path.read_bytes() == database


def test_execute_validates_packages_disposably_without_release_state_changes(approved_batch, monkeypatch):
    from newsroom.batch_release import BatchReleaseWorkflow
    f = approved_batch
    publish = BatchReleaseWorkflow(f.room).build_packages(f.batch_id)
    before_packages = snapshot(publish)
    before_states = f.room.db.all('SELECT * FROM state_transitions')
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    targets = []
    real_write = f.room._write_publication_package
    def record_package(article, image, sources, target):
        targets.append(target)
        return real_write(article, image, sources, target)
    monkeypatch.setattr(f.room, '_write_publication_package', record_package)
    result = service.execute(plan, backup)
    assert result['status'] == 'PASS'
    assert targets and all(not target.is_relative_to(f.room.root) for target in targets)
    assert snapshot(publish) == before_packages
    assert f.room.db.all('SELECT * FROM state_transitions') == before_states
    assert all(f.room.get_article(article)['status'] == 'APPROVED' for article in f.article_ids)


@pytest.mark.parametrize('mutation', ['metadata', 'content', 'license', 'checklist'])
def test_execute_rejects_corrupt_existing_package_without_mutation(approved_batch, mutation):
    from newsroom.batch_release import BatchReleaseWorkflow
    f = approved_batch
    publish = BatchReleaseWorkflow(f.room).build_packages(f.batch_id) / f.article_ids[0]
    if mutation == 'metadata':
        path = publish / 'metadata.json'
        document = json.loads(path.read_bytes())
        document['article_id'] = 'art-wrong'
        path.write_text(json.dumps(document))
    elif mutation == 'license':
        path = publish / 'image-license.json'
        document = json.loads(path.read_bytes())
        document['article_id'] = 'art-wrong'
        path.write_text(json.dumps(document))
    else:
        (publish / ('article.md' if mutation == 'content' else 'publication-checklist.md')).write_text('bad' if mutation == 'content' else '')
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_snapshot(f.room)
    database = f.room.db_path.read_bytes()
    with pytest.raises(WorkflowError, match='package'):
        service.execute(plan, backup)
    assert artifact_snapshot(f.room) == before
    assert f.room.db_path.read_bytes() == database


def artifact_details(room):
    return {name: data for name, data in filesystem_details(room.root).items()
            if name.split('/')[0] in {'research', 'articles', 'images', 'output', 'workspace'}}


def test_execute_rollback_restores_file_and_directory_modes_and_timestamps(legacy_workspace, monkeypatch):
    f = legacy_workspace
    from newsroom.batch import BatchWorkflow
    BatchWorkflow(f.room).prepare(write_batch_csv(f.room.root / 'canonical.csv', [candidate_row(company='Existing', product='Tool', primary_source='https://existing.example/tool')]))
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_details(f.room)
    real_cleanup = service._cleanup_originals
    def interrupt(*args):
        real_cleanup(*args)
        raise KeyboardInterrupt('cleanup interruption')
    monkeypatch.setattr(service, '_cleanup_originals', interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.execute(plan, backup)
    assert artifact_details(f.room) == before


def test_execute_completed_receipt_does_not_bypass_clean_plan_and_backup(legacy_workspace):
    from newsroom.migration import MigrationPlan
    f = legacy_workspace
    execute_clean_migration(f)
    with pytest.raises(WorkflowError, match='blocking'):
        migrator(f.room).execute(MigrationPlan(blocking_errors=['blocked']), f.room.root / 'missing-backup')


def test_idempotency_checks_current_legacy_article_version(legacy_workspace):
    f = legacy_workspace
    execute_clean_migration(f)
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET revision_number=2 WHERE article_id=?', (f.single_id,))
    with pytest.raises(WorkflowError, match='version'):
        migrator(f.room).execute_from_current_state()


def test_rollback_preserves_untracked_file_and_reports_required_recovery(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_verify = service._verify_workspace
    extra = f.room.root / f'workspace/batches/{f.batch_id}/editor-added.md'
    original = KeyboardInterrupt('verification interrupted')
    def interrupt(*args):
        real_verify(*args)
        extra.write_bytes(b'editor data')
        raise original
    monkeypatch.setattr(service, '_verify_workspace', interrupt)
    with pytest.raises(BaseExceptionGroup) as captured:
        service.execute(plan, backup)
    assert original in list(exception_tree(captured.value))
    assert any('quarantine' in str(error) for error in exception_tree(captured.value))
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    quarantine = Path(journal['quarantined_promotions'][0]['path'])
    assert (quarantine / f.batch_id / extra.name).read_bytes() == b'editor data'
    assert not extra.exists()
    with pytest.raises(WorkflowError, match='recovery'):
        migrator(f.room).execute_from_current_state()


def test_execute_rejects_package_rights_drift(approved_batch):
    from newsroom.batch_release import BatchReleaseWorkflow
    f = approved_batch
    package = BatchReleaseWorkflow(f.room).build_packages(f.batch_id) / f.article_ids[0]
    path = package / 'image-license.json'
    document = json.loads(path.read_bytes())
    document['rights_statement'] = 'Changed without editor confirmation'
    path.write_text(json.dumps(document))
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    with pytest.raises(WorkflowError, match='rights'):
        service.execute(plan, backup)


def test_execute_rejects_current_image_association_before_approval(ready_batch):
    f = ready_batch
    image = f.room.get_article(f.article_ids[1])['image_id']
    with f.room.db.connect() as connection:
        connection.execute('UPDATE articles SET image_id=? WHERE article_id=?', (image, f.article_ids[0]))
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    with pytest.raises(WorkflowError, match='image'):
        service.execute(plan, backup)


def test_execute_reopens_generated_files_after_promotion(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_snapshot(f.room)
    real_promote = service._promote_workspace
    def change_promoted_script(*args):
        real_promote(*args)
        (f.room.root / f'workspace/batches/{f.batch_id}/OPEN_BATCH.command').write_text('changed dispatch')
    monkeypatch.setattr(service, '_promote_workspace', change_promoted_script)
    with pytest.raises(BaseExceptionGroup) as captured:
        service.execute(plan, backup)
    assert any('hash' in str(error) for error in exception_tree(captured.value))
    assert artifact_snapshot(f.room) == before
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    quarantine = Path(journal['quarantined_promotions'][0]['path'])
    assert (quarantine / f.batch_id / 'OPEN_BATCH.command').read_text() == 'changed dispatch'


def test_idempotency_refuses_missing_generated_launcher(legacy_workspace):
    f = legacy_workspace
    execute_clean_migration(f)
    (f.room.root / 'workspace/Migrate Newsroom.command').unlink()
    with pytest.raises(WorkflowError, match='launcher'):
        migrator(f.room).execute_from_current_state()


@pytest.mark.parametrize('existing_canonical', [False, True])
def test_idempotency_remains_valid_after_preparing_new_canonical_batch(legacy_workspace, existing_canonical):
    f = legacy_workspace
    from newsroom.batch import BatchWorkflow
    workflow = BatchWorkflow(f.room)
    if existing_canonical:
        workflow.prepare(write_batch_csv(f.room.root / 'before.csv', [candidate_row(company='Before', product='Tool', primary_source='https://before.example/tool')]))
    execute_clean_migration(f)
    batch = workflow.prepare(write_batch_csv(f.room.root / 'after.csv', [candidate_row(company='After', product='Tool', primary_source='https://after.example/tool')]))['batch_id']
    assert f.room.db.one('SELECT workspace_version FROM batches WHERE batch_id=?', (batch,))['workspace_version'] == 1
    assert migrator(f.room).execute_from_current_state()['status'] == 'ALREADY_MIGRATED'


@pytest.mark.parametrize('error_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('boundary', ['commit', 'close'])
def test_commit_completion_interruption_restores_verified_originals_and_allows_retry(legacy_workspace, monkeypatch, error_type, boundary):
    f = legacy_workspace
    prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_details(f.room)
    original = error_type('commit completed but caller interrupted')
    class InterruptedCommit(sqlite3.Connection):
        def commit(self):
            super().commit()
            if boundary == 'commit':
                raise original

        def close(self):
            super().close()
            if boundary == 'close':
                raise original
    def connect():
        connection = sqlite3.connect(f.room.db_path, factory=InterruptedCommit)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection
    with monkeypatch.context() as patch:
        patch.setattr(f.room.db, 'connect', connect)
        with pytest.raises(BaseException) as captured:
            service.execute(plan, backup)
    assert original in list(exception_tree(captured.value))
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    assert artifact_details(f.room) == before
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['database_update_attempted'] is True
    assert journal['database_updated'] is (boundary == 'close')
    assert journal['recovery_status'] == 'ROLLED_BACK'
    assert not (f.room.root / 'data/workspace-migration-lock.json').exists()
    assert service.execute(plan, backup)['status'] == 'PASS'


def prepare_existing_canonical(f):
    from newsroom.batch import BatchWorkflow
    return BatchWorkflow(f.room).prepare(write_batch_csv(f.room.root / 'existing.csv', [candidate_row(company='Existing', product='Tool', primary_source='https://existing.example/tool')]))['batch_id']


@pytest.mark.parametrize('timing', ['before_rename', 'before_retirement', 'later_rollback'])
def test_retained_original_additions_are_never_retired_or_overwritten(legacy_workspace, monkeypatch, timing):
    f = legacy_workspace
    batch = prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    relative = Path('workspace/batches') / batch / 'editor-added.md'
    if timing == 'before_rename':
        real_promote = service._promote_workspace
        def add_then_promote(stage):
            (f.room.root / relative).write_bytes(b'editor added this')
            real_promote(stage)
        monkeypatch.setattr(service, '_promote_workspace', add_then_promote)
    else:
        real_verify = service._verify_workspace
        def add_retained_then_verify(*args):
            real_verify(*args)
            stage = Path(service._execution['staging_path'])
            path = stage / 'retained/batches' / batch / 'editor-added.md'
            path.write_bytes(b'editor added this')
            if timing == 'later_rollback':
                raise KeyboardInterrupt('later verification interrupted')
        monkeypatch.setattr(service, '_verify_workspace', add_retained_then_verify)
    with pytest.raises(BaseException):
        service.execute(plan, backup)
    assert (f.room.root / relative).read_bytes() == b'editor added this'
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['stage'] != 'CLEANED'
    assert not service.receipt_path.exists()


def test_barrier_refuses_normal_cli_and_finder_between_commit_and_promotion(legacy_workspace, monkeypatch):
    from newsroom.macos import MacOSBatchController
    from test_macos import FakeDialogs
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_promote = service._promote_workspace
    results = []
    def blocked_then_promote(stage):
        assert f.room.get_article(f.article_id)['draft_path'].startswith('workspace/batches/')
        assert not (f.room.root / f.room.get_article(f.article_id)['draft_path']).exists()
        lock = f.room.root / 'data/workspace-migration-lock.json'
        assert lock.exists()
        with monkeypatch.context() as patch:
            def forbidden_constructor(*args):
                raise AssertionError('normal command must stop before creating Newsroom')
            patch.setattr('newsroom.cli.Newsroom', forbidden_constructor)
            runner = CliRunner()
            for command in (['batch', 'status', f.batch_id], ['macos-action', f.batch_id, 'open-next-request']):
                result = runner.invoke(app, ['--root', str(f.room.root), *command])
                assert result.exit_code == 2, result.output
                assert 'recovery' in result.output.lower()
                results.append(result)
        dialogs = FakeDialogs()
        controller = MacOSBatchController(f.room, dialogs)
        with monkeypatch.context() as patch:
            patch.setattr(controller.workspace, '_batch', lambda *_: pytest.fail('must refuse before batch lookup'))
            assert controller.run(f.batch_id, 'open-next-request') == 2
        assert not dialogs.opened and not dialogs.revealed
        real_promote(stage)
    monkeypatch.setattr(service, '_promote_workspace', blocked_then_promote)
    assert service.execute(plan, backup)['status'] == 'PASS'
    assert len(results) == 2
    assert not (f.room.root / 'data/workspace-migration-lock.json').exists()


def test_stale_migration_barrier_blocks_normal_cli_without_database_access(legacy_workspace, monkeypatch):
    f = legacy_workspace
    lock = f.room.root / 'data/workspace-migration-lock.json'
    lock.write_text(json.dumps({'owner': 'crashed-owner', 'journal_path': 'data/workspace-migration-journals/crashed.json'}))
    before = snapshot(f.room.root)
    def forbidden_constructor(*args):
        raise AssertionError('blocked command created Newsroom')
    monkeypatch.setattr('newsroom.cli.Newsroom', forbidden_constructor)
    for command in (['init'], ['stats'], ['candidate', 'list'], ['published', 'list'], ['duplicate-check', 'id'], ['macos-action', f.batch_id, 'refresh']):
        result = CliRunner().invoke(app, ['--root', str(f.room.root), *command])
        assert result.exit_code == 2, result.output
        assert 'recovery' in result.output.lower()
    assert snapshot(f.room.root) == before


@pytest.mark.parametrize('replacement', ['foreign_record', 'copied_record'])
def test_migration_cannot_release_another_barrier_owner(legacy_workspace, monkeypatch, replacement):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_verify = service._verify_workspace
    foreign = {'schema_version': 1, 'owner': 'different-migration', 'journal_path': 'foreign.json'}
    def replace_owner(*args):
        real_verify(*args)
        lock = f.room.root / 'data/workspace-migration-lock.json'
        if replacement == 'copied_record':
            foreign.update(json.loads(lock.read_bytes()))
            copied = lock.with_suffix('.replacement')
            copied.write_bytes(lock.read_bytes())
            copied.replace(lock)
        else:
            lock.write_text(json.dumps(foreign))
        raise KeyboardInterrupt('interruption after foreign barrier replaced ours')
    monkeypatch.setattr(service, '_verify_workspace', replace_owner)
    with pytest.raises(BaseExceptionGroup):
        service.execute(plan, backup)
    assert json.loads((f.room.root / 'data/workspace-migration-lock.json').read_bytes()) == foreign
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['recovery_status'] == 'RECOVERY_REQUIRED'


def test_commit_interruption_preserves_original_when_connection_rollback_also_fails(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_details(f.room)
    original = KeyboardInterrupt('commit returned then interrupted')
    rollback_error = OSError('connection rollback failed')

    class InterruptedCommit(sqlite3.Connection):
        def commit(self):
            super().commit()
            raise original

        def rollback(self):
            raise rollback_error

    def connect():
        connection = sqlite3.connect(f.room.db_path, factory=InterruptedCommit)
        connection.row_factory = sqlite3.Row
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(f.room.db, 'connect', connect)
        with pytest.raises(BaseExceptionGroup) as captured:
            service.execute(plan, backup)
    assert original in list(exception_tree(captured.value))
    assert rollback_error in list(exception_tree(captured.value))
    assert artifact_details(f.room) == before
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['database_update_attempted'] is True
    assert journal['recovery_status'] == 'ROLLED_BACK'
    assert not (f.room.root / 'data/workspace-migration-lock.json').exists()


def test_unmanifested_retained_empty_directory_is_preserved(legacy_workspace, monkeypatch):
    f = legacy_workspace
    batch = prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    relative = Path('workspace/batches') / batch / 'editor-empty-directory'
    real_promote = service._promote_workspace

    def add_then_promote(stage):
        (f.room.root / relative).mkdir()
        real_promote(stage)

    monkeypatch.setattr(service, '_promote_workspace', add_then_promote)
    with pytest.raises(WorkflowError, match='retained originals changed'):
        service.execute(plan, backup)
    assert (f.room.root / relative).is_dir()
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['stage'] != 'CLEANED'
    assert not service.receipt_path.exists()


def test_retained_recovery_moves_original_inodes_back_and_repairs_only_missing_files(legacy_workspace, monkeypatch):
    f = legacy_workspace
    batch = prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    root = f.room.root / 'workspace/batches' / batch
    kept = root / 'README.md'
    missing = root / 'batch-state.json'
    addition = root / 'editor-added.md'
    before = artifact_snapshot(f.room)
    original_inodes = (root.stat().st_ino, kept.stat().st_ino)
    real_verify = service._verify_workspace

    def damage_then_verify(*args):
        real_verify(*args)
        retained = Path(service._execution['staging_path']) / 'retained/batches' / batch
        (retained / missing.name).unlink()
        (retained / addition.name).write_bytes(b'preserve editor addition')
        raise KeyboardInterrupt('verification interrupted after retained file disappeared')

    monkeypatch.setattr(service, '_verify_workspace', damage_then_verify)
    with pytest.raises(KeyboardInterrupt):
        service.execute(plan, backup)
    assert (root.stat().st_ino, kept.stat().st_ino) == original_inodes
    assert artifact_snapshot(f.room) == {**before, addition.relative_to(f.room.root).as_posix(): b'preserve editor addition'}
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    assert not (f.room.root / 'data/workspace-migration-lock.json').exists()


def test_migration_preflight_and_backup_deliberately_bypass_stale_barrier(legacy_workspace):
    f = legacy_workspace
    lock = f.room.root / 'data/workspace-migration-lock.json'
    lock.write_text(json.dumps({'owner': 'crashed-owner', 'journal_path': 'crashed.json'}))
    original = lock.read_bytes()
    runner = CliRunner()
    preflight = runner.invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--preflight'])
    assert preflight.exit_code == 0, preflight.output
    report = f.room.root / json.loads(preflight.output)['report_path']
    backup = runner.invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--backup-plan', '--plan', str(report)])
    assert backup.exit_code == 0, backup.output
    assert json.loads(backup.output)['status'] == 'BACKED_UP'
    assert lock.read_bytes() == original
    blocked = runner.invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--execute', '--plan', str(report), '--backup', json.loads(backup.output)['backup_path']])
    assert blocked.exit_code == 2, blocked.output
    assert 'recovery' in blocked.output.lower()
    assert lock.read_bytes() == original


def test_recovery_reconstructs_retained_tree_lost_after_first_rename(legacy_workspace, monkeypatch):
    f = legacy_workspace
    prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_details(f.room)
    original = KeyboardInterrupt('retained tree lost immediately after rename')
    real_replace = os.replace
    interrupted = False

    def lose_retained(source, destination):
        nonlocal interrupted
        real_replace(source, destination)
        path = Path(destination)
        if path.name == 'batches' and path.parent.name == 'retained' and not interrupted:
            interrupted = True
            shutil.rmtree(path)
            raise original

    monkeypatch.setattr('newsroom.migration.os.replace', lose_retained)
    with pytest.raises(KeyboardInterrupt) as captured:
        service.execute(plan, backup)
    assert captured.value is original
    assert artifact_details(f.room) == before
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['recovery_status'] == 'ROLLED_BACK'
    assert not (f.room.root / 'data/workspace-migration-lock.json').exists()


@pytest.mark.parametrize('timing', ['before_rename', 'before_retirement'])
def test_changed_retained_original_is_preserved_and_keeps_recovery_barrier(legacy_workspace, monkeypatch, timing):
    f = legacy_workspace
    batch = prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    relative = Path('workspace/batches') / batch / 'README.md'
    if timing == 'before_rename':
        real_promote = service._promote_workspace

        def change_then_promote(stage):
            (f.room.root / relative).write_bytes(b'editor changed original')
            real_promote(stage)

        monkeypatch.setattr(service, '_promote_workspace', change_then_promote)
    else:
        real_verify = service._verify_workspace

        def change_then_verify(*args):
            real_verify(*args)
            stage = Path(service._execution['staging_path'])
            (stage / 'retained/batches' / batch / 'README.md').write_bytes(b'editor changed original')

        monkeypatch.setattr(service, '_verify_workspace', change_then_verify)
    with pytest.raises(BaseExceptionGroup):
        service.execute(plan, backup)
    assert (f.room.root / relative).read_bytes() == b'editor changed original'
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['stage'] != 'CLEANED'
    assert journal['recovery_status'] == 'RECOVERY_REQUIRED'
    assert (f.room.root / 'data/workspace-migration-lock.json').exists()
    assert not service.receipt_path.exists()


def test_pending_finder_approval_prevents_migration_until_action_commits(ready_batch):
    from newsroom.batch_release import BatchReleaseWorkflow
    from newsroom.macos import MacOSBatchController
    from test_macos import FakeDialogs
    f = ready_batch
    BatchReleaseWorkflow(f.room).review(f.batch_id, 'editor', 'reviewed')
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    report = service.save_report(plan)
    waiting, resume = Event(), Event()

    class PausedDialogs(FakeDialogs):
        def confirm(self, *args):
            waiting.set()
            assert resume.wait(10), 'approval confirmation was not resumed'
            return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        action = pool.submit(MacOSBatchController(f.room, PausedDialogs()).run, f.batch_id, 'approve')
        try:
            assert waiting.wait(10)
            result = CliRunner().invoke(app, ['--root', str(f.room.root), 'workspace', 'migrate', '--execute', '--plan', str(report), '--backup', str(backup)])
            assert result.exit_code == 2, result.output
            assert 'active' in result.output.lower() and 'retry' in result.output.lower()
            assert not (f.room.root / 'data/workspace-migration-lock.json').exists()
            assert not list((f.room.root / 'workspace').glob('.migration-staging-*'))
            assert all(f.room.get_article(article)['status'] == 'REVIEWED' for article in f.article_ids)
        finally:
            resume.set()
        assert action.result(timeout=10) == 0
    assert all(f.room.get_article(article)['status'] == 'APPROVED' for article in f.article_ids)


@pytest.mark.parametrize('mutation', ['article', 'empty_directory', 'added_file', 'symlink'])
def test_changed_promoted_tree_is_quarantined_before_exact_rollback(legacy_workspace, monkeypatch, mutation):
    f = legacy_workspace
    prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_snapshot(f.room)
    relative = Path('batches') / f.batch_id / 'articles' / f.article_id / 'article-v2.md'
    added = Path('batches') / f.batch_id / 'editor-added'
    original = KeyboardInterrupt('stop after promoted editor change')
    real_verify = service._verify_workspace

    def change_then_interrupt(*args):
        real_verify(*args)
        if mutation == 'article':
            changed = service.workspace.root / relative
            changed.write_bytes(b'preserve edited promoted article')
            changed.chmod(0o640)
        elif mutation == 'empty_directory':
            (service.workspace.root / added).mkdir(mode=0o750)
        elif mutation == 'added_file':
            (service.workspace.root / added).write_bytes(b'preserve added promoted file')
        else:
            (service.workspace.root / added).symlink_to('article-v2.md')
        raise original

    monkeypatch.setattr(service, '_verify_workspace', change_then_interrupt)
    with pytest.raises(BaseException) as captured:
        service.execute(plan, backup)
    assert isinstance(captured.value, BaseExceptionGroup)
    assert original in list(exception_tree(captured.value))
    assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
    assert artifact_snapshot(f.room) == before
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    quarantine = Path(journal['quarantined_promotions'][0]['path'])
    assert quarantine.is_relative_to(f.room.root / 'data/workspace-migration-recovery')
    assert journal['stage'] != 'CLEANED' and journal['recovery_status'] == 'RECOVERY_REQUIRED'
    if mutation == 'article':
        assert (quarantine / relative.relative_to('batches')).read_bytes() == b'preserve edited promoted article'
        assert (quarantine / relative.relative_to('batches')).stat().st_mode & 0o777 == 0o640
    elif mutation == 'empty_directory':
        assert (quarantine / added.relative_to('batches')).is_dir()
        assert (quarantine / added.relative_to('batches')).stat().st_mode & 0o777 == 0o750
    elif mutation == 'added_file':
        assert (quarantine / added.relative_to('batches')).read_bytes() == b'preserve added promoted file'
    else:
        assert (quarantine / added.relative_to('batches')).is_symlink()
    assert (f.room.root / 'data/workspace-migration-lock.json').exists()
    assert not service.receipt_path.exists()
    blocked = CliRunner().invoke(app, ['--root', str(f.room.root), 'stats'])
    assert blocked.exit_code == 2 and 'recovery' in blocked.output.lower()


@pytest.mark.parametrize('error_type', [KeyboardInterrupt, SystemExit, OSError])
@pytest.mark.parametrize('unlinked', [True, False])
def test_completed_migration_never_rolls_back_when_barrier_release_raises(legacy_workspace, monkeypatch, error_type, unlinked):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    original = error_type('barrier finalization interrupted')
    real_unlink = Path.unlink
    committed = {}

    def interrupted_unlink(path, *args, **kwargs):
        if path.name == 'workspace-migration-lock.json':
            committed['database'] = f.room.db_path.read_bytes()
            committed['artifacts'] = artifact_snapshot(f.room)
            if unlinked:
                real_unlink(path, *args, **kwargs)
            raise original
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', interrupted_unlink)
    result, failure = None, None
    try:
        result = service.execute(plan, backup)
    except BaseException as error:
        failure = error
    if unlinked:
        assert failure is None, repr(failure)
        assert result['status'] == 'PASS'
    else:
        assert isinstance(failure, WorkflowError), repr(failure)
        assert 'committed' in str(failure) and 'finalization' in str(failure) and 'recovery' in str(failure)
        assert failure.__cause__ is original
    assert f.room.db_path.read_bytes() == committed['database']
    assert artifact_snapshot(f.room) == committed['artifacts']
    assert f.room.get_article(f.article_id)['draft_path'].startswith('workspace/batches/')
    assert service.receipt_path.exists()
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['stage'] == 'CLEANED' and not journal.get('recovery_status')
    assert journal['finalization_error']['type'] == error_type.__name__
    assert (f.room.root / 'data/workspace-migration-lock.json').exists() is (not unlinked)
    normal = CliRunner().invoke(app, ['--root', str(f.room.root), 'batch', 'status', f.batch_id])
    assert normal.exit_code == (0 if unlinked else 2), normal.output


def test_migration_exclusive_lock_refuses_new_actions_before_persistent_barrier(legacy_workspace, monkeypatch):
    from newsroom.macos import MacOSBatchController
    from test_macos import FakeDialogs
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_stage = service._stage_workspace

    def blocked_then_stage(*args):
        assert not (f.room.root / 'data/workspace-migration-lock.json').exists()
        with monkeypatch.context() as patch:
            patch.setattr('newsroom.cli.Newsroom', lambda *_: pytest.fail('normal action accessed Newsroom during migration'))
            result = CliRunner().invoke(app, ['--root', str(f.room.root), 'batch', 'status', f.batch_id])
            assert result.exit_code == 2 and ('migration operation active' in result.output or 'recovery' in result.output), result.output
        dialogs = FakeDialogs()
        controller = MacOSBatchController(f.room, dialogs)
        with monkeypatch.context() as patch:
            patch.setattr(controller.workspace, '_batch', lambda *_: pytest.fail('Finder action accessed batch during migration'))
            assert controller.run(f.batch_id, 'approve') == 2
        assert not dialogs.confirmations
        real_stage(*args)

    monkeypatch.setattr(service, '_stage_workspace', blocked_then_stage)
    assert service.execute(plan, backup)['status'] == 'PASS'


def test_normal_cli_holds_shared_lock_through_approval_commit(ready_batch, monkeypatch):
    from newsroom.batch_release import BatchReleaseWorkflow
    f = ready_batch
    release = BatchReleaseWorkflow(f.room)
    release.review(f.batch_id, 'editor', 'reviewed')
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    waiting, resume = Event(), Event()
    real_approve = BatchReleaseWorkflow.approve

    def paused_approve(self, *args):
        waiting.set()
        assert resume.wait(10)
        return real_approve(self, *args)

    monkeypatch.setattr(BatchReleaseWorkflow, 'approve', paused_approve)
    with ThreadPoolExecutor(max_workers=1) as pool:
        action = pool.submit(CliRunner().invoke, app, ['--root', str(f.room.root), 'batch', 'approve', f.batch_id, '--actor', 'editor', '--reason', 'approved'])
        try:
            assert waiting.wait(10)
            with pytest.raises(WorkflowError, match='active.*retry migration'):
                service.execute(plan, backup)
            assert not (f.room.root / 'data/workspace-migration-lock.json').exists()
        finally:
            resume.set()
        result = action.result(timeout=10)
        assert result.exit_code == 0, result.output
    assert all(f.room.get_article(article)['status'] == 'APPROVED' for article in f.article_ids)


def test_unverifiable_committed_receipt_reinstates_barrier_without_rollback(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_release = service._release_barrier
    original = KeyboardInterrupt('receipt damaged during final barrier removal')
    committed = {}

    def unlink_then_damage():
        committed['database'] = f.room.db_path.read_bytes()
        committed['artifacts'] = artifact_snapshot(f.room)
        real_release()
        service.receipt_path.write_bytes(b'{')
        raise original

    monkeypatch.setattr(service, '_release_barrier', unlink_then_damage)
    with pytest.raises(WorkflowError, match='committed.*finalization.*recovery') as captured:
        service.execute(plan, backup)
    assert original in list(exception_tree(captured.value.__cause__))
    assert f.room.db_path.read_bytes() == committed['database']
    assert artifact_snapshot(f.room) == committed['artifacts']
    assert (f.room.root / 'data/workspace-migration-lock.json').is_file()
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    assert journal['stage'] == 'CLEANED' and journal['finalization_status'] == 'RECOVERY_REQUIRED'
    result = CliRunner().invoke(app, ['--root', str(f.room.root), 'stats'])
    assert result.exit_code == 2 and 'recovery' in result.output.lower()


def test_nested_help_leaves_empty_root_unchanged(tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    result = CliRunner().invoke(app, ['--root', str(empty), 'batch', 'status', '--help'])
    assert result.exit_code == 0, result.output
    assert list(empty.iterdir()) == []


@pytest.mark.parametrize('state', ['DATABASE_UPDATED', 'CLEANED', 'corrupt', 'unreadable', 'failed_rollback'])
def test_ordinary_access_blocks_unresolved_journal_without_barrier(legacy_workspace, monkeypatch, state):
    from newsroom.macos import MacOSBatchController
    from test_macos import FakeDialogs
    f = legacy_workspace
    directory = f.room.root / 'data/workspace-migration-journals'
    directory.mkdir()
    journal = directory / 'interrupted.json'
    value = {'schema_version': 1, 'stage': state, 'history': [], 'journal_path': str(journal)}
    if state == 'failed_rollback':
        value.update(recovery_status='RECOVERY_REQUIRED', stage='VERIFIED')
    journal.write_bytes(b'{' if state == 'corrupt' else json.dumps(value).encode())
    original_read = Path.read_bytes
    if state == 'unreadable':
        def unreadable(path):
            if path == journal:
                raise OSError('journal unavailable')
            return original_read(path)
        monkeypatch.setattr(Path, 'read_bytes', unreadable)
    before = {path: original_read(path) for path in f.room.root.rglob('*') if path.is_file()}
    result = CliRunner().invoke(app, ['--root', str(f.room.root), 'stats'])
    assert result.exit_code == 2 and 'recovery' in result.output.lower(), result.output
    dialogs = FakeDialogs()
    assert MacOSBatchController(f.room, dialogs).run(f.batch_id, 'approve') == 2
    assert not dialogs.confirmations
    assert {path: original_read(path) for path in f.room.root.rglob('*') if path.is_file()} == before


def test_journal_stage_and_history_change_only_after_durable_write(legacy_workspace, monkeypatch):
    from copy import deepcopy
    service = migrator(legacy_workspace.room)
    service._execution = {'stage': 'VERIFIED', 'history': [{'stage': 'VERIFIED'}], 'journal_path': str(legacy_workspace.room.root / 'data/journal.json')}
    before = deepcopy(service._execution)
    def fail_write(*args):
        raise OSError('journal replacement did not happen')
    monkeypatch.setattr('newsroom.migration._durable_json', fail_write)
    with pytest.raises(OSError):
        service._journal('CLEANED')
    assert service._execution == before


@pytest.mark.parametrize('persisted', [False, True])
def test_uncertain_cleaned_write_uses_persisted_state(legacy_workspace, monkeypatch, persisted):
    import newsroom.migration as migration
    f = legacy_workspace
    prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_snapshot(f.room)
    original = KeyboardInterrupt('CLEANED write interrupted')
    durable = migration._durable_json
    interrupted = False
    def fail_cleaned(path, value):
        nonlocal interrupted
        if value.get('stage') == 'CLEANED' and not interrupted:
            interrupted = True
            if persisted:
                durable(path, value)
            raise original
        return durable(path, value)
    monkeypatch.setattr(migration, '_durable_json', fail_cleaned)
    with pytest.raises(BaseException) as captured:
        service.execute(plan, backup)
    assert original in list(exception_tree(captured.value))
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    if persisted:
        assert journal['stage'] == 'CLEANED'
        assert f.room.get_article(f.article_id)['draft_path'].startswith('workspace/')
        assert service.receipt_path.exists()
    else:
        assert journal['stage'] != 'CLEANED'
        assert journal['recovery_status'] == 'ROLLED_BACK'
        assert artifact_snapshot(f.room) == before
        assert f.room.db_path.read_bytes() == (backup / 'newsroom.db').read_bytes()
        assert not service.receipt_path.exists()


@pytest.mark.parametrize('boundary', ['rollback', 'retirement'])
def test_write_at_tree_retirement_boundary_is_preserved(legacy_workspace, monkeypatch, boundary):
    f = legacy_workspace
    batch = prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_replace, real_remove = os.replace, shutil.rmtree
    injected = False
    def inject(path):
        nonlocal injected
        path = Path(path)
        if injected:
            return
        if boundary == 'rollback' and path == f.room.root / 'workspace/batches':
            target = path / batch / 'boundary-write.md'
        elif boundary == 'retirement' and path.name.startswith('.migration-staging-'):
            target = path / 'retained/batches' / batch / 'boundary-write.md'
        else:
            return
        target.write_bytes(b'editor wrote at the deletion boundary')
        injected = True
    def replace(source, destination):
        if Path(destination).is_relative_to(f.room.root / 'data/workspace-migration-recovery'):
            inject(source)
        return real_replace(source, destination)
    def remove(path, *args, **kwargs):
        inject(path)
        return real_remove(path, *args, **kwargs)
    monkeypatch.setattr('newsroom.migration.os.replace', replace)
    monkeypatch.setattr('newsroom.migration.shutil.rmtree', remove)
    if boundary == 'rollback':
        def stop(*args):
            raise KeyboardInterrupt('rollback before tree retirement')
        monkeypatch.setattr(service, '_verify_workspace', stop)
    try:
        service.execute(plan, backup)
    except BaseException:
        pass
    assert injected
    preserved = list(f.room.root.rglob('boundary-write.md'))
    assert preserved and all(path.read_bytes() == b'editor wrote at the deletion boundary' for path in preserved)


def test_failed_barrier_recreation_still_blocks_access_by_journal(legacy_workspace, monkeypatch):
    from newsroom.macos import MacOSBatchController
    from test_macos import FakeDialogs
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_release, real_acquire = service._release_barrier, service._acquire_barrier
    def unlink_then_damage():
        real_release()
        service.receipt_path.write_bytes(b'{')
        raise KeyboardInterrupt('receipt damaged after release')
    def fail_recreation():
        if service._execution['stage'] == 'CLEANED':
            raise OSError('one-shot barrier recreation failed')
        return real_acquire()
    monkeypatch.setattr(service, '_release_barrier', unlink_then_damage)
    monkeypatch.setattr(service, '_acquire_barrier', fail_recreation)
    with pytest.raises(WorkflowError, match='committed.*recovery'):
        service.execute(plan, backup)
    assert not (f.room.root / 'data/workspace-migration-lock.json').exists()
    result = CliRunner().invoke(app, ['--root', str(f.room.root), 'stats'])
    assert result.exit_code == 2 and 'recovery' in result.output.lower(), result.output
    dialogs = FakeDialogs()
    assert MacOSBatchController(f.room, dialogs).run(f.batch_id, 'approve') == 2
    assert not dialogs.confirmations


def test_durably_cleaned_state_never_rolls_back_if_journal_is_unreadable(legacy_workspace, monkeypatch):
    f = legacy_workspace
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    real_release = service._release_barrier
    committed = {}
    def release_then_corrupt_journal():
        committed['database'] = f.room.db_path.read_bytes()
        committed['artifacts'] = artifact_snapshot(f.room)
        real_release()
        Path(service._execution['journal_path']).write_bytes(b'{')
        raise KeyboardInterrupt('journal damaged after durable CLEANED')
    monkeypatch.setattr(service, '_release_barrier', release_then_corrupt_journal)
    with pytest.raises(BaseException) as captured:
        service.execute(plan, backup)
    assert isinstance(captured.value, WorkflowError) and 'committed' in str(captured.value)
    assert f.room.db_path.read_bytes() == committed['database']
    assert artifact_snapshot(f.room) == committed['artifacts']
    assert service.receipt_path.exists()
    result = CliRunner().invoke(app, ['--root', str(f.room.root), 'stats'])
    assert result.exit_code == 2 and 'recovery' in result.output.lower()


def test_rollback_preserves_whole_tree_when_prescan_cannot_read_it(legacy_workspace, monkeypatch):
    f = legacy_workspace
    prepare_existing_canonical(f)
    service = migrator(f.room)
    plan = service.preflight()
    backup = service.backup(plan)
    before = artifact_snapshot(f.room)
    real_matches = service._promoted_matches
    failed = False
    def unreadable(path, **kwargs):
        nonlocal failed
        if path == service.workspace.root / 'batches' and not failed:
            failed = True
            raise OSError('cannot read promoted tree during prescan')
        return real_matches(path, **kwargs)
    def interrupt(*args):
        raise KeyboardInterrupt('rollback required')
    monkeypatch.setattr(service, '_promoted_matches', unreadable)
    monkeypatch.setattr(service, '_verify_workspace', interrupt)
    with pytest.raises(BaseExceptionGroup):
        service.execute(plan, backup)
    assert artifact_snapshot(f.room) == before
    journal = json.loads(next((f.room.root / 'data/workspace-migration-journals').glob('*.json')).read_bytes())
    record = next(record for record in journal['quarantined_promotions'] if Path(record['source']).name == 'batches')
    assert Path(record['path']).is_dir()
    assert journal['recovery_status'] == 'RECOVERY_REQUIRED'


@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
def test_terminal_access_validation_is_read_only(legacy_workspace, journal_mode):
    from newsroom.migration import assert_migration_access
    f = legacy_workspace
    execute_clean_migration(f)
    with f.room.db.connect() as connection:
        connection.execute(f'PRAGMA journal_mode={journal_mode}')
    before = filesystem_details(f.room.root)
    assert_migration_access(f.room.root)
    assert filesystem_details(f.room.root) == before


@pytest.mark.parametrize('mutation', ['missing_release', 'pending_finalization', 'receipt_mismatch', 'missing_retired_tree', 'corrupt_database'])
def test_cleaned_journal_without_matching_terminal_invariants_refuses_access(legacy_workspace, mutation):
    f = legacy_workspace
    result = execute_clean_migration(f)
    path = Path(result['journal_path'])
    journal = json.loads(path.read_bytes())
    if mutation == 'missing_release':
        journal.pop('access_released')
    elif mutation == 'pending_finalization':
        journal['finalization_status'] = 'PENDING'
    elif mutation == 'receipt_mismatch':
        receipt = json.loads(migrator(f.room).receipt_path.read_bytes())
        receipt['manifest_sha256'] = '0' * 64
        migrator(f.room).receipt_path.write_text(json.dumps(receipt))
    elif mutation == 'missing_retired_tree':
        Path(journal['retired_stages'][0]['path']).rename(Path(journal['retired_stages'][0]['path']).with_name('human-moved-retired'))
    else:
        f.room.db_path.write_bytes(b'not a SQLite database')
    path.write_text(json.dumps(journal))
    before = snapshot(f.room.root)
    admission = CliRunner().invoke(app, ['--root', str(f.room.root), 'stats'])
    assert admission.exit_code == 2 and 'recovery' in admission.output.lower(), admission.output
    assert snapshot(f.room.root) == before


@pytest.mark.parametrize('pending', ['recovery_failures', 'finalization_status'])
def test_rollback_journal_with_pending_diagnostics_refuses_access(legacy_workspace, pending):
    from newsroom.migration import assert_migration_access
    f = legacy_workspace
    directory = f.room.root / 'data/workspace-migration-journals'
    directory.mkdir()
    path = directory / 'rollback.json'
    journal = {'schema_version': 1, 'stage': 'PROMOTED', 'history': [], 'journal_path': str(path),
               'recovery_status': 'ROLLED_BACK', 'rollback_verified': True, 'access_released': True,
               pending: ['restore remains pending'] if pending == 'recovery_failures' else 'RECOVERY_REQUIRED'}
    path.write_text(json.dumps(journal))
    with pytest.raises(WorkflowError, match='recovery'):
        assert_migration_access(f.room.root)
