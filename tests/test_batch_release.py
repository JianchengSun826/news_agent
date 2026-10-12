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
    assert state["next_action"] == "export"
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


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("stage", ["promotion", "database"])
def test_image_plan_interruption_cleans_files_rolls_back_and_allows_retry(
    all_pass_batch, tmp_path, monkeypatch, interruption_type, stage,
):
    import newsroom.batch_release as release

    payload = image_plan(all_pass_batch, tmp_path)
    plan_path = write_plan(tmp_path / "plan.json", payload)
    before = snapshot(all_pass_batch)
    interruption = interruption_type("image operation interrupted")
    workflow = BatchReleaseWorkflow(all_pass_batch.room)
    original = release.os.link if stage == "promotion" else all_pass_batch.room._transition
    calls = 0

    def interrupt_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if stage == "database":
            # Complete real SQL writes, including the audit, before interruption.
            result = original(*args, **kwargs)
        if calls == 2:
            raise interruption
        if stage == "promotion":
            result = original(*args, **kwargs)
        return result

    with monkeypatch.context() as patch:
        if stage == "promotion":
            patch.setattr(release.os, "link", interrupt_second)
        else:
            patch.setattr(all_pass_batch.room, "_transition", interrupt_second)
        with pytest.raises(interruption_type) as caught:
            workflow.apply_image_plan(all_pass_batch.batch_id, plan_path)

    assert caught.value is interruption
    assert snapshot(all_pass_batch) == before
    assert_no_image_files(all_pass_batch)
    result = workflow.apply_image_plan(all_pass_batch.batch_id, plan_path)
    assert result["registered"] == 2
    assert all(all_pass_batch.room.get_article(article_id)["status"] == "IMAGE_READY" for article_id in all_pass_batch.article_ids)
    assert all_pass_batch.room.db.one("SELECT COUNT(*) AS count FROM images")["count"] == 2
    assert not list((tmp_path / "workspace").rglob(".staging-images-*"))


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


@pytest.fixture
def ready_batch(all_pass_batch, tmp_path):
    workflow = BatchReleaseWorkflow(all_pass_batch.room)
    workflow.apply_image_plan(all_pass_batch.batch_id, write_plan(tmp_path / "plan.json", image_plan(all_pass_batch, tmp_path)))
    return all_pass_batch


@pytest.fixture
def approved_batch(ready_batch):
    # Use the existing single-article gates so new package/publication tests
    # fail on the feature under test rather than on fixture construction.
    for article_id in ready_batch.article_ids:
        ready_batch.room.complete_review(article_id, "editor", "checked")
        ready_batch.room.approve(article_id, "editor", "approved")
    return ready_batch


@pytest.mark.parametrize("gate,required", [("review", "IMAGE_READY"), ("approve", "REVIEWED")])
def test_batch_gate_validates_all_articles_before_transition(ready_batch, gate, required):
    if gate == "approve":
        ready_batch.room.complete_review(ready_batch.article_ids[0], "editor", "checked")
    else:
        with ready_batch.room.db.connect() as connection:
            connection.execute("UPDATE articles SET status='FACT_CHECKED' WHERE article_id=?", (ready_batch.article_ids[1],))
    before = snapshot(ready_batch)
    with pytest.raises(WorkflowError, match=required):
        getattr(BatchReleaseWorkflow(ready_batch.room), gate)(ready_batch.batch_id, "editor", "whole batch")
    assert snapshot(ready_batch) == before


