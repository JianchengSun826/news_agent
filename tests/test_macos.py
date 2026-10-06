from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from newsroom.cli import app
from newsroom.macos import MacOSBatchController, OsaScriptDialogs
from newsroom.workspace import BatchWorkspace, BatchWorkspacePaths
from test_batch import (
    newsroom, prepared_batch, content_batch, write_content_result,
    write_fact_check_result, write_revision_result,
)
from test_batch_release import all_pass_batch, ready_batch, approved_batch, snapshot


class FakeDialogs:
    def __init__(self, *, files=(), choices=(), confirms=()):
        self.files = iter(files)
        self.choices = iter(choices)
        self.confirms = iter(confirms)
        self.errors = []
        self.alerts = []
        self.opened = []
        self.urls = []
        self.revealed = []
        self.pickers = []
        self.lists = []
        self.confirmations = []

    def alert(self, title, message, *, error=False):
        self.alerts.append((title, message))
        if error:
            self.errors.append(message)

    def confirm(self, title, message):
        self.confirmations.append((title, message))
        return next(self.confirms, True)

    def choose_file(self, title, prompt):
        self.pickers.append((title, prompt))
        return next(self.files, None)

    def choose_list(self, title, prompt, choices):
        self.lists.append(list(choices))
        answer = next(self.choices, 0)
        return None if answer is None else choices[answer]

    def reveal_file(self, path):
        self.revealed.append(path)

    def open_file(self, path):
        self.opened.append(path)

    def open_url(self, url):
        self.urls.append(url)


def db_snapshot(batch):
    return {table: batch.room.db.all(f'SELECT * FROM {table}') for table in (
        'articles', 'images', 'image_candidates', 'state_transitions', 'batches',
        'batch_items', 'batch_requests',
    )}


@pytest.mark.parametrize('mutation', ['batch', 'request', 'output', 'schema', 'membership', 'malformed'])
def test_import_rejects_invalid_result_without_mutation(prepared_batch, tmp_path, mutation):
    path = write_content_result(prepared_batch, tmp_path / 'result.json')
    payload = json.loads(path.read_text())
    if mutation == 'batch':
        payload['batch_id'] = 'batch-other'
    elif mutation == 'request':
        payload['request_id'] = 'req-other'
    elif mutation == 'output':
        payload['output_file'] = 'wrong.json'
    elif mutation == 'schema':
        payload['schema_version'] = 99
    elif mutation == 'membership':
        payload['items'].pop()
    path.write_text('not JSON' if mutation == 'malformed' else json.dumps(payload))
    before = db_snapshot(prepared_batch)
    dialogs = FakeDialogs(files=[path])
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'import-result') == 2
    assert db_snapshot(prepared_batch) == before
    assert dialogs.errors
    assert '请' in dialogs.errors[-1]
    if mutation in {'batch', 'request', 'output'}:
        assert dialogs.errors[-1].startswith('所选文件不属于当前批次')
    assert not list(BatchWorkspacePaths(BatchWorkspace(prepared_batch.room).batch_root(prepared_batch.batch_id)).archive.glob('*.json'))


@pytest.mark.parametrize('action', ['import-result', 'register-images', 'review', 'approve', 'build-packages'])
def test_cancel_is_noop(prepared_batch, action):
    before = db_snapshot(prepared_batch)
    dialogs = FakeDialogs(confirms=[False])
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, action) == 0
    assert db_snapshot(prepared_batch) == before


@pytest.mark.parametrize('count', [0, 1, 2])
def test_import_selects_by_identity_and_archives_success(prepared_batch, tmp_path, count):
    root = BatchWorkspace(prepared_batch.room).batch_root(prepared_batch.batch_id)
    inbox = BatchWorkspacePaths(root).inbox
    wrong = json.loads(write_content_result(prepared_batch, inbox / 'wrong.json').read_text())
    wrong['request_id'] = 'req-other'
    (inbox / 'wrong.json').write_text(json.dumps(wrong))
    (inbox / 'malformed.json').write_text('{')
    external = write_content_result(prepared_batch, tmp_path / 'unusual-name.json')
    for index in range(count):
        write_content_result(prepared_batch, inbox / f'matching-{index}.json')
    dialogs = FakeDialogs(files=[external], choices=[count - 1])
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'import-result') == 0
    assert all(prepared_batch.room.get_article(article_id)['status'] == 'DRAFTED' for article_id in prepared_batch.article_ids)
    assert (BatchWorkspacePaths(root).archive / 'content-result.json').is_file()
    assert len(dialogs.pickers) == (1 if count == 0 else 0)
    assert len(dialogs.lists) == (1 if count == 2 else 0)
    assert json.loads((root / 'batch-state.json').read_text())['next_action'] == 'open-next-request'


