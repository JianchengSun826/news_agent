from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from newsroom.cli import app
from newsroom.migration import WorkspaceMigrator
from newsroom.service import Newsroom, WorkflowError
from test_batch import candidate_row, write_batch_csv


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
    verify = service.verify_backup
    def corrupt_then_verify(path):
        (path / 'artifacts.tar.gz').write_bytes(b'corrupt archive')
        verify(path)
    monkeypatch.setattr(service, 'verify_backup', corrupt_then_verify)
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
    real_remove = shutil.rmtree
    def reject_backup_removal(path, *args, **kwargs):
        if Path(path).name.startswith('workspace-migration-'):
            raise OSError('backup removal unavailable')
        return real_remove(path, *args, **kwargs)
    def fail_verification(path):
        raise WorkflowError('backup verification failed: injected corruption')
    monkeypatch.setattr('newsroom.migration.shutil.rmtree', reject_backup_removal)
    monkeypatch.setattr(service, 'verify_backup', fail_verification)
    with pytest.raises(WorkflowError, match='backup verification'):
        service.backup(service.preflight())
    incomplete = list((f.room.root / 'data/backups').glob('workspace-migration-*'))
    assert len(incomplete) == 1
    assert (incomplete[0] / 'INCOMPLETE').is_file()
    assert not (f.room.root / 'workspace').exists()
