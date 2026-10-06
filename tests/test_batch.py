from __future__ import annotations

import csv
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from newsroom.batch import BatchWorkflow
from newsroom.service import Newsroom, WorkflowError
from newsroom.workspace import BatchWorkspace


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
    assert result["content_request_path"] is None
    assert newsroom.db.all("SELECT * FROM batch_requests") == []


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
    assert newsroom.get_article(article["article_id"])["owning_batch_id"] is None
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 1


def test_prepare_same_file_is_idempotent(newsroom: Newsroom, tmp_path: Path):
    csv_path = write_batch_csv(tmp_path / "same.csv", [candidate_row()])

    first = BatchWorkflow(newsroom).prepare(csv_path)
    second = BatchWorkflow(newsroom).prepare(csv_path)

    assert first["batch_id"] == second["batch_id"]
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM batches")["count"] == 1


def test_prepare_copies_csv_and_writes_human_named_content_request(newsroom: Newsroom, tmp_path: Path):
    source = write_batch_csv(tmp_path / "reviewed.csv", [candidate_row()])

    result = BatchWorkflow(newsroom).prepare(source)

    root = tmp_path / "workspace" / "batches" / result["batch_id"]
    assert Path(result["content_request_path"]) == root / "claude" / "requests" / "content-request.json"
    assert (root / "input" / "candidates.csv").read_bytes() == source.read_bytes()
    state = json.loads((root / "batch-state.json").read_text())
    assert state["status"] == result["status"]
    assert state["requests"][0]["request_path"] == "claude/requests/content-request.json"


def test_prepare_batch_article_has_no_legacy_research_template(newsroom: Newsroom, tmp_path: Path):
    result = BatchWorkflow(newsroom).prepare(write_batch_csv(tmp_path / "reviewed.csv", [candidate_row()]))
    article_id = result["items"][0]["article_id"]

    assert newsroom.get_article(article_id)["research_brief_path"] is None
    assert not list((tmp_path / "research").rglob("*.md"))


def test_source_csv_copy_rejects_hash_mismatch_before_copy(newsroom: Newsroom, tmp_path: Path, monkeypatch):
    source = write_batch_csv(tmp_path / "reviewed.csv", [candidate_row()])
    ensure_layout = BatchWorkspace.ensure_layout

    def change_recorded_hash(workspace, batch_id):
        root = ensure_layout(workspace, batch_id)
        with newsroom.db.connect() as connection:
            connection.execute("UPDATE batches SET source_sha256='different-hash' WHERE batch_id=?", (batch_id,))
        return root

    monkeypatch.setattr(BatchWorkspace, "ensure_layout", change_recorded_hash)

    with pytest.raises(WorkflowError, match="source_sha256"):
        BatchWorkflow(newsroom).prepare(source)

    assert not list((tmp_path / "workspace").rglob("candidates.csv"))
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 0


def test_byte_different_csv_reuses_candidates_but_creates_new_batch(newsroom: Newsroom, tmp_path: Path):
    first_path = write_batch_csv(tmp_path / "first.csv", [candidate_row()], line_ending="\n")
    second_path = write_batch_csv(tmp_path / "second.csv", [candidate_row()], line_ending="\r\n")

    one = BatchWorkflow(newsroom).prepare(first_path)
    two = BatchWorkflow(newsroom).prepare(second_path)

    assert one["batch_id"] != two["batch_id"]
    article = newsroom.get_article(one["items"][0]["article_id"])
    assert article["owning_batch_id"] == one["batch_id"]
    recreated = newsroom.create_brief(article["candidate_id"], owning_batch_id=two["batch_id"])
    assert recreated["owning_batch_id"] == one["batch_id"]
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
    assert newsroom.get_article(item["article_id"])["owning_batch_id"] == result["batch_id"]


