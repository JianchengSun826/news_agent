from __future__ import annotations

import csv
import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.main import get_command

from typer.testing import CliRunner

from newsroom.cli import app
from newsroom.service import Newsroom

from test_batch import (
    newsroom, prepared_batch, write_content_result, write_fact_check_result,
)


runner = CliRunner()


def write_candidate_csv(path: Path) -> Path:
    fields = [
        "candidate_title",
        "core_fact",
        "event_date",
        "company",
        "product",
        "topic",
        "subtopic",
        "event_type",
        "keywords",
        "primary_source",
        "secondary_sources",
        "discovered_from",
        "priority",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "candidate_title": "Example launches Tool",
                "core_fact": "Example launched Tool on 2026-10-05.",
                "event_date": "2026-10-05",
                "company": "Example",
                "product": "Tool",
                "topic": "AI",
                "subtopic": "Developer Tools",
                "event_type": "launch",
                "keywords": "example|tool",
                "primary_source": "https://example.com/tool",
                "secondary_sources": "",
                "discovered_from": "test",
                "priority": "4",
            }
        )
    return path


def test_cli_exposes_only_batch_post_selection_workflow():
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    commands = {name for name, command in get_command(app).commands.items() if not command.hidden}
    assert commands == {"init", "candidate", "duplicate-check", "batch", "published", "stats"}
    for command in (
        "image-plan", "review", "approve", "publish-packages",
        "publish-register", "refresh-workspace",
    ):
        result = runner.invoke(app, ["batch", command, "--help"])
        assert result.exit_code == 0, result.output


