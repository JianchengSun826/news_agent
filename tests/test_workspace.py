from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from newsroom.batch import BatchWorkflow
from newsroom.service import Newsroom, WorkflowError
from newsroom.workspace import BatchWorkspace, BatchWorkspacePaths, render_action_script
from test_batch import candidate_row, write_batch_csv


@pytest.fixture
def prepared_batch(tmp_path: Path):
    room = Newsroom(tmp_path)
    room.init()
    source = write_batch_csv(tmp_path / "batch.csv", [candidate_row()])
    batch_id = BatchWorkflow(room).prepare(source)["batch_id"]
    return room, batch_id


def test_workspace_generates_dashboard_and_relocatable_actions(prepared_batch, tmp_path: Path):
    room, batch_id = prepared_batch
    workspace = BatchWorkspace(room)
    root = workspace.ensure_layout(batch_id)
    state = workspace.refresh(batch_id)
    assert root == tmp_path / "workspace" / "batches" / batch_id
    paths = BatchWorkspacePaths(root)
    for directory in (paths.requests, paths.inbox, paths.archive, paths.articles, paths.publish, root / "input"):
        assert directory.is_dir()
    article_id = state["items"][0]["article_id"]
    assert workspace.article_root(batch_id, article_id).is_dir()
    assert state["batch_id"] == batch_id
    assert state["counts"] == {"READY_FOR_CONTENT": 1}
    assert state["next_action"] == "upload-request"
    assert json.loads((root / "batch-state.json").read_text()) == state
    readme = (root / "README.md").read_text()
    assert "Example launches Tool" in readme
    assert "READY_FOR_CONTENT" in readme
    assert state["requests"][0]["request_path"] in readme
    assert str(tmp_path) not in readme
    assert batch_id in (tmp_path / "workspace" / "README.md").read_text()
    scripts = list(root.rglob("*.command"))
    assert len(scripts) == 5
    assert not list(workspace.article_root(batch_id, article_id).rglob("*.command"))
    for script in scripts:
        assert script.stat().st_mode & stat.S_IXUSR
        assert str(tmp_path) not in script.read_text()
        subprocess.run(["zsh", "-n", str(script)], check=True, capture_output=True)
    assert (root / "actions" / "02-导入Claude返回文件.command").is_file()


def test_refresh_rebuilds_views_from_database_and_does_not_mutate_state(prepared_batch):
    room, batch_id = prepared_batch
    workspace = BatchWorkspace(room)
    state = workspace.refresh(batch_id)
    root = workspace.batch_root(batch_id)
    first_readme = (root / "README.md").read_bytes()
    first_json = (root / "batch-state.json").read_bytes()
    assert workspace.refresh(batch_id) == state
    assert (root / "README.md").read_bytes() == first_readme
    assert (root / "batch-state.json").read_bytes() == first_json
    (root / "README.md").unlink()
    (root / "batch-state.json").write_text('{"status":"APPROVED"}')
    candidate_id = state["items"][0]["candidate_id"]
    with room.db.connect() as connection:
        connection.execute("UPDATE batch_items SET error_message=? WHERE batch_id=? AND candidate_id=?", ("校验 | 失败\n重新检查", batch_id, candidate_id))
    updated = workspace.refresh(batch_id)
    assert updated["items"][0]["error_message"] == "校验 | 失败\n重新检查"
    assert "重新检查" in (root / "README.md").read_text()
    assert room.db.one("SELECT workspace_version FROM batches WHERE batch_id=?", (batch_id,))["workspace_version"] == 1
    assert not list(root.glob("*.tmp"))


def test_action_bootstrap_survives_repository_relocation_and_unicode_path(tmp_path: Path):
    original = tmp_path / "original"
    script = original / "workspace" / "batches" / "batch-abc123" / "actions" / "action.command"
    script.parent.mkdir(parents=True)
    (original / "pyproject.toml").write_text("")
    script.write_text(render_action_script("batch-abc123", "import-result"))
    relocated = tmp_path / "新闻 编辑室"
    shutil.move(original, relocated)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/zsh\nprintf "%s\\n" "$@"\n')
    uv.chmod(0o755)
    result = subprocess.run(["zsh", str(relocated / script.relative_to(original))], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, check=True, capture_output=True, text=True)
    assert result.stdout.splitlines() == ["run", "--project", str(relocated), "newsroom", "--root", str(relocated), "macos-action", "batch-abc123", "import-result"]