def test_resolve_preserves_superseded_content_request_bytes(newsroom: Newsroom, tmp_path: Path):
    workflow = BatchWorkflow(newsroom)
    prepared = workflow.prepare(write_batch_csv(tmp_path / "duplicates.csv", duplicate_rows()))
    path = tmp_path / "workspace/batches" / prepared["batch_id"] / "claude/requests/content-request.json"
    request_id = "req-legacy123456"
    path.write_bytes(json.dumps({"batch_id": prepared["batch_id"], "request_id": request_id}, indent=2).replace("\n", "\r\n").encode())
    with newsroom.db.connect() as connection:
        connection.execute(
            "INSERT INTO batch_requests(request_id,batch_id,request_type,revision_number,request_path,expected_result_filename,status,created_at) VALUES (?,?,'CONTENT',1,?,?,'PENDING','2026-10-05')",
            (request_id, prepared["batch_id"], str(path.relative_to(tmp_path)), f"{prepared['batch_id']}-content-result.json"),
        )
        connection.execute("UPDATE batches SET content_request_id=? WHERE batch_id=?", (request_id, prepared["batch_id"]))
    original = path.read_bytes()
    waiting = next(item for item in prepared["items"] if item["item_status"] == "WAITING_DUPLICATE")
    newsroom.override_duplicate(waiting["candidate_id"], "FOLLOW_UP", "independent follow-up", "editor")

    resolved = workflow.resolve(prepared["batch_id"], waiting["candidate_id"], "include", "editor", "follow-up confirmed")

    old = newsroom.db.one("SELECT * FROM batch_requests WHERE request_id=?", (request_id,))
    assert old["status"] == "SUPERSEDED"
    assert (tmp_path / old["request_path"]).read_bytes() == original
    assert Path(resolved["content_request_path"]) == path
    assert json.loads(path.read_bytes())["request_id"] != request_id


@pytest.mark.parametrize("decision", ["include", "exclude"])
def test_resolve_final_duplicate_creates_complete_content_request(newsroom: Newsroom, tmp_path: Path, decision: str):
    workflow = BatchWorkflow(newsroom)
    prepared = workflow.prepare(write_batch_csv(tmp_path / "duplicates.csv", duplicate_rows()))
    waiting = next(item for item in prepared["items"] if item["item_status"] == "WAITING_DUPLICATE")
    if decision == "include":
        newsroom.override_duplicate(waiting["candidate_id"], "FOLLOW_UP", "independent follow-up", "editor")

    resolved = workflow.resolve(prepared["batch_id"], waiting["candidate_id"], decision, "editor", "duplicate reviewed")

    request = json.loads(Path(resolved["content_request_path"]).read_text())
    expected = {item["article_id"] for item in resolved["items"] if item["item_status"] == "READY_FOR_CONTENT"}
    assert {item["article_id"] for item in request["items"]} == expected
    assert len(request["items"]) == (3 if decision == "include" else 2)
    assert "research_brief_md" in request["instructions"]
    assert "rights_url" in request["instructions"]


def test_resolve_earlier_duplicate_does_not_create_partial_request(newsroom: Newsroom, tmp_path: Path):
    rows = duplicate_rows() + [candidate_row(candidate_title="Another Tool launch report", primary_source="https://third.example/tool")]
    workflow = BatchWorkflow(newsroom)
    prepared = workflow.prepare(write_batch_csv(tmp_path / "duplicates.csv", rows))
    waiting = [item for item in prepared["items"] if item["item_status"] == "WAITING_DUPLICATE"]
    assert len(waiting) == 2

    resolved = workflow.resolve(prepared["batch_id"], waiting[0]["candidate_id"], "exclude", "editor", "duplicate reviewed")

    assert resolved["content_request_path"] is None
    assert newsroom.db.all("SELECT * FROM batch_requests") == []


@pytest.mark.parametrize("decision", ["include", "exclude"])
def test_resolve_rejects_legacy_completed_partial_batch_without_mutation(newsroom: Newsroom, tmp_path: Path, decision: str):
    workflow = BatchWorkflow(newsroom)
    prepared = workflow.prepare(write_batch_csv(tmp_path / "duplicates.csv", duplicate_rows()))
    waiting = next(item for item in prepared["items"] if item["item_status"] == "WAITING_DUPLICATE")
    newsroom.override_duplicate(waiting["candidate_id"], "FOLLOW_UP", "independent follow-up", "editor")
    with newsroom.db.connect() as connection:
        connection.execute(
            "INSERT INTO batch_requests(request_id,batch_id,request_type,revision_number,request_path,expected_result_filename,status,created_at) VALUES ('req-legacy123456',?,'CONTENT',1,'legacy-request.json','legacy-result.json','COMPLETED','2026-10-05')", (prepared["batch_id"],)
        )
    before = {table: newsroom.db.all(f"SELECT * FROM {table}") for table in ("candidates", "articles", "batches", "batch_items", "batch_requests", "state_transitions")}
    paths = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    with pytest.raises(WorkflowError, match="content.*completed"):
        workflow.resolve(prepared["batch_id"], waiting["candidate_id"], decision, "editor", "duplicate reviewed")

    assert {table: newsroom.db.all(f"SELECT * FROM {table}") for table in before} == before
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == paths