@pytest.mark.parametrize("command", [
    "brief", "prompt", "research", "article", "fact-check", "image",
    "review", "approve", "publish-package", "publish",
])
def test_removed_commands_are_rejected_without_creating_files(tmp_path: Path, command: str):
    result = runner.invoke(app, ["--root", str(tmp_path), command])
    assert result.exit_code == 2
    assert "No such command" in result.output
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def release_batch(prepared_batch: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:
    batch = prepared_batch
    content = write_content_result(batch, tmp_path / "content-result.json")
    result = invoke_batch(batch, "content-import", str(content))
    assert result.exit_code == 0, result.output
    batch.fact_request = json.loads(Path(json.loads(result.output)["fact_check_request_path"]).read_text())
    checks = write_fact_check_result(batch, tmp_path / "checks.json", ["PASS", "PASS"])
    result = invoke_batch(batch, "fact-check-import", str(checks))
    assert result.exit_code == 0, result.output
    image_items = []
    for index, article_id in enumerate(batch.article_ids):
        image_file = tmp_path / f"download-{index}.JPG"
        image_file.write_bytes(f"local image {index}".encode())
        image_items.append({
            "article_id": article_id,
            "image_candidate_id": batch.room.list_image_candidates(article_id)[0]["image_candidate_id"],
            "image_file": str(image_file),
            "rights_confirmed": True,
        })
    batch.image_plan = tmp_path / "images.json"
    batch.image_plan.write_text(json.dumps({"schema_version": 1, "batch_id": batch.batch_id, "items": image_items}))
    batch.publication_result = tmp_path / "publications.json"
    batch.publication_result.write_text(json.dumps({
        "schema_version": 1, "batch_id": batch.batch_id,
        "items": [{"article_id": article_id, "article_url": f"https://news.example/{index}"}
                  for index, article_id in enumerate(batch.article_ids)],
    }))
    return batch


def invoke_batch(batch: SimpleNamespace, command: str, *args: str):
    return runner.invoke(app, ["--root", str(batch.room.root), "batch", command, batch.batch_id, *args])


def release_steps(batch: SimpleNamespace) -> list[tuple[str, list[str]]]:
    return [
        ("image-plan", [str(batch.image_plan)]),
        ("review", ["--actor", "reviewer", "--reason", "checked whole batch"]),
        ("approve", ["--actor", "approver", "--reason", "approved whole batch"]),
        ("publish-packages", []),
        ("publish-register", [str(batch.publication_result), "--actor", "publisher"]),
    ]


def workspace_snapshot(batch: SimpleNamespace) -> tuple[list[str], dict[str, bytes]]:
    with batch.room.db.connect() as connection:
        database = list(connection.iterdump())
    files = {str(path.relative_to(batch.room.root)): path.read_bytes()
             for path in batch.room.root.rglob("*") if path.is_file() and path != batch.room.db_path}
    return database, files


def test_cli_batch_release_sequence_and_workspace_refresh(release_batch: SimpleNamespace):
    batch = release_batch
    root = batch.room.root / "workspace/batches" / batch.batch_id
    for (command, args), expected_state in zip(release_steps(batch), [
        "IMAGE_READY", "REVIEWED", "APPROVED", "APPROVED", "PUBLISHED",
    ], strict=True):
        result = invoke_batch(batch, command, *args)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["batch_id"] == batch.batch_id
        assert all(batch.room.get_article(article_id)["status"] == expected_state for article_id in batch.article_ids)
        if command == "image-plan":
            assert payload["registered"] == 2
            for item in payload["items"]:
                image = batch.room.db.one("SELECT * FROM images WHERE image_id=?", (item["image_id"],))
                assert image["rights_basis"] == "OFFICIAL_PRESS_LICENSE"
                assert (batch.room.root / image["image_path"]).read_bytes().startswith(b"local image")
        elif command in {"review", "approve"}:
            key = "reviewed" if command == "review" else "approved"
            assert payload[key] == 2
            assert all(batch.room.audit(article_id)[-1]["actor"] == args[1] for article_id in batch.article_ids)
        elif command == "publish-packages":
            assert Path(payload["path"]) == root / "publish"
            manifest = json.loads((root / "publish/batch-manifest.json").read_text())
            assert {item["article_id"] for item in manifest["items"]} == set(batch.article_ids)
            for article_id in batch.article_ids:
                article = batch.room.get_article(article_id)
                assert (root / "publish" / article_id / "article.md").read_bytes() == (batch.room.root / article["draft_path"]).read_bytes()
                assert (root / "publish" / article_id / "hero.jpg").is_file()
        else:
            assert payload["published"] == 2
            for index, article_id in enumerate(batch.article_ids):
                article = batch.room.get_article(article_id)
                assert article["article_url"] == f"https://news.example/{index}"
                assert article["published_at"]

    before = workspace_snapshot(batch)
    (root / "batch-state.json").unlink()
    result = invoke_batch(batch, "refresh-workspace")
    assert result.exit_code == 0, result.output
    state = json.loads(result.output)
    assert state["batch_id"] == batch.batch_id
    assert state["status"] == "PUBLISHED"
    assert all(item["approval_state"] == "PUBLISHED" for item in state["items"])
    assert json.loads((root / "batch-state.json").read_text()) == state
    assert workspace_snapshot(batch) == before


def test_cli_publish_packages_oserror_restores_packages_and_database(release_batch, monkeypatch):
    import newsroom.batch_release as release

    batch = release_batch
    for command, args in release_steps(batch)[:4]:
        result = invoke_batch(batch, command, *args)
        assert result.exit_code == 0, result.output
    publish = batch.room.root / "workspace/batches" / batch.batch_id / "publish"
    before = workspace_snapshot(batch)
    original_replace = release.os.replace

    def fail_package_promotion(source, destination):
        if Path(destination) == publish and Path(source).name.startswith(".staging-publish-"):
            raise PermissionError(errno.EACCES, "Permission denied", str(publish))
        return original_replace(source, destination)

    monkeypatch.setattr(release.os, "replace", fail_package_promotion)

    result = invoke_batch(batch, "publish-packages")

    assert result.exit_code == 2, result.output
    assert "error:" in result.output
    assert "Permission denied" in result.output
    assert str(publish) in result.output
    assert workspace_snapshot(batch) == before
    assert not list(publish.parent.glob(".staging-publish-*"))
    assert not list(publish.parent.glob(".previous-publish-*"))


def test_cli_refresh_workspace_oserror_preserves_database_and_files(release_batch, monkeypatch):
    import newsroom.workspace as workspace

    batch = release_batch
    state_path = batch.room.root / "workspace/batches" / batch.batch_id / "batch-state.json"
    before = workspace_snapshot(batch)
    original_replace = workspace.os.replace

    def fail_state_write(source, destination):
        if Path(destination) == state_path:
            raise OSError(errno.ENOSPC, "No space left on device", str(state_path))
        return original_replace(source, destination)

    monkeypatch.setattr(workspace.os, "replace", fail_state_write)

    result = invoke_batch(batch, "refresh-workspace")

    assert result.exit_code == 2, result.output
    assert "error:" in result.output
    assert "No space left on device" in result.output
    assert str(state_path) in result.output
    assert workspace_snapshot(batch) == before


@pytest.mark.parametrize("command,argument_key,message", [
    ("image-plan", "image_plan", "rights_confirmed"),
    ("review", None, "IMAGE_READY"),
    ("approve", None, "REVIEWED"),
    ("publish-packages", None, "APPROVED"),
    ("publish-register", "publication_result", "APPROVED"),
])
def test_cli_batch_release_errors_preserve_database_and_files(release_batch, command, argument_key, message):
    batch = release_batch
    if command == "image-plan":
        payload = json.loads(batch.image_plan.read_text())
        payload["items"][1]["rights_confirmed"] = False
        batch.image_plan.write_text(json.dumps(payload))
    args = [str(getattr(batch, argument_key))] if argument_key else []
    if command in {"review", "approve"}:
        args += ["--actor", "editor", "--reason", "checked batch"]
    elif command == "publish-register":
        args += ["--actor", "publisher"]
    before = workspace_snapshot(batch)
    result = invoke_batch(batch, command, *args)
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert workspace_snapshot(batch) == before


@pytest.mark.parametrize("command", ["image-plan", "review", "approve", "publish-packages", "publish-register", "refresh-workspace"])
def test_cli_batch_release_missing_batch_preserves_database_and_files(release_batch, command):
    batch = release_batch
    args = []
    if command in {"image-plan", "publish-register"}:
        args.append(str(batch.image_plan if command == "image-plan" else batch.publication_result))
    if command in {"review", "approve"}:
        args += ["--actor", "editor", "--reason", "checked batch"]
    if command == "publish-register":
        args += ["--actor", "publisher"]
    before = workspace_snapshot(batch)
    result = runner.invoke(app, ["--root", str(batch.room.root), "batch", command, "batch-missing", *args])
    assert result.exit_code == 2, result.output
    assert "batch not found: batch-missing" in result.output
    assert workspace_snapshot(batch) == before


@pytest.mark.parametrize("failure", ["actor", "reason", "publication_mapping"])
def test_cli_batch_release_rejects_invalid_confirmation_without_mutation(release_batch, failure):
    batch = release_batch
    steps = release_steps(batch)
    limit = 1 if failure != "publication_mapping" else 4
    for command, args in steps[:limit]:
        result = invoke_batch(batch, command, *args)
        assert result.exit_code == 0, result.output
    if failure == "publication_mapping":
        payload = json.loads(batch.publication_result.read_text())
        payload["items"].pop()
        batch.publication_result.write_text(json.dumps(payload))
        command, args = steps[-1]
        message = "missing article IDs"
    else:
        command = "review"
        args = ["--actor", "" if failure == "actor" else "editor", "--reason", "" if failure == "reason" else "checked batch"]
        message = f"transition {failure} is required"
    before = workspace_snapshot(batch)
    result = invoke_batch(batch, command, *args)
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert workspace_snapshot(batch) == before


def test_typer_exposes_batch_commands():
    for command in (
        "prepare",
        "status",
        "resolve",
        "content-import",
        "fact-check-import",
        "revision-request",
        "revision-import",
    ):
        result = runner.invoke(app, ["batch", command, "--help"])
        assert result.exit_code == 0, result.output


def test_cli_prepare_outputs_one_claude_request(tmp_path: Path):
    csv_path = write_candidate_csv(tmp_path / "candidates.csv")
    assert runner.invoke(app, ["--root", str(tmp_path), "init"]).exit_code == 0

    result = runner.invoke(
        app, ["--root", str(tmp_path), "batch", "prepare", str(csv_path)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    batch_root = tmp_path / "workspace/batches" / payload["batch_id"]
    assert Path(payload["content_request_path"]) == batch_root / "claude/requests/content-request.json"
    assert (batch_root / "input/candidates.csv").read_bytes() == csv_path.read_bytes()
    assert payload["claude_instruction"] == (
        "严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。"
    )


def test_cli_batch_status_missing_batch_is_actionable(tmp_path: Path):
    assert runner.invoke(app, ["--root", str(tmp_path), "init"]).exit_code == 0

    result = runner.invoke(
        app, ["--root", str(tmp_path), "batch", "status", "batch-missing"]
    )

    assert result.exit_code == 2
    assert "batch not found: batch-missing" in result.output


def test_cli_content_import_reports_validation_field(tmp_path: Path):
    csv_path = write_candidate_csv(tmp_path / "candidates.csv")
    runner.invoke(app, ["--root", str(tmp_path), "init"])
    prepared = runner.invoke(
        app, ["--root", str(tmp_path), "batch", "prepare", str(csv_path)]
    )
    batch_id = json.loads(prepared.output)["batch_id"]
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--root", str(tmp_path), "batch", "content-import", batch_id, str(invalid)],
    )

    assert result.exit_code == 2
    assert "batch_id\n  Field required" in result.output