@pytest.mark.parametrize("inactive", ["EXCLUDED", "REUSED_COMPLETE"])
def test_review_and_approval_ignore_inactive_and_audit_all_active(ready_batch, tmp_path, inactive):
    ignored = ready_batch.article_ids[1]
    with ready_batch.room.db.connect() as connection:
        connection.execute("UPDATE batch_items SET item_status=? WHERE article_id=?", (inactive, ignored))
        connection.execute("UPDATE articles SET status='NEEDS_REVISION' WHERE article_id=?", (ignored,))
    ignored_before = ready_batch.room.get_article(ignored)
    ignored_audit = ready_batch.room.audit(ignored)
    workflow = BatchReleaseWorkflow(ready_batch.room)
    assert workflow.review(ready_batch.batch_id, "batch-editor", "checked all material")["reviewed"] == 1
    assert workflow.approve(ready_batch.batch_id, "batch-editor", "approved batch")["approved"] == 1
    active = ready_batch.article_ids[0]
    assert ready_batch.room.get_article(active)["status"] == "APPROVED"
    assert [(audit["actor"], audit["reason"], audit["to_status"]) for audit in ready_batch.room.audit(active)[-2:]] == [
        ("batch-editor", "checked all material", "REVIEWED"),
        ("batch-editor", "approved batch", "APPROVED"),
    ]
    assert ready_batch.room.get_article(ignored) == ignored_before
    assert ready_batch.room.audit(ignored) == ignored_audit
    state = json.loads((tmp_path / "workspace/batches" / ready_batch.batch_id / "batch-state.json").read_text())
    assert state["next_action"] == "export"


@pytest.mark.parametrize("gate", ["review", "approve"])
def test_batch_gate_database_failure_rolls_back_every_article(ready_batch, gate):
    if gate == "approve":
        for article_id in ready_batch.article_ids:
            ready_batch.room.complete_review(article_id, "editor", "checked")
    with ready_batch.room.db.connect() as connection:
        connection.execute("CREATE TRIGGER reject_release BEFORE UPDATE ON articles "
                           "WHEN NEW.article_id='" + ready_batch.article_ids[1] + "' "
                           "BEGIN SELECT RAISE(ABORT,'release rejected'); END")
    before = snapshot(ready_batch)
    with pytest.raises(sqlite3.IntegrityError, match="release rejected"):
        getattr(BatchReleaseWorkflow(ready_batch.room), gate)(ready_batch.batch_id, "editor", "checked batch")
    assert snapshot(ready_batch) == before


def test_review_and_approval_transition_every_active_article(ready_batch):
    workflow = BatchReleaseWorkflow(ready_batch.room)
    assert workflow.review(ready_batch.batch_id, "editor", "checked all material")["reviewed"] == 2
    assert workflow.approve(ready_batch.batch_id, "editor", "approved batch")["approved"] == 2
    assert all(ready_batch.room.get_article(article_id)["status"] == "APPROVED" for article_id in ready_batch.article_ids)


def publish_root(batch):
    return batch.room.root / "workspace/batches" / batch.batch_id / "publish"


def test_package_failure_preserves_previous_publish_directory(approved_batch):
    publish = publish_root(approved_batch)
    (publish / "sentinel.txt").write_text("old", encoding="utf-8")
    (approved_batch.room.root / approved_batch.room.get_article(approved_batch.article_ids[1])["draft_path"]).unlink()
    before = snapshot(approved_batch)
    with pytest.raises(WorkflowError, match="article file not found"):
        BatchReleaseWorkflow(approved_batch.room).build_packages(approved_batch.batch_id)
    assert (publish / "sentinel.txt").read_text() == "old"
    assert snapshot(approved_batch) == before
    assert not list(publish.parent.glob(".staging-publish-*"))


