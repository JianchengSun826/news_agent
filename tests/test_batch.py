from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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
    assert "research_brief_md" in request["instructions"]
    assert "rights_url" in request["instructions"]


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


def test_prepare_ignores_whitespace_only_csv_rows(newsroom: Newsroom, tmp_path: Path):
    csv_path = write_batch_csv(
        tmp_path / "blank-row.csv",
        [candidate_row(), {field: " " for field in CSV_FIELDS}],
    )

    result = BatchWorkflow(newsroom).prepare(csv_path)

    assert result["counts"] == {"READY_FOR_CONTENT": 1}
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 1


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


def test_resolve_includes_possible_duplicate_after_explicit_override(
    newsroom: Newsroom, tmp_path: Path
):
    rows = [
        candidate_row(),
        candidate_row(
            candidate_title="Tool gains a separate announcement",
            core_fact="Example described a separate Tool announcement for customers.",
            event_date="2026-10-06",
            primary_source="https://example.com/official/tool-followup",
        ),
    ]
    workflow = BatchWorkflow(newsroom)
    result = workflow.prepare(write_batch_csv(tmp_path / "override.csv", rows))
    waiting = [
        item
        for item in workflow.status(result["batch_id"])["items"]
        if item["item_status"] == "WAITING_DUPLICATE"
    ][0]
    newsroom.override_duplicate(
        waiting["candidate_id"], "FOLLOW_UP", "confirmed separate follow-up", "editor"
    )

    resolved = workflow.resolve(
        result["batch_id"],
        waiting["candidate_id"],
        "include",
        "editor",
        "explicit override recorded",
    )

    item = next(
        item for item in resolved["items"] if item["candidate_id"] == waiting["candidate_id"]
    )
    assert item["item_status"] == "READY_FOR_CONTENT"
    assert item["article_id"]


@pytest.fixture
def prepared_batch(newsroom: Newsroom, tmp_path: Path) -> SimpleNamespace:
    rows = [
        candidate_row(),
        candidate_row(
            candidate_title="Other releases Widget",
            core_fact="Other released Widget on 2026-10-05.",
            company="Other",
            product="Widget",
            keywords="other|widget",
            primary_source="https://other.example/widget",
        ),
    ]
    workflow = BatchWorkflow(newsroom)
    prepared = workflow.prepare(write_batch_csv(tmp_path / "content.csv", rows))
    request_path = Path(prepared["content_request_path"])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    return SimpleNamespace(
        room=newsroom,
        workflow=workflow,
        batch_id=prepared["batch_id"],
        request=request,
        request_path=request_path,
        article_ids=[item["article_id"] for item in request["items"]],
    )


def write_content_result(
    prepared: SimpleNamespace,
    path: Path,
    *,
    mutation: str | None = None,
) -> Path:
    items = []
    for index, request_item in enumerate(prepared.request["items"], start=1):
        items.append(
            {
                "article_id": request_item["article_id"],
                "research_brief_md": f"# Research Brief {index}\n\nVerified source findings for this article.",
                "title_cn": f"示例新闻标题 {index}",
                "slug": f"example-news-{index}",
                "article_md": f"# 示例新闻标题 {index}\n\n这是基于已核实来源撰写的完整新闻文章内容。",
                "image_candidates": [
                    {
                        "landing_page_url": f"https://images.example.org/item-{index}",
                        "direct_image_url": f"https://images.example.org/item-{index}.jpg",
                        "creator": "Example Press Team",
                        "rights_basis": "OFFICIAL_PRESS_LICENSE",
                        "license_name": "Press use",
                        "rights_url": "https://images.example.org/press-terms",
                        "rights_statement": "The official press page permits editorial reuse.",
                        "attribution_required": False,
                        "attribution_text": None,
                        "relevance": "The image directly depicts the announced product.",
                    }
                ],
            }
        )
    if mutation == "missing_article":
        items.pop()
    elif mutation == "extra_article":
        items.append({**items[0], "article_id": "art-extra123456"})
    elif mutation == "duplicate_slug":
        items[1]["slug"] = items[0]["slug"]
    elif mutation == "missing_rights_url":
        items[0]["image_candidates"][0].pop("rights_url")

    payload = {
        "schema_version": 1,
        "batch_id": prepared.batch_id,
        "request_id": (
            "req-wrong123456" if mutation == "wrong_request_id" else prepared.request["request_id"]
        ),
        "output_file": prepared.request["output_file"],
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_content_import_registers_every_item_and_creates_fact_check_request(
    prepared_batch: SimpleNamespace, tmp_path: Path
):
    result_path = write_content_result(prepared_batch, tmp_path / "content-result.json")

    result = prepared_batch.workflow.import_content(prepared_batch.batch_id, result_path)

    assert result["imported"] == len(prepared_batch.article_ids)
    for article_id in prepared_batch.article_ids:
        article = prepared_batch.room.get_article(article_id)
        assert article["status"] == "DRAFTED"
        assert Path(prepared_batch.room.root, article["draft_path"]).name == "article-v1.md"
    assert Path(result["fact_check_request_path"]).exists()
    status = prepared_batch.workflow.status(prepared_batch.batch_id)
    assert status["fact_check_request_path"] == result["fact_check_request_path"]


def test_one_missing_article_rolls_back_entire_content_import(
    prepared_batch: SimpleNamespace, tmp_path: Path
):
    result_path = write_content_result(
        prepared_batch, tmp_path / "missing-result.json", mutation="missing_article"
    )

    with pytest.raises(WorkflowError, match="missing article IDs"):
        prepared_batch.workflow.import_content(prepared_batch.batch_id, result_path)

    assert all(
        prepared_batch.room.get_article(article_id)["status"] == "SELECTED"
        for article_id in prepared_batch.article_ids
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("wrong_request_id", "request_id"),
        ("extra_article", "unexpected article IDs"),
        ("duplicate_slug", "duplicate slug"),
        ("missing_rights_url", "rights_url"),
    ],
)
def test_content_import_rejects_invalid_batch_result(
    prepared_batch: SimpleNamespace, tmp_path: Path, mutation: str, message: str
):
    path = write_content_result(
        prepared_batch, tmp_path / f"{mutation}.json", mutation=mutation
    )
    with pytest.raises((WorkflowError, ValidationError), match=message):
        prepared_batch.workflow.import_content(prepared_batch.batch_id, path)


