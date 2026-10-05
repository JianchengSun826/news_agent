from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from newsroom.batch_models import BatchImagePlan
from newsroom.batch_release import BatchReleaseWorkflow
from newsroom.service import WorkflowError

from test_batch import (
    content_batch, newsroom, prepared_batch, write_fact_check_result,
    duplicate_rows, write_batch_csv, write_content_result,
)


@pytest.fixture
def all_pass_batch(content_batch: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:
    path = write_fact_check_result(content_batch, tmp_path / "checks.json", ["PASS", "PASS"])
    content_batch.workflow.import_fact_checks(content_batch.batch_id, path)
    return content_batch


def image_plan(batch: SimpleNamespace, tmp_path: Path) -> dict:
    items = []
    for index, article_id in enumerate(batch.article_ids):
        source = tmp_path / f"download-{index}.JPG"
        source.write_bytes(f"local image {index}".encode())
        candidate = batch.room.list_image_candidates(article_id)[0]
        items.append({
            "article_id": article_id,
            "image_candidate_id": candidate["image_candidate_id"],
            "image_file": str(source),
            "rights_confirmed": True,
        })
    return {"schema_version": 1, "batch_id": batch.batch_id, "items": items}


def write_plan(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def snapshot(batch: SimpleNamespace) -> dict:
    return {table: batch.room.db.all(f"SELECT * FROM {table}") for table in (
        "articles", "images", "image_candidates", "state_transitions", "batch_items", "batches",
    )}


def assert_no_image_files(batch: SimpleNamespace) -> None:
    assert not list((batch.room.root / "workspace").rglob("hero.*"))
    assert not list((batch.room.root / "workspace").rglob(".staging-images-*"))


def test_image_plan_requires_every_active_article_to_pass(content_batch, tmp_path: Path):
    path = write_fact_check_result(content_batch, tmp_path / "checks.json", ["PASS", "FAIL"])
    content_batch.workflow.import_fact_checks(content_batch.batch_id, path)
    before = snapshot(content_batch)
    with pytest.raises(WorkflowError, match="all included articles must pass"):
        BatchReleaseWorkflow(content_batch.room).apply_image_plan(content_batch.batch_id, tmp_path / "absent.json")
    assert snapshot(content_batch) == before
    assert_no_image_files(content_batch)


@pytest.mark.parametrize("mutation,message", [
    ("missing", "missing article"),
    ("extra", "unexpected article"),
    ("duplicate", "duplicate article"),
    ("foreign_candidate", "different article"),
    ("missing_candidate", "image candidate not found"),
    ("selected_candidate", "already selected"),
    ("missing_file", "image file not found"),
    ("directory", "regular file"),
    ("unsafe_suffix", "suffix"),
    ("no_suffix", "suffix"),
    ("wrong_batch", "batch_id"),
    ("already_ready", "FACT_CHECKED"),
])
def test_invalid_image_plan_changes_nothing(all_pass_batch, tmp_path, mutation, message):
    payload = image_plan(all_pass_batch, tmp_path)
    if mutation == "missing":
        payload["items"].pop()
    elif mutation == "extra":
        payload["items"].append({**payload["items"][0], "article_id": "art-extra"})
    elif mutation == "duplicate":
        payload["items"].append(payload["items"][0])
    elif mutation == "foreign_candidate":
        payload["items"][1]["image_candidate_id"] = payload["items"][0]["image_candidate_id"]
    elif mutation == "missing_candidate":
        payload["items"][1]["image_candidate_id"] = "imgcand-missing"
    elif mutation == "selected_candidate":
        with all_pass_batch.room.db.connect() as connection:
            connection.execute("UPDATE image_candidates SET review_status='SELECTED' WHERE article_id=?", (all_pass_batch.article_ids[1],))
    elif mutation in {"missing_file", "directory", "unsafe_suffix", "no_suffix"}:
        source = tmp_path / {"missing_file": "absent.jpg", "directory": "folder.jpg", "unsafe_suffix": "image.bad-suffix", "no_suffix": "image"}[mutation]
        if mutation == "directory":
            source.mkdir()
        elif mutation != "missing_file":
            source.write_bytes(b"image")
        payload["items"][1]["image_file"] = str(source)
    elif mutation == "wrong_batch":
        payload["batch_id"] = "batch-wrong"
    elif mutation == "already_ready":
        with all_pass_batch.room.db.connect() as connection:
            connection.execute("UPDATE articles SET status='IMAGE_READY' WHERE article_id=?", (all_pass_batch.article_ids[1],))
    before = snapshot(all_pass_batch)
    with pytest.raises(WorkflowError, match=message):
        BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / "plan.json", payload))
    assert snapshot(all_pass_batch) == before
    assert_no_image_files(all_pass_batch)