def test_pending_revision_request_is_preserved_when_repeated(content_batch: SimpleNamespace, tmp_path: Path):
    result = write_fact_check_result(content_batch, tmp_path / "failed.json", ["FAIL", "PASS"])
    content_batch.workflow.import_fact_checks(content_batch.batch_id, result)
    request = content_batch.workflow.create_revision_request(content_batch.batch_id)
    before = request.read_bytes()

    with pytest.raises(WorkflowError, match="already pending"):
        content_batch.workflow.create_revision_request(content_batch.batch_id)

    assert request.read_bytes() == before


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


def test_content_import_stages_result_bytes_before_archive(prepared_batch: SimpleNamespace, tmp_path: Path, monkeypatch):
    result = write_content_result(prepared_batch, tmp_path / "content-result.json")
    write_bytes = Path.write_bytes

    def fail_result_write(path, data):
        if path.name == "content-result.json":
            write_bytes(path, data[:20])
            raise OSError("result write failed")
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_result_write)
    with pytest.raises(OSError, match="result write failed"):
        prepared_batch.workflow.import_content(prepared_batch.batch_id, result)

    root = tmp_path / "workspace/batches" / prepared_batch.batch_id
    assert not (root / "claude/archive/content-result.json").exists()
    assert not list(root.glob(".staging-*"))
    assert all(prepared_batch.room.get_article(article_id)["status"] == "SELECTED" for article_id in prepared_batch.article_ids)


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
    assert first == second
    assert prepared_batch.room.db.one("SELECT COUNT(*) AS count FROM image_candidates")["count"] == 2


def test_content_and_fact_check_import_use_article_workspace(prepared_batch: SimpleNamespace, tmp_path: Path):
    content = write_content_result(prepared_batch, tmp_path / "content.json")
    imported = prepared_batch.workflow.import_content(prepared_batch.batch_id, content)
    prefix = f"workspace/batches/{prepared_batch.batch_id}"
    article_id = prepared_batch.article_ids[0]
    article = prepared_batch.room.get_article(article_id)
    assert article["research_brief_path"] == f"{prefix}/articles/{article_id}/research-brief-v1.md"
    assert article["draft_path"] == f"{prefix}/articles/{article_id}/article-v1.md"
    assert Path(imported["fact_check_request_path"]) == tmp_path / prefix / "claude/requests/fact-check-request-v1.json"
    archived = tmp_path / prefix / "claude/archive/content-result.json"
    assert archived.read_bytes() == content.read_bytes()
    images = json.loads((tmp_path / prefix / "articles" / article_id / "image-candidates.json").read_text())
    assert images == prepared_batch.room.list_image_candidates(article_id)
    request = prepared_batch.room.db.one("SELECT * FROM batch_requests WHERE request_id=?", (imported["request_id"],))
    assert request["result_path"] == f"{prefix}/claude/archive/content-result.json"
    prepared_batch.fact_request = json.loads(Path(imported["fact_check_request_path"]).read_text())
    checked = write_fact_check_result(prepared_batch, tmp_path / "checked.json", ["PASS", "FAIL"])
    prepared_batch.workflow.import_fact_checks(prepared_batch.batch_id, checked)
    assert prepared_batch.room.get_article(article_id)["fact_check_path"] == f"{prefix}/articles/{article_id}/fact-check-v1.md"
    assert (tmp_path / prefix / "claude/archive/fact-check-result-v1.json").read_bytes() == checked.read_bytes()
    state = json.loads((tmp_path / prefix / "batch-state.json").read_text())
    assert state["status"] == "NEEDS_REVISION"
    assert not list((tmp_path / prefix).glob(".staging-*"))