def test_script_quotes_literal_arguments_without_evaluating_shell_content(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("")
    script = tmp_path / "action.command"
    literal = 'batch-"$(touch injected)`touch injected`\\'
    script.write_text(render_action_script(literal, "import-result"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/zsh\nprintf "%s\\n" "$@"\n')
    uv.chmod(0o755)
    result = subprocess.run(["zsh", str(script)], cwd=tmp_path, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, check=True, capture_output=True, text=True)
    assert result.stdout.splitlines()[-2:] == [literal, "import-result"]
    assert not (tmp_path / "injected").exists()


def test_workspace_rejects_unknown_batches_and_path_traversal(prepared_batch):
    room, _ = prepared_batch
    workspace = BatchWorkspace(room)
    with pytest.raises(WorkflowError, match="batch not found"):
        workspace.refresh("batch-missing")
    with pytest.raises(WorkflowError):
        workspace.batch_root("../escape")
    with pytest.raises(WorkflowError):
        workspace.article_root("batch-safe", "../../escape")
    assert not (room.root / "workspace" / "batches" / "batch-missing").exists()


def test_install_root_actions_sets_up_root_dashboard(prepared_batch):
    room, batch_id = prepared_batch
    path = BatchWorkspace(room).install_root_actions()
    assert path == room.root / "workspace" / "actions"
    assert path.is_dir()
    assert batch_id in (path.parent / "README.md").read_text()
    assert not (room.root / "Migrate Newsroom.command").exists()


def test_reused_article_links_to_owner_and_reports_canonical_release_state(prepared_batch):
    room, owner = prepared_batch
    article_id = room.db.one("SELECT article_id FROM batch_items WHERE batch_id=?", (owner,))["article_id"]
    with room.db.connect() as connection:
        connection.execute("UPDATE articles SET status='APPROVED',fact_check_outcome='PASS',image_id='img-selected' WHERE article_id=?", (article_id,))
        connection.execute("INSERT INTO batches(batch_id,source_filename,source_sha256,status,created_at,updated_at) VALUES ('batch-later','later.csv','later-hash','COMPLETE','2026-10-05','2026-10-05')")
        connection.execute("INSERT INTO batch_items(batch_id,candidate_id,article_id,item_status,created_at,updated_at) SELECT 'batch-later',candidate_id,article_id,'REUSED_COMPLETE',created_at,updated_at FROM batch_items WHERE batch_id=?", (owner,))
    workspace = BatchWorkspace(room)
    state = workspace.refresh("batch-later")
    item = state["items"][0]
    assert item["owning_batch_id"] == owner
    assert item["article_path"] == f"../{owner}/articles/{article_id}"
    assert item["fact_check_outcome"] == "PASS"
    assert item["image_state"] == "REGISTERED"
    assert item["approval_state"] == "APPROVED"
    assert not workspace.article_root("batch-later", article_id).exists()
    assert item["article_path"] in (workspace.batch_root("batch-later") / "README.md").read_text()


def test_action_fails_before_uv_when_repository_marker_is_missing(tmp_path: Path):
    script = tmp_path / "action.command"
    script.write_text(render_action_script("batch-safe", "refresh"))
    result = subprocess.run(["zsh", str(script)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Cannot locate newsroom pyproject.toml" in result.stderr


def test_failed_json_replacement_preserves_previous_view(prepared_batch, monkeypatch):
    room, batch_id = prepared_batch
    workspace = BatchWorkspace(room)
    workspace.refresh(batch_id)
    json_path = workspace.batch_root(batch_id) / "batch-state.json"
    previous = json_path.read_bytes()
    real_replace = os.replace

    def reject_json_replace(source, destination):
        if Path(destination) == json_path:
            raise OSError("view write failed")
        real_replace(source, destination)

    monkeypatch.setattr("newsroom.workspace.os.replace", reject_json_replace)
    with pytest.raises(OSError, match="view write failed"):
        workspace.refresh(batch_id)
    assert json_path.read_bytes() == previous
    assert not list(json_path.parent.glob("*.tmp"))


def test_dashboard_multiline_cell_escapes_source_html_before_line_breaks(prepared_batch):
    room, batch_id = prepared_batch
    with room.db.connect() as connection:
        connection.execute(
            "UPDATE batch_items SET error_message=? WHERE batch_id=?",
            ("校验 <script>失败</script>\n重新检查", batch_id),
        )
    workspace = BatchWorkspace(room)
    workspace.refresh(batch_id)
    readme = (workspace.batch_root(batch_id) / "README.md").read_text()
    assert "校验 &lt;script&gt;失败&lt;/script&gt;<br>重新检查" in readme
    assert "&lt;br&gt;" not in readme


def test_readonly_snapshot_does_not_create_missing_workspace(prepared_batch):
    room, batch_id = prepared_batch
    expected = BatchWorkspace(room).refresh(batch_id)
    shutil.rmtree(room.root / 'workspace')
    before_db = {table: room.db.all(f'SELECT * FROM {table}') for table in ('batches', 'batch_items', 'batch_requests', 'articles', 'images', 'state_transitions')}
    state = BatchWorkspace(room).snapshot(batch_id)
    assert state == expected
    assert not (room.root / 'workspace').exists()
    assert {table: room.db.all(f'SELECT * FROM {table}') for table in before_db} == before_db


def test_root_migration_launcher_bootstraps_after_relocation(prepared_batch, tmp_path):
    room, _ = prepared_batch
    BatchWorkspace(room).install_root_actions()
    source = room.root / 'workspace/Migrate Newsroom.command'
    assert source.is_file() and os.access(source, os.X_OK)
    assert 'rm ' not in source.read_text()
    assert subprocess.run(['zsh', '-n', str(source)], capture_output=True).returncode == 0
    relocated = tmp_path / 'relocated newsroom'
    relocated.mkdir()
    (relocated / 'pyproject.toml').write_text('[project]\nname="relocated"\n')
    (relocated / 'workspace').mkdir()
    script = relocated / 'workspace/Migrate Newsroom.command'
    shutil.copy2(source, script)
    bin_dir = tmp_path / 'migration-bin'
    bin_dir.mkdir()
    uv = bin_dir / 'uv'
    uv.write_text('#!/bin/zsh\nprintf "%s\\n" "$@"\n')
    uv.chmod(0o755)
    result = subprocess.run(['zsh', str(script)], cwd=tmp_path, env={**os.environ, 'PATH': f"{bin_dir}:{os.environ['PATH']}"}, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ['run', '--project', str(relocated), 'newsroom', '--root', str(relocated), 'macos-migrate']
