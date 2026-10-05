from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from newsroom.batch import BatchWorkflow
from newsroom.service import Newsroom, WorkflowError


CSV_FIELDS = [
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


@pytest.fixture
def newsroom(tmp_path: Path) -> Newsroom:
    room = Newsroom(tmp_path)
    room.init()
    return room


def candidate_row(**overrides) -> dict[str, str]:
    row = {
        "candidate_title": "Example launches Tool",
        "core_fact": "Example launched Tool on 2026-10-05.",
        "event_date": "2026-10-05",
        "company": "Example",
        "product": "Tool",
        "topic": "AI",
        "subtopic": "Developer Tools",
        "event_type": "launch",
        "keywords": "example|tool",
        "primary_source": "https://example.com/official/tool",
        "secondary_sources": "",
        "discovered_from": "gemini-spark",
        "priority": "4",
    }
    row.update(overrides)
    return row


def write_batch_csv(path: Path, rows: list[dict[str, str]], *, line_ending: str = "\n") -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator=line_ending)
        writer.writeheader()
        writer.writerows(rows)
    return path


def duplicate_rows() -> list[dict[str, str]]:
    return [
        candidate_row(),
        candidate_row(
            candidate_title="Tool now available",
            primary_source="https://news.example.org/tool-launch",
        ),
        candidate_row(
            candidate_title="Other releases Widget",
            core_fact="Other released Widget on 2026-10-05.",
            company="Other",
            product="Widget",
            keywords="other|widget",
            primary_source="https://other.example/widget",
        ),
    ]


def test_prepare_auto_selects_new_and_pauses_non_new(newsroom: Newsroom, tmp_path: Path):
    csv_path = write_batch_csv(tmp_path / "batch.csv", duplicate_rows())

    result = BatchWorkflow(newsroom).prepare(csv_path)
    status = BatchWorkflow(newsroom).status(result["batch_id"])

    assert status["counts"]["READY_FOR_CONTENT"] == 2
    assert status["counts"]["WAITING_DUPLICATE"] == 1
    request = json.loads(Path(result["content_request_path"]).read_text(encoding="utf-8"))
    assert len(request["items"]) == 2
    assert "instructions" in request


def test_prepare_reuses_old_candidate_and_completed_article(newsroom: Newsroom, tmp_path: Path):
    values = candidate_row()
    candidate = newsroom.add_candidate(
        candidate_title=values["candidate_title"],
        core_fact=values["core_fact"],
        event_date=values["event_date"],
        company=values["company"],
        product=values["product"],
        topic=values["topic"],
        subtopic=values["subtopic"],
        event_type=values["event_type"],
        keywords=values["keywords"].split("|"),
        primary_source=values["primary_source"],
        secondary_sources=[],
        discovered_from=values["discovered_from"],
        priority=int(values["priority"]),
    )
    newsroom.check_duplicate(candidate["candidate_id"])
    newsroom.select_candidate(candidate["candidate_id"], "editor", "selected before batch support")
    article = newsroom.create_brief(candidate["candidate_id"])
    with newsroom.db.connect() as connection:
        connection.execute("UPDATE articles SET status='APPROVED' WHERE article_id=?", (article["article_id"],))
    csv_path = write_batch_csv(tmp_path / "existing.csv", [values])

    result = BatchWorkflow(newsroom).prepare(csv_path)
    item = BatchWorkflow(newsroom).status(result["batch_id"])["items"][0]

    assert item["candidate_id"] == candidate["candidate_id"]
    assert item["article_id"] == article["article_id"]
    assert item["item_status"] == "REUSED_COMPLETE"
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 1


def test_prepare_same_file_is_idempotent(newsroom: Newsroom, tmp_path: Path):
    csv_path = write_batch_csv(tmp_path / "same.csv", [candidate_row()])

    first = BatchWorkflow(newsroom).prepare(csv_path)
    second = BatchWorkflow(newsroom).prepare(csv_path)

    assert first["batch_id"] == second["batch_id"]
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM batches")["count"] == 1


def test_byte_different_csv_reuses_candidates_but_creates_new_batch(newsroom: Newsroom, tmp_path: Path):
    first_path = write_batch_csv(tmp_path / "first.csv", [candidate_row()], line_ending="\n")
    second_path = write_batch_csv(tmp_path / "second.csv", [candidate_row()], line_ending="\r\n")

    one = BatchWorkflow(newsroom).prepare(first_path)
    two = BatchWorkflow(newsroom).prepare(second_path)

    assert one["batch_id"] != two["batch_id"]
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 1


def test_prepare_validates_entire_csv_before_mutation(newsroom: Newsroom, tmp_path: Path):
    invalid = candidate_row(priority="99")
    csv_path = write_batch_csv(tmp_path / "invalid.csv", [candidate_row(), invalid])

    with pytest.raises(ValueError, match="priority"):
        BatchWorkflow(newsroom).prepare(csv_path)

    assert newsroom.db.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 0
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM batches")["count"] == 0


def test_resolve_requires_override_for_possible_duplicate(newsroom: Newsroom, tmp_path: Path):
    rows = [
        candidate_row(),
        candidate_row(
            candidate_title="Tool gains a separate announcement",
            core_fact="Example described a separate Tool announcement for customers.",
            event_date="2026-10-06",
            primary_source="https://example.com/official/tool-followup",
        ),
    ]
    result = BatchWorkflow(newsroom).prepare(write_batch_csv(tmp_path / "possible.csv", rows))
    waiting = [item for item in BatchWorkflow(newsroom).status(result["batch_id"])["items"] if item["item_status"] == "WAITING_DUPLICATE"]
    assert waiting[0]["duplicate_classification"] == "POSSIBLE_DUPLICATE"

    with pytest.raises(WorkflowError, match="duplicate override"):
        BatchWorkflow(newsroom).resolve(
            result["batch_id"], waiting[0]["candidate_id"], "include", "editor", "include it"
        )
