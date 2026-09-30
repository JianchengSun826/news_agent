import json
import sqlite3
from pathlib import Path

import pytest

from newsroom.db import Database
from newsroom.dedup import classify_candidate
from newsroom.models import CandidateInput, make_event_id
from newsroom.service import Newsroom, WorkflowError


@pytest.fixture
def newsroom(tmp_path: Path) -> Newsroom:
    room = Newsroom(tmp_path)
    room.init()
    return room


def add_candidate(room: Newsroom, **overrides):
    values = {
        "candidate_title": "OpenAI releases Codex Desktop",
        "core_fact": "OpenAI released Codex Desktop on 2026-09-28.",
        "event_date": "2026-09-28",
        "company": "OpenAI",
        "product": "Codex",
        "topic": "AI Coding",
        "subtopic": "Developer Tools",
        "event_type": "launch",
        "keywords": ["openai", "codex", "desktop"],
        "primary_source": "https://openai.com/news/codex-desktop",
        "secondary_sources": ["https://example.com/independent-report"],
        "discovered_from": "manual",
        "priority": 4,
    }
    values.update(overrides)
    return room.add_candidate(**values)


def test_event_id_is_stable_lowercase_ascii():
    assert make_event_id("OpenAI", "Codex Desktop", "Launch", "2026-09-28") == "openai-codex-desktop-launch-20260928"


def test_candidate_input_uses_pydantic_for_priority_and_urls():
    with pytest.raises(ValueError):
        CandidateInput(
            candidate_title="Example", core_fact="Example happened.", event_date="2026-09-29",
            company="Example", product="Tool", topic="AI", subtopic="Tools", event_type="launch",
            keywords=["example"], primary_source="not-a-url", secondary_sources=[],
            discovered_from="manual", priority=9,
        )


def test_invalid_state_transition_is_rejected_and_audited_only_on_success(newsroom: Newsroom):
    candidate = add_candidate(newsroom)
    with pytest.raises(WorkflowError, match="requires DUPLICATE_CHECKED"):
        newsroom.select_candidate(candidate["candidate_id"], actor="editor", reason="strong story")
    assert newsroom.get_candidate(candidate["candidate_id"])["status"] == "CANDIDATE"
    assert [row["to_status"] for row in newsroom.audit(candidate["candidate_id"])] == ["DISCOVERED", "CANDIDATE"]


def test_duplicate_heuristic_uses_event_attributes():
    existing = {
        "event_id": "openai-codex-launch-20260928",
        "event_date": "2026-09-28",
        "company": "OpenAI",
        "product": "Codex",
        "event_type": "launch",
        "core_fact": "OpenAI released Codex Desktop on 2026-09-28.",
        "keywords": ["openai", "codex", "desktop"],
    }
    exact = dict(existing)
    exact["candidate_id"] = "cand-exact"
    result = classify_candidate(exact, [existing])
    assert result.classification == "DUPLICATE"
    assert result.matching_event_id == existing["event_id"]


def test_invalid_primary_source_is_rejected(newsroom: Newsroom):
    with pytest.raises(ValueError, match="primary_source"):
        add_candidate(newsroom, primary_source="not-a-url")


def test_primary_only_candidate_is_accepted(newsroom: Newsroom):
    candidate = add_candidate(newsroom, secondary_sources=[])

    assert candidate["secondary_sources"] == []


def prepare_fact_checked_article(room: Newsroom, tmp_path: Path) -> str:
    candidate = add_candidate(room)
    room.check_duplicate(candidate["candidate_id"])
    room.override_duplicate(candidate["candidate_id"], "NEW", "editor confirmed distinct event", "editor")
    room.select_candidate(candidate["candidate_id"], "editor", "high reader value")
    article = room.create_brief(candidate["candidate_id"])
    research = tmp_path / "research-input.md"
    research.write_text("# Confirmed Facts\n\nLaunch confirmed by primary source.\n", encoding="utf-8")
    room.register_research(article["article_id"], research)
    draft = tmp_path / "draft.md"
    draft.write_text("# OpenAI 发布 Codex Desktop\n\n这是经过来源核验的示例稿件。\n", encoding="utf-8")
    room.register_article(article["article_id"], draft, "OpenAI 发布 Codex Desktop", "codex-desktop-launch")
    fact_check = tmp_path / "fact-check.md"
    fact_check.write_text("# Fact Check\n\nPASS: dates and claims verified.\n", encoding="utf-8")
    room.register_fact_check(article["article_id"], fact_check)
    return article["article_id"]