@pytest.mark.parametrize("inactive", ["EXCLUDED", "REUSED_COMPLETE"])
def test_packages_contain_current_material_for_exact_active_set(approved_batch, inactive):
    ignored = approved_batch.article_ids[1]
    with approved_batch.room.db.connect() as connection:
        connection.execute("UPDATE batch_items SET item_status=? WHERE article_id=?", (inactive, ignored))
        connection.execute("UPDATE articles SET status='NEEDS_REVISION' WHERE article_id=?", (ignored,))
    ignored_before = approved_batch.room.get_article(ignored)
    ignored_audit = approved_batch.room.audit(ignored)
    workflow = BatchReleaseWorkflow(approved_batch.room)
    target = workflow.build_packages(approved_batch.batch_id)
    assert target == publish_root(approved_batch)
    assert {path.name for path in target.iterdir()} == {approved_batch.article_ids[0], "batch-manifest.json"}
    manifest = json.loads((target / "batch-manifest.json").read_text())
    assert manifest["batch_id"] == approved_batch.batch_id
    assert [item["article_id"] for item in manifest["items"]] == [approved_batch.article_ids[0]]
    article = approved_batch.room.get_article(approved_batch.article_ids[0])
    package = target / article["article_id"]
    assert {"article.md", "research-brief.md", "fact-check.md", "hero.jpg", "metadata.json", "image-license.json", "sources.md", "publication-checklist.md"} <= {path.name for path in package.iterdir()}
    for output, field in [("article.md", "draft_path"), ("research-brief.md", "research_brief_path"), ("fact-check.md", "fact_check_path")]:
        assert (package / output).read_bytes() == (approved_batch.room.root / article[field]).read_bytes()
    assert json.loads((package / "metadata.json").read_text())["revision_number"] == article["revision_number"]
    assert json.loads((package / "image-license.json").read_text())["image_id"] == article["image_id"]
    assert "[x] approved for publication" in (package / "publication-checklist.md").read_text()
    assert article["status"] == "APPROVED"
    assert approved_batch.room.db.one("SELECT status FROM batches WHERE batch_id=?", (approved_batch.batch_id,))["status"] == "PACKAGED"
    state = json.loads((target.parent / "batch-state.json").read_text())
    assert state["next_action"] == "complete"
    assert next(item for item in state["items"] if item["article_id"] == article["article_id"])["package_path"] == f"publish/{article['article_id']}"
    assert approved_batch.room.get_article(ignored) == ignored_before
    assert approved_batch.room.audit(ignored) == ignored_audit
    # Rebuilding replaces the full managed directory, without duplicate artifacts.
    workflow.build_packages(approved_batch.batch_id)
    assert {path.name for path in target.iterdir()} == {article["article_id"], "batch-manifest.json"}