def test_import_single_match_declined_is_noop(prepared_batch):
    inbox = BatchWorkspacePaths(BatchWorkspace(prepared_batch.room).batch_root(prepared_batch.batch_id)).inbox
    write_content_result(prepared_batch, inbox / 'result.json')
    before = db_snapshot(prepared_batch)
    dialogs = FakeDialogs(confirms=[False])
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'import-result') == 0
    assert db_snapshot(prepared_batch) == before


def test_import_fact_check_and_revision_routes_to_current_request(content_batch, tmp_path):
    dialogs = FakeDialogs(files=[write_fact_check_result(content_batch, tmp_path / 'checks.json', ['FAIL', 'PASS'])])
    controller = MacOSBatchController(content_batch.room, dialogs)
    assert controller.run(content_batch.batch_id, 'import-result') == 0
    assert controller.run(content_batch.batch_id, 'open-next-request') == 0
    request = content_batch.room.db.one("SELECT * FROM batch_requests WHERE request_type='REVISION' AND status='PENDING'")
    request_path = content_batch.room.root / request['request_path']
    assert request_path in dialogs.opened
    dialogs.files = iter([write_revision_result(json.loads(request_path.read_text()), tmp_path / 'revision.json')])
    assert controller.run(content_batch.batch_id, 'import-result') == 0
    assert content_batch.room.db.one('SELECT status FROM batch_requests WHERE request_id=?', (request['request_id'],))['status'] == 'COMPLETED'
    assert content_batch.room.db.one("SELECT revision_number FROM batch_requests WHERE request_type='FACT_CHECK' AND status='PENDING'")['revision_number'] == 2


def test_image_selection_saves_each_selection_and_resumes(all_pass_batch, tmp_path):
    batch = all_pass_batch
    downloads = [tmp_path / 'download-1.jpg', tmp_path / 'download-2.jpg']
    for path in downloads:
        path.write_bytes(b'local downloaded image')
    before = snapshot(batch)
    first = FakeDialogs(files=[downloads[0], None])
    controller = MacOSBatchController(batch.room, first)
    assert controller.run(batch.batch_id, 'register-images') == 0
    assert snapshot(batch) == before
    draft = BatchWorkspace(batch.room).image_draft_path(batch.batch_id)
    selections = json.loads(draft.read_text())['items']
    assert len(selections) == 1
    assert selections[0]['rights_confirmed'] is True
    assert len(first.urls) == 4
    second = FakeDialogs(files=[downloads[1]])
    assert MacOSBatchController(batch.room, second).run(batch.batch_id, 'register-images') == 0
    assert len(second.pickers) == 1
    assert len(second.urls) == 2
    assert batch.room.db.one('SELECT COUNT(*) AS n FROM images')['n'] == 2
    assert all(batch.room.get_article(article_id)['status'] == 'IMAGE_READY' for article_id in batch.article_ids)
    assert len(json.loads(draft.read_text())['items']) == 2


def test_image_rights_decline_does_not_save_or_apply(all_pass_batch, tmp_path):
    download = tmp_path / 'download.jpg'
    download.write_bytes(b'local image')
    before = snapshot(all_pass_batch)
    dialogs = FakeDialogs(files=[download], confirms=[True, False])
    assert MacOSBatchController(all_pass_batch.room, dialogs).run(all_pass_batch.batch_id, 'register-images') == 0
    assert snapshot(all_pass_batch) == before
    assert not BatchWorkspace(all_pass_batch.room).image_draft_path(all_pass_batch.batch_id).exists()