def test_image_without_rights_cannot_become_ready(newsroom: Newsroom, tmp_path: Path):
    article_id = prepare_fact_checked_article(newsroom, tmp_path)
    with pytest.raises(WorkflowError, match="license or AI provenance"):
        newsroom.register_image(article_id, image_type="OTHER", source_url="https://example.com/image.jpg")


def test_image_file_is_required_to_become_ready(newsroom: Newsroom, tmp_path: Path):
    article_id = prepare_fact_checked_article(newsroom, tmp_path)

    with pytest.raises(WorkflowError, match="image file is required"):
        newsroom.register_image(article_id, image_type="AI_GENERATED", ai_provider="Gemini", ai_model="manual-subscription", prompt_path="prompts/image_concept.md")

    assert newsroom.get_article(article_id)["status"] == "FACT_CHECKED"


def test_approval_guard_and_publication_package(newsroom: Newsroom, tmp_path: Path):
    article_id = prepare_fact_checked_article(newsroom, tmp_path)
    with pytest.raises(WorkflowError, match="IMAGE_READY"):
        newsroom.approve(article_id, "editor", "ready")
    prepared_image = tmp_path / "prepared-image.png"
    prepared_image.write_bytes(b"fake-png-image-data")
    image_id = newsroom.register_image(article_id, image_file=prepared_image, image_type="AI_GENERATED", ai_provider="Gemini", ai_model="manual-subscription", prompt_path="prompts/image_concept.md")
    stored_image = newsroom.root / "images" / article_id / "hero.png"
    assert stored_image.read_bytes() == b"fake-png-image-data"
    assert newsroom.db.one("SELECT image_path FROM images WHERE image_id=?", (image_id,))["image_path"] == f"images/{article_id}/hero.png"
    newsroom.complete_review(article_id, "editor", "sources, title, facts, and rights checked")
    newsroom.approve(article_id, "editor", "approved for local publication package")
    package = newsroom.create_publication_package(article_id)
    assert (package / "article.md").exists()
    assert (package / "hero.png").read_bytes() == b"fake-png-image-data"
    assert json.loads((package / "metadata.json").read_text())["status"] == "APPROVED"
    assert json.loads((package / "image-license.json").read_text())["image_path"] == f"images/{article_id}/hero.png"
    assert "[x] approved for publication" in (package / "publication-checklist.md").read_text()
    with pytest.raises(FileExistsError):
        newsroom.create_publication_package(article_id)


def test_database_schema_contains_audit_and_registry(newsroom: Newsroom):
    tables = Database(newsroom.db_path).table_names()
    assert {"candidates", "articles", "sources", "images", "state_transitions"} <= tables


def test_init_migrates_v1_images_table_to_store_image_path(tmp_path: Path):
    db_path = tmp_path / "data" / "newsroom.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (1, '2026-09-29T00:00:00+00:00')")
        connection.execute("CREATE TABLE images (image_id TEXT PRIMARY KEY)")

    Database(db_path).initialize("2026-09-30T00:00:00+00:00")

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(images)")}
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version ORDER BY version")]
    assert "image_path" in columns
    assert versions == [1, 2]


def test_init_seeds_source_registry(newsroom: Newsroom):
    sources = newsroom.db.all("SELECT source_id,domain FROM sources ORDER BY source_id")
    assert {row["domain"] for row in sources} >= {"openai.com", "reuters.com"}


def test_prompt_package_uses_versioned_template_and_article_context(newsroom: Newsroom):
    candidate = add_candidate(newsroom)
    newsroom.check_duplicate(candidate["candidate_id"])
    newsroom.override_duplicate(candidate["candidate_id"], "NEW", "human verification", "editor")
    newsroom.select_candidate(candidate["candidate_id"], "editor", "valuable")
    article = newsroom.create_brief(candidate["candidate_id"])
    prompt_path = newsroom.create_prompt(article["article_id"], "research")
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "version: 1" in prompt
    assert article["event_id"] in prompt
    assert candidate["primary_source"] in prompt


def test_reject_records_reason_in_candidate_and_audit(newsroom: Newsroom):
    candidate = add_candidate(newsroom)
    newsroom.reject_candidate(candidate["candidate_id"], "editor", "insufficient reader value")
    stored = newsroom.get_candidate(candidate["candidate_id"])
    assert stored["status"] == "REJECTED"
    assert stored["rejection_reason"] == "insufficient reader value"
    assert newsroom.audit(candidate["candidate_id"])[-1]["to_status"] == "REJECTED"
