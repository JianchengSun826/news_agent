from contextlib import closing
import json
import sqlite3
from pathlib import Path

import pytest

from newsroom.db import SCHEMA, Database
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


def add_image_candidate(room: Newsroom, article_id: str, batch_id: str = "batch-images") -> dict:
    now = "2026-10-05T00:00:00+00:00"
    candidate_id = "imgcand-example"
    with room.db.connect() as connection:
        connection.execute(
            "INSERT INTO batches(batch_id,source_filename,source_sha256,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (batch_id, "images.csv", batch_id, "READY_FOR_IMAGE", now, now),
        )
        connection.execute(
            "INSERT INTO image_candidates(image_candidate_id,batch_id,article_id,landing_page_url,"
            "direct_image_url,creator,rights_basis,license_name,rights_url,rights_statement,"
            "attribution_required,attribution_text,relevance,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                candidate_id,
                batch_id,
                article_id,
                "https://images.example.org/press/tool",
                "https://images.example.org/press/tool.jpg",
                "Example Press Team",
                "OPEN_LICENSE",
                "CC BY 4.0",
                "https://creativecommons.org/licenses/by/4.0/",
                "The source page identifies the image as CC BY 4.0.",
                1,
                "Example Press Team / CC BY 4.0",
                "The image depicts the announced product.",
                now,
                now,
            ),
        )
    return room.list_image_candidates(article_id)[0]


def test_candidate_image_requires_explicit_rights_confirmation(newsroom: Newsroom, tmp_path: Path):
    article_id = prepare_fact_checked_article(newsroom, tmp_path)
    candidate = add_image_candidate(newsroom, article_id)
    image = tmp_path / "hero.jpg"
    image.write_bytes(b"jpeg")

    with pytest.raises(WorkflowError, match="rights-confirmed"):
        newsroom.register_image(
            article_id,
            image_file=image,
            image_type="OPEN_LICENSE",
            image_candidate_id=candidate["image_candidate_id"],
        )


def test_candidate_image_cannot_be_registered_for_another_article(newsroom: Newsroom, tmp_path: Path):
    article_id = prepare_fact_checked_article(newsroom, tmp_path)
    candidate = add_image_candidate(newsroom, article_id)
    with newsroom.db.connect() as connection:
        connection.execute("UPDATE articles SET status='FACT_CHECKED' WHERE article_id=?", (article_id,))
    other_candidate = add_candidate(
        newsroom,
        candidate_title="Other image story",
        core_fact="Other released Image Tool on 2026-10-04.",
        company="Other",
        product="Image Tool",
        primary_source="https://other.example/image-tool",
    )
    newsroom.check_duplicate(other_candidate["candidate_id"])
    newsroom.select_candidate(other_candidate["candidate_id"], "editor", "separate story")
    other_article = newsroom.create_brief(other_candidate["candidate_id"])
    with newsroom.db.connect() as connection:
        connection.execute(
            "UPDATE articles SET status='FACT_CHECKED' WHERE article_id=?",
            (other_article["article_id"],),
        )
    image = tmp_path / "other.jpg"
    image.write_bytes(b"jpeg")

    with pytest.raises(WorkflowError, match="different article"):
        newsroom.register_image(
            other_article["article_id"],
            image_file=image,
            image_candidate_id=candidate["image_candidate_id"],
            rights_confirmed=True,
        )


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


def test_batch_article_publication_package_uses_current_version_and_rights(
    newsroom: Newsroom, tmp_path: Path
):
    article_id = prepare_fact_checked_article(newsroom, tmp_path)
    article_dir = newsroom.root / "articles" / article_id
    current_article = article_dir / "article-v2.md"
    current_markdown = "# 修订后的文章\n\n这是当前第二版文章，发布包必须使用这一版本。\n"
    current_article.write_text(current_markdown, encoding="utf-8")
    with newsroom.db.connect() as connection:
        connection.execute(
            "UPDATE articles SET draft_path=?,revision_number=2 WHERE article_id=?",
            (str(current_article.relative_to(newsroom.root)), article_id),
        )
    image_candidate = add_image_candidate(newsroom, article_id, "batch-publication")
    downloaded = tmp_path / "licensed.jpg"
    downloaded.write_bytes(b"licensed-image")
    image_id = newsroom.register_image(
        article_id,
        image_file=downloaded,
        image_candidate_id=image_candidate["image_candidate_id"],
        rights_confirmed=True,
    )
    newsroom.complete_review(article_id, "editor", "current article and image rights reviewed")
    newsroom.approve(article_id, "editor", "approved current revision")

    package = newsroom.create_publication_package(article_id)

    metadata = json.loads((package / "metadata.json").read_text(encoding="utf-8"))
    rights = json.loads((package / "image-license.json").read_text(encoding="utf-8"))
    assert metadata["revision_number"] == 2
    assert rights["image_id"] == image_id
    assert rights["image_candidate_id"] == image_candidate["image_candidate_id"]
    assert rights["rights_basis"] == "OPEN_LICENSE"
    assert (package / "article.md").read_text(encoding="utf-8") == current_markdown