def test_image_chooser_ignores_inactive_articles(all_pass_batch, tmp_path):
    ignored = all_pass_batch.article_ids[1]
    with all_pass_batch.room.db.connect() as connection:
        connection.execute("UPDATE batch_items SET item_status='EXCLUDED' WHERE article_id=?", (ignored,))
    download = tmp_path / 'download.jpg'
    download.write_bytes(b'image')
    dialogs = FakeDialogs(files=[download])
    assert MacOSBatchController(all_pass_batch.room, dialogs).run(all_pass_batch.batch_id, 'register-images') == 0
    assert len(dialogs.pickers) == 1
    assert all_pass_batch.room.get_article(ignored)['status'] == 'FACT_CHECKED'
    assert all_pass_batch.room.db.one('SELECT COUNT(*) AS n FROM images')['n'] == 1


def test_confirmed_review_approve_and_packages_use_typed_gates(ready_batch):
    dialogs = FakeDialogs()
    controller = MacOSBatchController(ready_batch.room, dialogs)
    for action, state in [('review', 'REVIEWED'), ('approve', 'APPROVED'), ('build-packages', 'APPROVED')]:
        assert controller.run(ready_batch.batch_id, action) == 0
        assert all(ready_batch.room.get_article(article_id)['status'] == state for article_id in ready_batch.article_ids)
    transitions = ready_batch.room.db.all("SELECT * FROM state_transitions WHERE to_status IN ('REVIEWED','APPROVED')")
    assert all(row['actor'] == 'editor' and '确认' in row['reason'] for row in transitions)
    assert (BatchWorkspace(ready_batch.room).batch_root(ready_batch.batch_id) / 'publish' / 'batch-manifest.json').is_file()


def test_service_gate_error_is_actionable_chinese(prepared_batch):
    before = db_snapshot(prepared_batch)
    dialogs = FakeDialogs()
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'approve') == 2
    assert db_snapshot(prepared_batch) == before
    assert '请' in dialogs.errors[-1]


def test_open_batch_reports_status_and_opens_current_request(prepared_batch):
    dialogs = FakeDialogs()
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'open-batch') == 0
    root = BatchWorkspace(prepared_batch.room).batch_root(prepared_batch.batch_id)
    assert root in dialogs.opened
    assert root / 'README.md' in dialogs.opened
    assert prepared_batch.request_path in dialogs.opened
    assert any('READY_FOR_CONTENT' in message and '下一步' in message for _, message in dialogs.alerts)


def test_open_review_is_read_only(content_batch):
    before = db_snapshot(content_batch)
    root = BatchWorkspace(content_batch.room).batch_root(content_batch.batch_id)
    paths = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
    dialogs = FakeDialogs()
    assert MacOSBatchController(content_batch.room, dialogs).run(content_batch.batch_id, 'open-review') == 0
    assert db_snapshot(content_batch) == before
    assert {path: path.read_bytes() for path in root.rglob('*') if path.is_file()} == paths
    assert root / 'articles' in dialogs.opened


def test_unknown_action_rejected_before_any_mutation(prepared_batch):
    before = db_snapshot(prepared_batch)
    dialogs = FakeDialogs()
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'delete') == 2
    assert db_snapshot(prepared_batch) == before
    assert not dialogs.opened
    assert dialogs.errors


def test_expected_oserror_is_dialog_but_programming_errors_escape(prepared_batch, monkeypatch):
    dialogs = FakeDialogs()
    def fail_os(*args):
        raise OSError('permission denied')
    monkeypatch.setattr(BatchWorkspace, 'refresh', fail_os)
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'refresh') == 2
    assert '请' in dialogs.errors[-1]
    def bug(*args):
        raise RuntimeError('programming bug')
    monkeypatch.setattr(BatchWorkspace, 'refresh', bug)
    with pytest.raises(RuntimeError, match='programming bug'):
        MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'refresh')


def test_hidden_command_dispatches_real_controller(prepared_batch, monkeypatch):
    dialogs = FakeDialogs()
    monkeypatch.setattr('newsroom.cli.OsaScriptDialogs', lambda: dialogs)
    runner = CliRunner()
    result = runner.invoke(app, ['--root', str(prepared_batch.room.root), 'macos-action', prepared_batch.batch_id, 'open-review'])
    assert result.exit_code == 0, result.output
    assert dialogs.opened
    assert 'macos-action' not in runner.invoke(app, ['--help']).output