def test_content_import_refreshes_reused_article_owner_workspace(prepared_batch: SimpleNamespace, tmp_path: Path):
    source_rows = duplicate_rows()[::2]
    reused = prepared_batch.workflow.prepare(write_batch_csv(tmp_path / "reuse.csv", source_rows, line_ending="\r\n"))
    assert reused["batch_id"] != prepared_batch.batch_id
    request = json.loads(Path(reused["content_request_path"]).read_text())
    imported_batch = SimpleNamespace(batch_id=reused["batch_id"], request=request)
    article_id = prepared_batch.article_ids[0]
    owner_root = tmp_path / "workspace/batches" / prepared_batch.batch_id
    reused_root = tmp_path / "workspace/batches" / reused["batch_id"]
    candidates_path = owner_root / "articles" / article_id / "image-candidates.json"
    assert json.loads(candidates_path.read_text()) == []
    result = write_content_result(imported_batch, tmp_path / "reused-content.json")

    imported = prepared_batch.workflow.import_content(reused["batch_id"], result)

    assert json.loads(candidates_path.read_text()) == prepared_batch.room.list_image_candidates(article_id)
    assert len(json.loads(candidates_path.read_text())) == 1
    for root in (owner_root, reused_root):
        state = json.loads((root / "batch-state.json").read_text())
        item = next(item for item in state["items"] if item["article_id"] == article_id)
        assert item["article_status"] == "DRAFTED"
        assert item["image_state"] == "PROPOSED"
        assert item["owning_batch_id"] == prepared_batch.batch_id
        assert item["article_path"] in (root / "README.md").read_text()
        assert (root / item["draft_path"]).exists()
    assert not (reused_root / "articles" / article_id).exists()

    imported_batch.fact_request = json.loads(Path(imported["fact_check_request_path"]).read_text())
    checked = write_fact_check_result(imported_batch, tmp_path / "reused-fact-check.json", ["FAIL", "PASS"])
    prepared_batch.workflow.import_fact_checks(reused["batch_id"], checked)
    owner_state = json.loads((owner_root / "batch-state.json").read_text())
    assert {item["article_status"] for item in owner_state["items"]} == {"FACT_CHECKED", "NEEDS_REVISION"}
    revision_path = prepared_batch.workflow.create_revision_request(reused["batch_id"])
    revision_request = json.loads(revision_path.read_text())
    revision_result = write_revision_result(revision_request, tmp_path / "reused-revision.json")
    prepared_batch.workflow.import_revisions(reused["batch_id"], revision_result)
    revised_id = revision_request["items"][0]["article_id"]
    for root in (owner_root, reused_root):
        state = json.loads((root / "batch-state.json").read_text())
        revised = next(item for item in state["items"] if item["article_id"] == revised_id)
        assert revised["revision_number"] == 2
        assert revised["article_status"] == "DRAFTED"
        assert (root / revised["draft_path"]).exists()