def test_database_schema_contains_audit_and_registry(newsroom: Newsroom):
    tables = Database(newsroom.db_path).table_names()
    assert {"candidates", "articles", "sources", "images", "state_transitions"} <= tables


def test_init_migrates_v1_images_table_to_store_image_path(tmp_path: Path):
    db_path = tmp_path / "data" / "newsroom.db"
    db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (1, '2026-09-29T00:00:00+00:00')")
        connection.execute("CREATE TABLE images (image_id TEXT PRIMARY KEY)")
        connection.commit()

    Database(db_path).initialize("2026-09-30T00:00:00+00:00")

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(images)")}
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version ORDER BY version")]
    assert "image_path" in columns
    assert versions == [1, 2, 3, 4]


def build_v2_database(tmp_path: Path, *, duplicate_candidates: bool = False) -> Path:
    db_path = tmp_path / "data" / "newsroom.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_version VALUES (1, '2026-09-29T00:00:00+00:00');
            INSERT INTO schema_version VALUES (2, '2026-09-30T00:00:00+00:00');
            CREATE TABLE candidates (
              candidate_id TEXT PRIMARY KEY,
              event_date TEXT NOT NULL,
              company TEXT NOT NULL,
              product TEXT NOT NULL,
              event_type TEXT NOT NULL,
              core_fact TEXT NOT NULL,
              primary_source TEXT NOT NULL
            );
            CREATE TABLE articles (article_id TEXT PRIMARY KEY);
            CREATE TABLE images (image_id TEXT PRIMARY KEY, image_path TEXT);
            """
        )
        rows = [
            ("cand-one", "2026-10-05", "Example", "Tool", "launch", "Example launched Tool.", "https://example.com/tool"),
        ]
        if duplicate_candidates:
            rows.append(("cand-two", *rows[0][1:]))
        connection.executemany("INSERT INTO candidates VALUES (?,?,?,?,?,?,?)", rows)
    return db_path


def test_init_migrates_v2_to_v4_and_creates_backup(tmp_path: Path):
    db_path = build_v2_database(tmp_path)

    Database(db_path).initialize("2026-10-05T00:00:00+00:00")

    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        candidate_columns = {row[1] for row in connection.execute("PRAGMA table_info(candidates)")}
        article_columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
        image_columns = {row[1] for row in connection.execute("PRAGMA table_info(images)")}
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version ORDER BY version")]
        fingerprint = connection.execute("SELECT candidate_fingerprint FROM candidates WHERE candidate_id='cand-one'").fetchone()[0]
    assert {"batches", "batch_requests", "batch_items", "image_candidates"} <= tables
    assert "candidate_fingerprint" in candidate_columns
    assert {"fact_check_outcome", "revision_number"} <= article_columns
    assert {"image_candidate_id", "rights_basis", "rights_statement"} <= image_columns
    assert versions == [1, 2, 3, 4]
    assert fingerprint
    assert len(list((tmp_path / "data" / "backups").glob("newsroom-v2-*.db"))) == 1


def test_migration_reports_fingerprint_collisions_without_merging(tmp_path: Path):
    db_path = build_v2_database(tmp_path, duplicate_candidates=True)

    with pytest.raises(ValueError, match="candidate fingerprint collision"):
        Database(db_path).initialize("2026-10-05T00:00:00+00:00")

    assert len(list((tmp_path / "data" / "backups").glob("*.db"))) == 1


def test_version_4_init_is_idempotent_and_does_not_create_extra_backup(tmp_path: Path):
    room = Newsroom(tmp_path)

    room.init()
    room.init()

    assert list((tmp_path / "data" / "backups").glob("*.db")) == []


@pytest.mark.parametrize("unsupported_version", [5, -1])
def test_init_rejects_unsupported_schema_without_mutating_database(
    tmp_path: Path, unsupported_version: int
):
    db_path = tmp_path / "data" / "newsroom.db"
    db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_version VALUES (?, '2026-10-06T00:00:00+00:00')",
            (unsupported_version,),
        )
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('preserve-me')")
        connection.commit()

    before = db_path.read_bytes()

    with pytest.raises(ValueError, match=r"schema version.*supports versions 1 through 4"):
        Database(db_path).initialize("2026-10-06T01:00:00+00:00")

    assert db_path.read_bytes() == before
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()
    assert not (db_path.parent / "backups").exists()
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "preserve-me"
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == unsupported_version


def test_init_rejects_future_wal_schema_without_mutating_database_or_sidecars(tmp_path: Path):
    db_path = tmp_path / "data" / "newsroom.db"
    db_path.parent.mkdir(parents=True)
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        writer.execute("INSERT INTO schema_version VALUES (5, 'future')")
        writer.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        writer.execute("INSERT INTO sentinel VALUES ('preserve-wal')")
        writer.commit()
        files = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
        assert all(path.is_file() for path in files)
        before = {path: path.read_bytes() for path in files}

        with pytest.raises(ValueError, match=r"schema version 5.*supports versions 1 through 4"):
            Database(db_path).initialize("2026-10-06T01:00:00+00:00")

        assert {path: path.read_bytes() for path in files} == before
        assert not (db_path.parent / "backups").exists()
        assert writer.execute("SELECT value FROM sentinel").fetchone()[0] == "preserve-wal"
    finally:
        writer.close()


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


def build_v3_database(tmp_path: Path) -> Path:
    db_path = tmp_path / "data" / "newsroom.db"
    db_path.parent.mkdir(parents=True)
    # Reproduce the v3 schema without either v4 column.
    schema = SCHEMA.replace("  owning_batch_id TEXT REFERENCES batches(batch_id),\n", "")
    schema = schema.replace("  workspace_version INTEGER NOT NULL DEFAULT 0,\n", "")
    with sqlite3.connect(db_path) as connection:
        connection.executescript(schema)
        connection.execute(
            "INSERT INTO schema_version VALUES (3, '2026-10-04T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO batches(batch_id,source_filename,source_sha256,status,created_at,updated_at) "
            "VALUES ('batch-legacy','legacy.csv','legacy-hash','PREPARING','old','old')"
        )
    return db_path


def test_v3_upgrade_adds_workspace_columns_and_backup(tmp_path: Path):
    db_path = build_v3_database(tmp_path)
    database = Database(db_path)
    candidate = add_candidate(Newsroom(tmp_path))
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO articles(article_id,candidate_id,event_id,status,event_date,core_fact,"
            "topic,subtopic,company,product,event_type,keywords,primary_source,secondary_sources,"
            "created_at,updated_at) "
            "SELECT 'art-legacy',candidate_id,'event-legacy','SELECTED',event_date,core_fact,"
            "topic,subtopic,company,product,event_type,keywords,primary_source,secondary_sources,"
            "created_at,updated_at FROM candidates WHERE candidate_id=?",
            (candidate["candidate_id"],),
        )

    database.initialize("2026-10-05T00:00:00+00:00")

    assert "owning_batch_id" in {row["name"] for row in database.all("PRAGMA table_info(articles)")}
    assert "workspace_version" in {row["name"] for row in database.all("PRAGMA table_info(batches)")}
    assert database.one("SELECT workspace_version FROM batches")["workspace_version"] == 0
    legacy_article = database.one("SELECT * FROM articles WHERE article_id='art-legacy'")
    assert legacy_article["owning_batch_id"] is None
    assert legacy_article["candidate_id"] == candidate["candidate_id"]
    assert legacy_article["core_fact"] == "OpenAI released Codex Desktop on 2026-09-28."
    assert database.one("SELECT MAX(version) AS version FROM schema_version")["version"] == 4
    backups = list((db_path.parent / "backups").glob("newsroom-v3-*.db"))
    assert len(backups) == 1
    backup = Database(backups[0])
    assert backup.one("SELECT MAX(version) AS version FROM schema_version")["version"] == 3
    assert "owning_batch_id" not in {row["name"] for row in backup.all("PRAGMA table_info(articles)")}
    original_backup = backups[0].read_bytes()

    database.initialize("2026-10-06T00:00:00+00:00")

    assert list((db_path.parent / "backups").glob("*.db")) == backups
    assert backups[0].read_bytes() == original_backup
    assert database.one("SELECT COUNT(*) AS count FROM batches")["count"] == 1


def test_manual_article_has_no_owning_batch(newsroom: Newsroom):
    candidate = add_candidate(newsroom)
    newsroom.check_duplicate(candidate["candidate_id"])
    newsroom.select_candidate(candidate["candidate_id"], "editor", "selected manually")

    article = newsroom.create_brief(candidate["candidate_id"])

    assert article["owning_batch_id"] is None
