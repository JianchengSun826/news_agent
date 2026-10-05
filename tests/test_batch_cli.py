from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from newsroom.cli import app
from newsroom.service import Newsroom


runner = CliRunner()


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
