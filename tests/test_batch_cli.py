from __future__ import annotations

import csv
import json
from pathlib import Path

from typer.testing import CliRunner

from newsroom.cli import app
from newsroom.service import Newsroom


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


def batch_image_workspace(tmp_path: Path) -> tuple[Path, str, Path]:
    room = Newsroom(tmp_path)
    room.init()
    candidate = room.add_candidate(
        candidate_title="Example launches Tool",
        core_fact="Example launched Tool on 2026-10-05.",
        event_date="2026-10-05",
        company="Example",
        product="Tool",
        topic="AI",
        subtopic="Developer Tools",
        event_type="launch",
        keywords=["example", "tool"],
        primary_source="https://example.com/tool",
        secondary_sources=[],
        discovered_from="test",
        priority=4,
    )
    room.check_duplicate(candidate["candidate_id"])
    room.select_candidate(candidate["candidate_id"], "editor", "test selection")
    article = room.create_brief(candidate["candidate_id"])
    now = "2026-10-05T00:00:00+00:00"
    with room.db.connect() as connection:
        connection.execute(
            "UPDATE articles SET status='FACT_CHECKED' WHERE article_id=?",
            (article["article_id"],),
        )
        connection.execute(
            "INSERT INTO batches(batch_id,source_filename,source_sha256,status,created_at,updated_at) "
            "VALUES ('batch-cli','test.csv','batch-cli','READY_FOR_IMAGE',?,?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO image_candidates(image_candidate_id,batch_id,article_id,landing_page_url,"
            "creator,rights_basis,license_name,rights_url,rights_statement,attribution_required,"
            "attribution_text,relevance,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "imgcand-cli",
                "batch-cli",
                article["article_id"],
                "https://images.example.org/tool",
                "Example Team",
                "OPEN_LICENSE",
                "CC BY 4.0",
                "https://creativecommons.org/licenses/by/4.0/",
                "Original page declares CC BY 4.0.",
                1,
                "Example Team / CC BY 4.0",
                "Product image",
                now,
                now,
            ),
        )
    image_file = tmp_path / "downloaded.jpg"
    image_file.write_bytes(b"jpeg")
    return tmp_path, article["article_id"], image_file


def test_cli_lists_and_registers_image_candidate(tmp_path: Path):
    root, article_id, image_file = batch_image_workspace(tmp_path)

    listed = runner.invoke(
        app, ["--root", str(root), "image", "candidates", article_id]
    )
    assert listed.exit_code == 0, listed.output
    candidate_id = json.loads(listed.output)[0]["image_candidate_id"]
    registered = runner.invoke(
        app,
        [
            "--root",
            str(root),
            "image",
            "register",
            article_id,
            "--candidate",
            candidate_id,
            "--file",
            str(image_file),
            "--rights-confirmed",
        ],
    )

    assert registered.exit_code == 0, registered.output
    image_id = json.loads(registered.output)["image_id"]
    stored = Newsroom(root).db.one("SELECT * FROM images WHERE image_id=?", (image_id,))
    assert stored["image_candidate_id"] == candidate_id
    assert stored["rights_basis"] == "OPEN_LICENSE"


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
    assert Path(payload["content_request_path"]).exists()
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