@pytest.mark.parametrize("rights", [False, 1, "true", None])
def test_image_plan_requires_literal_rights_confirmation(all_pass_batch, tmp_path, rights):
    payload = image_plan(all_pass_batch, tmp_path)
    payload["items"][1]["rights_confirmed"] = rights
    before = snapshot(all_pass_batch)
    with pytest.raises(WorkflowError, match="rights_confirmed"):
        BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / "plan.json", payload))
    assert snapshot(all_pass_batch) == before
    assert_no_image_files(all_pass_batch)


def test_cancelled_image_selection_draft_is_resumable(all_pass_batch, tmp_path):
    payload = image_plan(all_pass_batch, tmp_path)
    payload["items"].pop()
    workflow = BatchReleaseWorkflow(all_pass_batch.room)
    before = snapshot(all_pass_batch)
    path = workflow.save_image_draft(all_pass_batch.batch_id, BatchImagePlan.model_validate(payload))
    assert path == tmp_path / "workspace" / "batches" / all_pass_batch.batch_id / "image-selections.json"
    assert workflow.load_image_draft(all_pass_batch.batch_id).model_dump(mode="json") == payload
    assert snapshot(all_pass_batch) == before
    with pytest.raises(WorkflowError, match="missing article"):
        workflow.apply_image_plan(all_pass_batch.batch_id, path)
    assert snapshot(all_pass_batch) == before
    assert_no_image_files(all_pass_batch)


def test_image_draft_atomic_write_preserves_previous_selection(all_pass_batch, tmp_path, monkeypatch):
    import newsroom.workspace as workspace

    workflow = BatchReleaseWorkflow(all_pass_batch.room)
    payload = image_plan(all_pass_batch, tmp_path)
    payload["items"].pop()
    path = workflow.save_image_draft(all_pass_batch.batch_id, BatchImagePlan.model_validate(payload))
    original = path.read_bytes()
    def fail_replace(*args):
        raise OSError("draft replacement failed")
    monkeypatch.setattr(workspace.os, "replace", fail_replace)
    with pytest.raises(OSError, match="draft replacement failed"):
        workflow.save_image_draft(all_pass_batch.batch_id, BatchImagePlan.model_validate(image_plan(all_pass_batch, tmp_path)))
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_image_plan_registers_provenance_and_refreshes_views(all_pass_batch, tmp_path):
    payload = image_plan(all_pass_batch, tmp_path)
    result = BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / "plan.json", payload))
    assert result["batch_id"] == all_pass_batch.batch_id
    assert result["registered"] == 2
    for index, item in enumerate(payload["items"]):
        article = all_pass_batch.room.get_article(item["article_id"])
        image = all_pass_batch.room.db.one("SELECT * FROM images WHERE image_id=?", (article["image_id"],))
        assert article["status"] == "IMAGE_READY"
        assert image["image_path"] == f"workspace/batches/{all_pass_batch.batch_id}/articles/{item['article_id']}/hero.jpg"
        assert (tmp_path / image["image_path"]).read_bytes() == f"local image {index}".encode()
        candidate = all_pass_batch.room.list_image_candidates(item["article_id"])[0]
        assert candidate["review_status"] == "SELECTED"
        assert image["image_candidate_id"] == item["image_candidate_id"]
        assert image["source_url"] == candidate["landing_page_url"]
        assert image["license_url"] == candidate["rights_url"]
        assert image["rights_basis"] == candidate["rights_basis"]
        assert image["rights_statement"] == candidate["rights_statement"]
        assert all_pass_batch.room.audit(item["article_id"])[-1]["to_status"] == "IMAGE_READY"
    state = json.loads((tmp_path / "workspace/batches" / all_pass_batch.batch_id / "batch-state.json").read_text())
    assert all(item["image_state"] == "REGISTERED" for item in state["items"])
    assert state["next_action"] == "review"
    assert not list((tmp_path / "workspace").rglob(".staging-images-*"))