def test_reimporting_identical_content_result_is_idempotent(
    prepared_batch: SimpleNamespace, tmp_path: Path
):
    path = write_content_result(prepared_batch, tmp_path / "content-result.json")

    first = prepared_batch.workflow.import_content(prepared_batch.batch_id, path)
    second = prepared_batch.workflow.import_content(prepared_batch.batch_id, path)

    assert first["request_id"] == second["request_id"]


@pytest.fixture
def content_batch(prepared_batch: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:
    content_result = write_content_result(prepared_batch, tmp_path / "content-result.json")
    imported = prepared_batch.workflow.import_content(prepared_batch.batch_id, content_result)
    fact_request_path = Path(imported["fact_check_request_path"])
    fact_request = json.loads(fact_request_path.read_text(encoding="utf-8"))
    assert "blocking_issues" in fact_request["instructions"]
    prepared_batch.fact_request_path = fact_request_path
    prepared_batch.fact_request = fact_request
    prepared_batch.article_ids = [item["article_id"] for item in fact_request["items"]]
    return prepared_batch


def write_fact_check_result(
    content: SimpleNamespace,
    path: Path,
    outcomes: list[str],
    *,
    request: dict | None = None,
) -> Path:
    request = request or content.fact_request
    items = []
    for item, outcome in zip(request["items"], outcomes, strict=True):
        items.append(
            {
                "article_id": item["article_id"],
                "outcome": outcome,
                "report_md": "# Fact-check report\n\nAll material claims were checked against the listed sources.",
                "blocking_issues": (
                    [] if outcome == "PASS" else ["The launch date is not supported by the primary source."]
                ),
            }
        )
    payload = {
        "schema_version": 1,
        "batch_id": content.batch_id,
        "request_id": request["request_id"],
        "output_file": request["output_file"],
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_fact_check_import_routes_pass_and_fail(content_batch: SimpleNamespace, tmp_path: Path):
    path = write_fact_check_result(
        content_batch, tmp_path / "fact-check-result.json", ["PASS", "FAIL"]
    )

    result = content_batch.workflow.import_fact_checks(content_batch.batch_id, path)

    assert result["passed"] == 1
    assert result["needs_revision"] == 1
    assert content_batch.room.get_article(content_batch.article_ids[0])["status"] == "FACT_CHECKED"
    assert content_batch.room.get_article(content_batch.article_ids[1])["status"] == "NEEDS_REVISION"


@pytest.fixture
def revised_batch(content_batch: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:
    first_request = content_batch.fact_request
    failed = write_fact_check_result(
        content_batch, tmp_path / "first-fact-check.json", ["FAIL", "PASS"]
    )
    content_batch.workflow.import_fact_checks(content_batch.batch_id, failed)
    revision_request_path = content_batch.workflow.create_revision_request(content_batch.batch_id)
    revision_request = json.loads(revision_request_path.read_text(encoding="utf-8"))
    assert "research_brief_md" in revision_request["instructions"]
    result_items = []
    for item in revision_request["items"]:
        result_items.append(
            {
                "article_id": item["article_id"],
                "research_brief_md": "# Revised research brief\n\nThe date was rechecked against the primary source.",
                "title_cn": "修订后的示例新闻标题",
                "slug": "revised-example-news",
                "article_md": "# 修订后的示例新闻标题\n\n这是修正日期后、基于来源重新撰写的完整文章内容。",
            }
        )
    revision_result = {
        "schema_version": 1,
        "batch_id": content_batch.batch_id,
        "request_id": revision_request["request_id"],
        "output_file": revision_request["output_file"],
        "items": result_items,
    }
    revision_result_path = tmp_path / "revision-result.json"
    revision_result_path.write_text(
        json.dumps(revision_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    imported = content_batch.workflow.import_revisions(
        content_batch.batch_id, revision_result_path
    )
    content_batch.first_fact_request = first_request
    content_batch.fact_request_path = Path(imported["fact_check_request_path"])
    content_batch.fact_request = json.loads(
        content_batch.fact_request_path.read_text(encoding="utf-8")
    )
    return content_batch


def test_revision_preserves_v1_and_writes_v2(revised_batch: SimpleNamespace):
    article_id = revised_batch.article_ids[0]
    article_dir = revised_batch.room.root / "articles" / article_id

    assert (article_dir / "article-v1.md").exists()
    assert (article_dir / "article-v2.md").exists()
    assert revised_batch.room.get_article(article_id)["revision_number"] == 2
    assert revised_batch.room.get_article(article_id)["status"] == "DRAFTED"


def test_second_round_rejects_first_round_fact_check_result(
    revised_batch: SimpleNamespace, tmp_path: Path
):
    stale = write_fact_check_result(
        revised_batch,
        tmp_path / "stale-first-round.json",
        ["PASS", "PASS"],
        request=revised_batch.first_fact_request,
    )

    with pytest.raises(WorkflowError, match="request_id"):
        revised_batch.workflow.import_fact_checks(revised_batch.batch_id, stale)