@pytest.mark.parametrize("stage", ["copy", "promotion", "database"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_package_failure_or_interruption_restores_prior_directory(approved_batch, monkeypatch, stage, error_type):
    import newsroom.batch_release as release

    publish = publish_root(approved_batch)
    (publish / "sentinel.txt").write_text("old", encoding="utf-8")
    before = snapshot(approved_batch)
    error = error_type("package interrupted")
    if stage == "database" and error_type is OSError:
        with approved_batch.room.db.connect() as connection:
            connection.execute("CREATE TRIGGER reject_package BEFORE UPDATE ON batches "
                               "BEGIN SELECT RAISE(ABORT,'package interrupted'); END")
        expected = sqlite3.IntegrityError
    else:
        expected = error_type
        if stage == "copy":
            import newsroom.service as service
            original = service.shutil.copyfile
            count = 0
            def fail_second_copy(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 2:
                    raise error
                return original(*args, **kwargs)
            monkeypatch.setattr(service.shutil, "copyfile", fail_second_copy)
        elif stage == "promotion":
            original = release.os.replace
            def fail_promotion(source, destination):
                if Path(destination) == publish and Path(source).name.startswith(".staging-publish-"):
                    raise error
                return original(source, destination)
            monkeypatch.setattr(release.os, "replace", fail_promotion)
        else:
            original = approved_batch.room.db.connect
            def fail_after_promotion():
                if (publish / "batch-manifest.json").is_file():
                    raise error
                return original()
            monkeypatch.setattr(approved_batch.room.db, "connect", fail_after_promotion)
    with pytest.raises(expected, match="package interrupted"):
        BatchReleaseWorkflow(approved_batch.room).build_packages(approved_batch.batch_id)
    assert {path.name for path in publish.iterdir()} == {"sentinel.txt"}
    assert (publish / "sentinel.txt").read_text() == "old"
    assert snapshot(approved_batch) == before
    assert not list(publish.parent.glob(".staging-publish-*"))
    assert not list(publish.parent.glob(".previous-publish-*"))


@pytest.mark.parametrize("escape", ["publish", "article"])
def test_packages_never_touch_external_symlink_targets(approved_batch, tmp_path, escape):
    external = tmp_path.parent / (tmp_path.name + "-external")
    external.mkdir()
    sentinel = external / "sentinel.md"
    sentinel.write_text("external", encoding="utf-8")
    if escape == "publish":
        publish_root(approved_batch).rmdir()
        publish_root(approved_batch).symlink_to(external, target_is_directory=True)
    else:
        article = approved_batch.room.get_article(approved_batch.article_ids[0])
        source = approved_batch.room.root / article["draft_path"]
        source.unlink()
        source.symlink_to(sentinel)
    before = snapshot(approved_batch)
    with pytest.raises(WorkflowError, match="within|symlink"):
        BatchReleaseWorkflow(approved_batch.room).build_packages(approved_batch.batch_id)
    assert sentinel.read_text() == "external"
    assert {path.name for path in external.iterdir()} == {"sentinel.md"}
    assert snapshot(approved_batch) == before


def publication_payload(batch):
    return {"schema_version": 1, "batch_id": batch.batch_id, "items": [
        {"article_id": article_id, "article_url": f"https://news.example/articles/{article_id}"}
        for article_id in batch.article_ids
    ]}


@pytest.mark.parametrize("mutation,message", [
    ("missing", "missing article"), ("extra", "unexpected article"),
    ("duplicate", "duplicate article"), ("wrong_batch", "batch_id"),
    ("invalid_url", "article_url"), ("unknown_field", "Extra inputs"),
    ("unknown_item_field", "Extra inputs"), ("wrong_state", "APPROVED"),
])
def test_publication_result_rejects_entire_invalid_mapping(approved_batch, tmp_path, mutation, message):
    payload = publication_payload(approved_batch)
    if mutation == "missing":
        payload["items"].pop()
    elif mutation == "extra":
        payload["items"].append({"article_id": "art-extra", "article_url": "https://news.example/extra"})
    elif mutation == "duplicate":
        payload["items"].append(payload["items"][0])
    elif mutation == "wrong_batch":
        payload["batch_id"] = "batch-other"
    elif mutation == "invalid_url":
        payload["items"][1]["article_url"] = "file:///tmp/article"
    elif mutation == "unknown_field":
        payload["approved"] = True
    elif mutation == "unknown_item_field":
        payload["items"][0]["approved"] = True
    else:
        with approved_batch.room.db.connect() as connection:
            connection.execute("UPDATE articles SET status='REVIEWED' WHERE article_id=?", (approved_batch.article_ids[1],))
    before = snapshot(approved_batch)
    with pytest.raises(WorkflowError, match=message):
        BatchReleaseWorkflow(approved_batch.room).register_publications(approved_batch.batch_id, write_plan(tmp_path / "publication.json", payload), "publisher")
    assert snapshot(approved_batch) == before


@pytest.mark.parametrize("inactive", ["EXCLUDED", "REUSED_COMPLETE", None])
def test_publication_records_all_active_urls_status_timestamps_and_audits(approved_batch, tmp_path, inactive):
    payload = publication_payload(approved_batch)
    ignored_before = None
    if inactive:
        ignored = approved_batch.article_ids[1]
        payload["items"].pop()
        with approved_batch.room.db.connect() as connection:
            connection.execute("UPDATE batch_items SET item_status=? WHERE article_id=?", (inactive, ignored))
        ignored_before = approved_batch.room.get_article(ignored)
    result = BatchReleaseWorkflow(approved_batch.room).register_publications(approved_batch.batch_id, write_plan(tmp_path / "publication.json", payload), "publisher")
    assert result["published"] == len(payload["items"])
    assert result["batch_id"] == approved_batch.batch_id
    assert approved_batch.room.db.one("SELECT status FROM batches WHERE batch_id=?", (approved_batch.batch_id,))["status"] == "PUBLISHED"
    for item in payload["items"]:
        article = approved_batch.room.get_article(item["article_id"])
        assert article["status"] == "PUBLISHED"
        assert article["article_url"] == item["article_url"]
        assert article["published_at"]
        assert article["updated_at"] == article["published_at"]
        assert article["publish_date"] == article["published_at"][:10]
        audit = approved_batch.room.audit(item["article_id"])[-1]
        assert (audit["actor"], audit["from_status"], audit["to_status"]) == ("publisher", "APPROVED", "PUBLISHED")
    if inactive:
        assert approved_batch.room.get_article(ignored) == ignored_before
    state = json.loads((publish_root(approved_batch).parent / "batch-state.json").read_text())
    assert all(item["approval_state"] == "PUBLISHED" for item in state["items"] if item["item_status"] not in {"EXCLUDED", "REUSED_COMPLETE"})


def test_publication_database_failure_rolls_back_urls_statuses_and_audits(approved_batch, tmp_path):
    with approved_batch.room.db.connect() as connection:
        connection.execute("CREATE TRIGGER reject_publication BEFORE UPDATE ON articles "
                           "WHEN NEW.article_id='" + approved_batch.article_ids[1] + "' "
                           "BEGIN SELECT RAISE(ABORT,'publication rejected'); END")
    before = snapshot(approved_batch)
    with pytest.raises(sqlite3.IntegrityError, match="publication rejected"):
        BatchReleaseWorkflow(approved_batch.room).register_publications(approved_batch.batch_id, write_plan(tmp_path / "publication.json", publication_payload(approved_batch)), "publisher")
    assert snapshot(approved_batch) == before


def test_active_cross_owner_packages_link_to_current_batch_and_refresh_owner(prepared_batch, tmp_path):
    batch = prepared_batch
    reused = batch.workflow.prepare(write_batch_csv(tmp_path / "reuse.csv", duplicate_rows()[::2], line_ending="\r\n"))
    imported = batch.workflow.import_content(batch.batch_id, write_content_result(batch, tmp_path / "content-result.json"))
    batch.fact_request = json.loads(Path(imported["fact_check_request_path"]).read_text())
    batch.workflow.import_fact_checks(batch.batch_id, write_fact_check_result(batch, tmp_path / "checks.json", ["PASS", "PASS"]))
    workflow = BatchReleaseWorkflow(batch.room)
    payload = image_plan(batch, tmp_path)
    payload["batch_id"] = reused["batch_id"]
    workflow.apply_image_plan(reused["batch_id"], write_plan(tmp_path / "images.json", payload))
    workflow.review(reused["batch_id"], "editor", "checked whole batch")
    workflow.approve(reused["batch_id"], "editor", "approved whole batch")
    workflow.build_packages(reused["batch_id"])
    current_root = tmp_path / "workspace/batches" / reused["batch_id"]
    state = json.loads((current_root / "batch-state.json").read_text())
    assert state["status"] == "PACKAGED"
    for item in state["items"]:
        assert item["package_path"] == f"publish/{item['article_id']}"
        assert (current_root / item["package_path"] / "article.md").is_file()
        assert item["owning_batch_id"] == batch.batch_id
    owner_state = json.loads((tmp_path / "workspace/batches" / batch.batch_id / "batch-state.json").read_text())
    assert all(item["approval_state"] == "APPROVED" for item in owner_state["items"])
    payload = publication_payload(batch)
    payload["batch_id"] = reused["batch_id"]
    workflow.register_publications(reused["batch_id"], write_plan(tmp_path / "publications.json", payload), "publisher")
    owner_state = json.loads((tmp_path / "workspace/batches" / batch.batch_id / "batch-state.json").read_text())
    assert all(item["approval_state"] == "PUBLISHED" for item in owner_state["items"])


def test_reused_complete_package_link_only_falls_back_to_owner(approved_batch, tmp_path):
    workflow = BatchReleaseWorkflow(approved_batch.room)
    workflow.build_packages(approved_batch.batch_id)
    reused = approved_batch.workflow.prepare(write_batch_csv(tmp_path / "reuse.csv", duplicate_rows()[::2], line_ending="\r\n"))
    current_id = reused["batch_id"]
    active, inactive = approved_batch.article_ids
    with approved_batch.room.db.connect() as connection:
        connection.execute("UPDATE batch_items SET item_status='FACT_CHECKED' WHERE batch_id=? AND article_id=?", (current_id, active))
    workflow.build_packages(current_id)
    current_root = tmp_path / "workspace/batches" / current_id
    state = json.loads((current_root / "batch-state.json").read_text())
    items = {item["article_id"]: item for item in state["items"]}
    assert items[active]["package_path"] == f"publish/{active}"
    assert items[inactive]["package_path"] == f"../{approved_batch.batch_id}/publish/{inactive}"
    assert not (current_root / "publish" / inactive).exists()
    # A stale local directory must never redirect a REUSED_COMPLETE link.
    (current_root / "publish" / inactive).mkdir()
    state = workflow.workspace.refresh(current_id)
    items = {item["article_id"]: item for item in state["items"]}
    assert items[inactive]["package_path"] == f"../{approved_batch.batch_id}/publish/{inactive}"


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_package_interruption_immediately_after_promotion_restores_prior_tree(approved_batch, monkeypatch, error_type):
    import newsroom.batch_release as release

    publish = publish_root(approved_batch)
    (publish / "sentinel.txt").write_text("old", encoding="utf-8")
    before = snapshot(approved_batch)
    original = release.os.replace
    def interrupt_after_promotion(source, destination):
        original(source, destination)
        if Path(destination) == publish and Path(source).name.startswith(".staging-publish-"):
            raise error_type("after promotion")
    monkeypatch.setattr(release.os, "replace", interrupt_after_promotion)
    with pytest.raises(error_type, match="after promotion"):
        BatchReleaseWorkflow(approved_batch.room).build_packages(approved_batch.batch_id)
    assert {path.name for path in publish.iterdir()} == {"sentinel.txt"}
    assert (publish / "sentinel.txt").read_text() == "old"
    assert snapshot(approved_batch) == before
    assert not list(publish.parent.glob(".previous-publish-*"))


def interrupt_after_package_commit(batch, monkeypatch, interruption):
    """Keep real SQLite commits, then inject one interruption at context exit."""
    from contextlib import contextmanager

    original = batch.room.db.connect
    injected = False
    @contextmanager
    def commit_then_interrupt():
        nonlocal injected
        with original() as connection:
            yield connection
            packaged_update = connection.in_transaction and connection.execute(
                "SELECT status FROM batches WHERE batch_id=?", (batch.batch_id,)
            ).fetchone()["status"] == "PACKAGED"
        if packaged_update and not injected:
            injected = True
            raise interruption
    monkeypatch.setattr(batch.room.db, "connect", commit_then_interrupt)


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_package_post_commit_interruption_restores_exact_metadata_and_allows_retry(approved_batch, monkeypatch, error_type):
    workflow = BatchReleaseWorkflow(approved_batch.room)
    publish = publish_root(approved_batch)
    (publish / "sentinel.txt").write_text("old", encoding="utf-8")
    with approved_batch.room.db.connect() as connection:
        connection.execute("UPDATE batches SET updated_at=? WHERE batch_id=?", ("2000-01-01T00:00:00+00:00", approved_batch.batch_id))
    before = snapshot(approved_batch)
    prior_batch = approved_batch.room.db.one("SELECT * FROM batches WHERE batch_id=?", (approved_batch.batch_id,))
    interruption = error_type("interrupted after SQLite commit")
    interrupt_after_package_commit(approved_batch, monkeypatch, interruption)
    with pytest.raises(error_type) as caught:
        workflow.build_packages(approved_batch.batch_id)
    assert caught.value is interruption
    assert {path.name for path in publish.iterdir()} == {"sentinel.txt"}
    assert (publish / "sentinel.txt").read_text() == "old"
    assert approved_batch.room.db.one("SELECT * FROM batches WHERE batch_id=?", (approved_batch.batch_id,)) == prior_batch
    assert snapshot(approved_batch) == before
    assert not list(publish.parent.glob(".staging-publish-*"))
    assert not list(publish.parent.glob(".previous-publish-*"))
    assert workflow.build_packages(approved_batch.batch_id) == publish
    assert approved_batch.room.db.one("SELECT status FROM batches WHERE batch_id=?", (approved_batch.batch_id,))["status"] == "PACKAGED"
    assert {path.name for path in publish.iterdir()} == {*approved_batch.article_ids, "batch-manifest.json"}
    after = snapshot(approved_batch)
    assert {table: rows for table, rows in after.items() if table != "batches"} == {table: rows for table, rows in before.items() if table != "batches"}
    assert not list(publish.parent.glob(".staging-publish-*"))
    assert not list(publish.parent.glob(".previous-publish-*"))


def test_package_refresh_failure_restores_prior_tree_and_batch_metadata(approved_batch, monkeypatch):
    workflow = BatchReleaseWorkflow(approved_batch.room)
    publish = publish_root(approved_batch)
    (publish / "sentinel.txt").write_text("old", encoding="utf-8")
    with approved_batch.room.db.connect() as connection:
        connection.execute("UPDATE batches SET updated_at=? WHERE batch_id=?", ("2000-01-01T00:00:00+00:00", approved_batch.batch_id))
    before = snapshot(approved_batch)
    failure = OSError("workspace refresh failed")
    def fail_refresh(*args):
        raise failure
    monkeypatch.setattr(workflow, "_refresh_workspaces", fail_refresh)
    with pytest.raises(OSError) as caught:
        workflow.build_packages(approved_batch.batch_id)
    assert caught.value is failure
    assert {path.name for path in publish.iterdir()} == {"sentinel.txt"}
    assert snapshot(approved_batch) == before
    assert not list(publish.parent.glob(".staging-publish-*"))
    assert not list(publish.parent.glob(".previous-publish-*"))


@pytest.mark.parametrize("compensation", ["database", "filesystem"])
def test_package_reports_original_interruption_and_compensation_failure(approved_batch, monkeypatch, compensation):
    import newsroom.batch_release as release

    workflow = BatchReleaseWorkflow(approved_batch.room)
    publish = publish_root(approved_batch)
    (publish / "sentinel.txt").write_text("old", encoding="utf-8")
    prior_batch = approved_batch.room.db.one("SELECT * FROM batches WHERE batch_id=?", (approved_batch.batch_id,))
    interruption = KeyboardInterrupt("interrupted after SQLite commit")
    if compensation == "database":
        with approved_batch.room.db.connect() as connection:
            connection.execute("CREATE TRIGGER reject_compensation BEFORE UPDATE ON batches "
                               "WHEN NEW.status != 'PACKAGED' BEGIN SELECT RAISE(ABORT,'metadata restoration rejected'); END")
    else:
        original_replace = release.os.replace
        def fail_restoration(source, destination):
            if Path(source).parent.name.startswith(".previous-publish-"):
                raise OSError("directory restoration rejected")
            return original_replace(source, destination)
        monkeypatch.setattr(release.os, "replace", fail_restoration)
    interrupt_after_package_commit(approved_batch, monkeypatch, interruption)
    with pytest.raises(BaseException) as caught:
        workflow.build_packages(approved_batch.batch_id)
    assert isinstance(caught.value, BaseExceptionGroup)
    assert caught.value.exceptions[0] is interruption
    assert len(caught.value.exceptions) == 2
    if compensation == "database":
        assert isinstance(caught.value.exceptions[1], sqlite3.IntegrityError)
        assert "metadata restoration rejected" in str(caught.value.exceptions[1])
        assert {path.name for path in publish.iterdir()} == {"sentinel.txt"}
        assert approved_batch.room.db.one("SELECT status FROM batches WHERE batch_id=?", (approved_batch.batch_id,))["status"] == "PACKAGED"
    else:
        assert isinstance(caught.value.exceptions[1], OSError)
        assert "directory restoration rejected" in str(caught.value.exceptions[1])
        backups = list(publish.parent.glob(".previous-publish-*/publish/sentinel.txt"))
        assert len(backups) == 1
        assert backups[0].read_text() == "old"
        assert approved_batch.room.db.one("SELECT * FROM batches WHERE batch_id=?", (approved_batch.batch_id,)) == prior_batch
    assert not list(publish.parent.glob(".staging-publish-*"))


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_package_post_commit_backup_cleanup_failure_returns_committed_success(approved_batch, monkeypatch, error_type):
    import newsroom.batch_release as release

    workflow = BatchReleaseWorkflow(approved_batch.room)
    publish = publish_root(approved_batch)
    (publish / "discard.txt").write_text("old disposable file", encoding="utf-8")
    (publish / "recovery.txt").write_text("retained recovery data", encoding="utf-8")
    before = snapshot(approved_batch)
    original = release.shutil.rmtree
    def fail_partial_backup_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(".previous-publish-"):
            # Model a cleanup that has already removed one old file before
            # failing. The remaining tree is recovery data, never canonical.
            (Path(path) / "publish/discard.txt").unlink()
            raise error_type("backup cleanup interrupted")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(release.shutil, "rmtree", fail_partial_backup_cleanup)
        try:
            result = workflow.build_packages(approved_batch.batch_id)
        except BaseException as error:
            pytest.fail(f"committed packages must return success after backup cleanup fails: {error!r}")
        assert result == publish
    assert {path.name for path in publish.iterdir()} == {*approved_batch.article_ids, "batch-manifest.json"}
    manifest = json.loads((publish / "batch-manifest.json").read_text())
    assert manifest["batch_id"] == approved_batch.batch_id
    assert {item["article_id"] for item in manifest["items"]} == set(approved_batch.article_ids)
    for article_id in approved_batch.article_ids:
        article = approved_batch.room.get_article(article_id)
        package = publish / article_id
        assert (package / "article.md").read_bytes() == (approved_batch.room.root / article["draft_path"]).read_bytes()
        assert (package / "hero.jpg").is_file()
        assert json.loads((package / "metadata.json").read_text())["status"] == "APPROVED"
        assert "[x] approved for publication" in (package / "publication-checklist.md").read_text()
    after = snapshot(approved_batch)
    assert after["batches"][0]["status"] == "PACKAGED"
    assert {table: rows for table, rows in after.items() if table != "batches"} == {table: rows for table, rows in before.items() if table != "batches"}
    state = json.loads((publish.parent / "batch-state.json").read_text())
    assert state["status"] == "PACKAGED"
    assert state["next_action"] == "complete"
    backups = list(publish.parent.glob(".previous-publish-*"))
    assert len(backups) == 1
    retained = backups[0] / "publish/recovery.txt"
    assert retained.read_text() == "retained recovery data"
    assert not (backups[0] / "publish/discard.txt").exists()
    assert not list(publish.parent.glob(".staging-publish-*"))
    # A subsequent build uses a fresh unique backup and cleans up only that
    # backup; the incomplete recovery data from the first build is untouched.
    assert workflow.build_packages(approved_batch.batch_id) == publish
    assert list(publish.parent.glob(".previous-publish-*")) == backups
    assert retained.read_text() == "retained recovery data"
    assert {path.name for path in publish.iterdir()} == {*approved_batch.article_ids, "batch-manifest.json"}
    assert not list(publish.parent.glob(".staging-publish-*"))