def write_revision_result(request: dict, path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "batch_id": request["batch_id"],
        "request_id": request["request_id"],
        "output_file": request["output_file"],
        "items": [{
            "article_id": item["article_id"],
            "research_brief_md": item["research_brief_md"],
            "title_cn": item["title_cn"],
            "slug": item["slug"],
            "article_md": item["article_md"],
        } for item in request["items"]],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("import_stage", ["content", "revision"])
@pytest.mark.parametrize("failure_step", ["insert", "update"])
def test_fact_check_request_sql_failure_removes_new_file_and_allows_retry(
    prepared_batch: SimpleNamespace, tmp_path: Path, failure_step: str, import_stage: str
):
    room = prepared_batch.room
    content = write_content_result(prepared_batch, tmp_path / "content-for-retry.json")
    import_result = prepared_batch.workflow.import_content
    result_path = content
    revision_number = 1
    if import_stage == "revision":
        content_imported = import_result(prepared_batch.batch_id, content)
        prepared_batch.fact_request = json.loads(Path(content_imported["fact_check_request_path"]).read_text())
        failed = write_fact_check_result(prepared_batch, tmp_path / "failed-for-retry.json", ["FAIL", "PASS"])
        prepared_batch.workflow.import_fact_checks(prepared_batch.batch_id, failed)
        request = prepared_batch.workflow.create_revision_request(prepared_batch.batch_id)
        result_path = write_revision_result(json.loads(request.read_text()), tmp_path / "revision-for-retry.json")
        import_result = prepared_batch.workflow.import_revisions
        revision_number = 2
    if failure_step == "insert":
        trigger = "BEFORE INSERT ON batch_requests WHEN NEW.request_type='FACT_CHECK'"
    else:
        trigger = "BEFORE UPDATE OF fact_check_request_id ON batches"
    with room.db.connect() as connection:
        connection.execute(f"CREATE TRIGGER fail_fact_request {trigger} BEGIN SELECT RAISE(ABORT,'fact request SQL failed'); END")
    tables = ("candidates", "articles", "batches", "batch_items", "batch_requests", "image_candidates", "state_transitions")
    before = {table: room.db.all(f"SELECT * FROM {table}") for table in tables}
    root = tmp_path / "workspace/batches" / prepared_batch.batch_id
    before_files = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    with pytest.raises(sqlite3.IntegrityError, match="fact request SQL failed"):
        import_result(prepared_batch.batch_id, result_path)

    assert not (root / f"claude/requests/fact-check-request-v{revision_number}.json").exists()
    assert {table: room.db.all(f"SELECT * FROM {table}") for table in tables} == before
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before_files
    assert not list(root.glob(".staging-*"))
    with room.db.connect() as connection:
        connection.execute("DROP TRIGGER fail_fact_request")

    imported = import_result(prepared_batch.batch_id, result_path)

    assert imported["imported" if import_stage == "content" else "revised"] == (2 if import_stage == "content" else 1)
    assert (root / f"claude/requests/fact-check-request-v{revision_number}.json").exists()


def test_content_import_preserves_preexisting_fact_check_request_file(prepared_batch: SimpleNamespace, tmp_path: Path):
    room = prepared_batch.room
    root = tmp_path / "workspace/batches" / prepared_batch.batch_id
    existing = root / "claude/requests/fact-check-request-v1.json"
    existing.write_bytes(b"existing immutable request")
    before = room.db.all("SELECT * FROM articles ORDER BY article_id")
    content = write_content_result(prepared_batch, tmp_path / "content-existing-request.json")

    with pytest.raises(WorkflowError, match="fact-check request version 1 already exists"):
        prepared_batch.workflow.import_content(prepared_batch.batch_id, content)

    assert existing.read_bytes() == b"existing immutable request"
    assert room.db.all("SELECT * FROM articles ORDER BY article_id") == before


def test_content_import_rejects_another_batch_inbox_result_without_mutation(
    prepared_batch: SimpleNamespace, tmp_path: Path
):
    other = prepared_batch.workflow.prepare(write_batch_csv(tmp_path / "other.csv", [candidate_row(
        candidate_title="Separate launches Device", core_fact="Separate launched Device.",
        company="Separate", product="Device", primary_source="https://separate.example/device",
    )]))
    request = json.loads(Path(other["content_request_path"]).read_text())
    foreign = SimpleNamespace(batch_id=other["batch_id"], request=request)
    inbox = tmp_path / "workspace/batches" / prepared_batch.batch_id / "claude/inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    result = write_content_result(foreign, inbox / "foreign-content.json")
    before_articles = prepared_batch.room.db.all("SELECT * FROM articles ORDER BY article_id")
    before_requests = prepared_batch.room.db.all("SELECT * FROM batch_requests ORDER BY request_id")
    before_paths = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    with pytest.raises(WorkflowError, match="batch_id"):
        prepared_batch.workflow.import_content(prepared_batch.batch_id, result)

    assert prepared_batch.room.db.all("SELECT * FROM articles ORDER BY article_id") == before_articles
    assert prepared_batch.room.db.all("SELECT * FROM batch_requests ORDER BY request_id") == before_requests
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before_paths


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
    article_dir = revised_batch.room.root / "workspace/batches" / revised_batch.batch_id / "articles" / article_id

    assert (article_dir / "article-v1.md").exists()
    assert (article_dir / "article-v2.md").exists()
    assert revised_batch.room.get_article(article_id)["revision_number"] == 2
    assert revised_batch.room.get_article(article_id)["status"] == "DRAFTED"


def test_revision_canonical_path_preserves_request_result_versions(revised_batch: SimpleNamespace):
    root = revised_batch.room.root / "workspace/batches" / revised_batch.batch_id
    for relative in (
        "claude/requests/content-request.json",
        "claude/requests/fact-check-request-v1.json",
        "claude/requests/revision-request-v2.json",
        "claude/requests/fact-check-request-v2.json",
        "claude/archive/content-result.json",
        "claude/archive/fact-check-result-v1.json",
        "claude/archive/revision-result-v2.json",
    ):
        assert (root / relative).is_file()
    state = json.loads((root / "batch-state.json").read_text())
    assert state["status"] == "WAITING_FACT_CHECK"
    assert sorted(request["revision_number"] for request in state["requests"]) == [1, 1, 2, 2]


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


@pytest.mark.parametrize("preselected", [False, True])
def test_batch_created_article_records_owning_batch(
    newsroom: Newsroom, tmp_path: Path, preselected: bool
):
    source = write_batch_csv(tmp_path / "batch.csv", [candidate_row()])
    if preselected:
        candidate_id = newsroom.import_candidates(source)[0]
        newsroom.check_duplicate(candidate_id)
        newsroom.select_candidate(candidate_id, "editor", "selected before batch preparation")

    prepared = BatchWorkflow(newsroom).prepare(source)

    article = newsroom.get_article(prepared["items"][0]["article_id"])
    assert article["owning_batch_id"] == prepared["batch_id"]
    batch = newsroom.db.one("SELECT workspace_version FROM batches WHERE batch_id=?", (prepared["batch_id"],))
    assert batch["workspace_version"] == 1