@pytest.mark.parametrize("stage", ["copy", "promotion", "database"])
def test_image_plan_failure_rolls_back_files_database_and_audits(all_pass_batch, tmp_path, monkeypatch, stage):
    import newsroom.batch_release as release

    payload = image_plan(all_pass_batch, tmp_path)
    before = snapshot(all_pass_batch)
    if stage == "database":
        with all_pass_batch.room.db.connect() as connection:
            connection.execute(f"CREATE TRIGGER reject_second_image BEFORE INSERT ON images WHEN NEW.article_id='{all_pass_batch.article_ids[1]}' BEGIN SELECT RAISE(ABORT,'second image rejected'); END")
        error_type, message = sqlite3.IntegrityError, "second image rejected"
    else:
        calls = 0
        original = release.shutil.copy2 if stage == "copy" else release.os.link
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(f"second {stage} failed")
            return original(*args, **kwargs)
        monkeypatch.setattr(release.shutil if stage == "copy" else release.os, "copy2" if stage == "copy" else "link", fail_second)
        error_type, message = OSError, f"second {stage} failed"
    with pytest.raises(error_type, match=message):
        BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / "plan.json", payload))
    assert snapshot(all_pass_batch) == before
    assert_no_image_files(all_pass_batch)


def test_image_plan_promotes_every_file_before_database_updates(all_pass_batch, tmp_path, monkeypatch):
    payload = image_plan(all_pass_batch, tmp_path)
    original = all_pass_batch.room._transition
    def check_files(connection, *args):
        for article_id in all_pass_batch.article_ids:
            assert (tmp_path / "workspace/batches" / all_pass_batch.batch_id / "articles" / article_id / "hero.jpg").is_file()
        return original(connection, *args)
    monkeypatch.setattr(all_pass_batch.room, "_transition", check_files)
    BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / "plan.json", payload))


def test_image_plan_preserves_existing_destination(all_pass_batch, tmp_path):
    payload = image_plan(all_pass_batch, tmp_path)
    destination = tmp_path / "workspace/batches" / all_pass_batch.batch_id / "articles" / all_pass_batch.article_ids[1] / "hero.jpg"
    destination.write_bytes(b"existing file")
    before = snapshot(all_pass_batch)
    with pytest.raises(WorkflowError, match="already exists"):
        BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / "plan.json", payload))
    assert destination.read_bytes() == b"existing file"
    assert snapshot(all_pass_batch) == before
    assert len(list((tmp_path / "workspace").rglob("hero.*"))) == 1


@pytest.mark.parametrize("excluded_status", ["EXCLUDED", "REUSED_COMPLETE"])
def test_image_plan_excludes_inactive_articles(all_pass_batch, tmp_path, excluded_status):
    payload = image_plan(all_pass_batch, tmp_path)
    payload["items"].pop()
    ignored_id = all_pass_batch.article_ids[1]
    with all_pass_batch.room.db.connect() as connection:
        connection.execute("UPDATE batch_items SET item_status=? WHERE article_id=?", (excluded_status, ignored_id))
        connection.execute("UPDATE articles SET fact_check_outcome='FAIL' WHERE article_id=?", (ignored_id,))
    BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / "plan.json", payload))
    assert all_pass_batch.room.get_article(ignored_id)["status"] == "FACT_CHECKED"
    assert all_pass_batch.room.db.one("SELECT COUNT(*) AS count FROM images")["count"] == 1


def test_image_plan_refreshes_current_and_owner_batch(prepared_batch, tmp_path):
    batch = prepared_batch
    reused = batch.workflow.prepare(write_batch_csv(tmp_path / "reuse.csv", duplicate_rows()[::2], line_ending="\r\n"))
    imported = batch.workflow.import_content(batch.batch_id, write_content_result(batch, tmp_path / "content-result.json"))
    batch.fact_request = json.loads(Path(imported["fact_check_request_path"]).read_text())
    batch.workflow.import_fact_checks(batch.batch_id, write_fact_check_result(batch, tmp_path / "checks.json", ["PASS", "PASS"]))
    payload = image_plan(batch, tmp_path)
    payload["batch_id"] = reused["batch_id"]
    BatchReleaseWorkflow(batch.room).apply_image_plan(reused["batch_id"], write_plan(tmp_path / "plan.json", payload))
    for batch_id in (batch.batch_id, reused["batch_id"]):
        root = tmp_path / "workspace/batches" / batch_id
        state = json.loads((root / "batch-state.json").read_text())
        assert all(item["image_state"] == "REGISTERED" for item in state["items"])
    assert not list((tmp_path / "workspace/batches" / reused["batch_id"] / "articles").rglob("hero.*"))