def test_osascript_receives_dynamic_values_only_as_argv(monkeypatch, tmp_path):
    calls = []
    responses = iter(['', 'true\n', str(tmp_path / 'chosen.jpg') + '\n', 'choice\n', '', '', ''])
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, next(responses), '')
    monkeypatch.setattr('newsroom.macos.subprocess.run', run)
    monkeypatch.setattr('newsroom.macos.sys.platform', 'darwin')
    dialogs = OsaScriptDialogs()
    value = '值 " $(touch injected) `bad` \\ newline\nend'
    dialogs.alert(value, value, error=True)
    assert dialogs.confirm(value, value)
    assert dialogs.choose_file(value, value) == tmp_path / 'chosen.jpg'
    assert dialogs.choose_list(value, value, ['choice', value]) == 'choice'
    dialogs.reveal_file(tmp_path / value)
    dialogs.open_file(tmp_path / value)
    dialogs.open_url('https://example.org/' + value)
    assert len(calls) == 7
    for argv in calls:
        assert argv[:2] == ['/usr/bin/osascript', '-e']
        assert 'on run argv' in argv[2]
        assert value not in argv[2]
        assert any(value in item for item in argv[3:])


@pytest.mark.parametrize('operation', ['confirm', 'choose_file', 'choose_list'])
def test_native_dialog_cancellation_is_noop(monkeypatch, operation):
    monkeypatch.setattr('newsroom.macos.sys.platform', 'darwin')
    monkeypatch.setattr('newsroom.macos.subprocess.run', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, 'NEWSROOM_CANCELLED\n', ''))
    dialogs = OsaScriptDialogs()
    args = ('title', 'prompt', ['one']) if operation == 'choose_list' else ('title', 'prompt')
    assert not getattr(dialogs, operation)(*args)


def test_multiple_matching_results_chooser_cancel_is_noop(prepared_batch):
    inbox = BatchWorkspacePaths(BatchWorkspace(prepared_batch.room).batch_root(prepared_batch.batch_id)).inbox
    for filename in ['one.json', 'two.json']:
        write_content_result(prepared_batch, inbox / filename)
    before = db_snapshot(prepared_batch)
    dialogs = FakeDialogs(choices=[None])
    assert MacOSBatchController(prepared_batch.room, dialogs).run(prepared_batch.batch_id, 'import-result') == 0
    assert db_snapshot(prepared_batch) == before
    assert not dialogs.confirmations


def test_image_candidate_chooser_cancel_preserves_draft(all_pass_batch):
    before = db_snapshot(all_pass_batch)
    dialogs = FakeDialogs(choices=[None])
    assert MacOSBatchController(all_pass_batch.room, dialogs).run(all_pass_batch.batch_id, 'register-images') == 0
    assert db_snapshot(all_pass_batch) == before
    assert not BatchWorkspace(all_pass_batch.room).image_draft_path(all_pass_batch.batch_id).exists()


def test_error_dialog_unavailable_returns_two_with_actionable_message(prepared_batch, capsys):
    class UnavailableDialogs(FakeDialogs):
        def alert(self, title, message, *, error=False):
            raise OSError('dialog permission denied')
    assert MacOSBatchController(prepared_batch.room, UnavailableDialogs()).run(prepared_batch.batch_id, 'delete') == 2
    assert '请' in capsys.readouterr().err


def test_native_process_failure_becomes_oserror(monkeypatch):
    monkeypatch.setattr('newsroom.macos.sys.platform', 'darwin')
    monkeypatch.setattr('newsroom.macos.subprocess.run', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, '', 'AppleScript permission denied'))
    with pytest.raises(OSError, match='permission denied'):
        OsaScriptDialogs().open_file(Path('/tmp/anything'))


def test_interruptions_are_never_swallowed(prepared_batch, monkeypatch):
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(BatchWorkspace, 'refresh', interrupt)
    with pytest.raises(KeyboardInterrupt):
        MacOSBatchController(prepared_batch.room, FakeDialogs()).run(prepared_batch.batch_id, 'refresh')


def test_hidden_dispatcher_is_registered_when_cli_runs_as_module():
    import sys
    result = subprocess.run(
        [sys.executable, '-m', 'newsroom.cli', 'macos-action', '--help'],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert 'BATCH_ID' in result.stdout
