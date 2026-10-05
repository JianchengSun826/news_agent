# Claude Batch Newsroom Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable batch workflow that imports a reviewed CSV, automatically processes deterministic `NEW` candidates, exchanges one JSON file with each of two manual Claude conversations, persists image candidates, and routes fact-check results without using model APIs.

**Architecture:** Add strict Pydantic interchange models and a focused `BatchWorkflow` service around the existing `Newsroom` and SQLite database. Schema version 3 stores batches, immutable Claude requests, batch items, image candidates, fingerprints, fact-check outcomes, and artifact versions while retaining all single-article commands. Typer exposes the batch operations; prompts remain versioned Markdown files and the offline demo synthesizes Claude result fixtures.

**Tech Stack:** Python 3.12+, SQLite, Pydantic v2, Typer, pytest, standard-library JSON/CSV/hash/file operations.

**Spec:** `docs/superpowers/specs/2026-10-05-claude-batch-workflow-design.md`

## Global Constraints

- Gemini is used only before CSV creation; all post-import model work is performed manually in Claude App.
- Do not add Claude/Gemini APIs, paid API keys, network calls, image generation, web UI, Docker, Redis, queues, schedulers, or cloud storage.
- SQLite remains the canonical state store; Markdown and JSON are artifacts and interchange formats.
- Use Pydantic v2 with `extra="forbid"` for every Claude request/result boundary.
- Keep SQL parameterized and multi-record state changes transactional.
- Preserve existing single-article CLI behavior and existing version 2 data.
- Never modify `docs/archive/WORKFLOW_PHASE1_ORIGINAL.md`.
- Write a failing test before each executable behavior change.

## Review Focus

- A CSV already imported by the old workflow must reuse its candidate rows and exclude an already approved article instead of duplicating either record; Task 3 adds this regression test.
- Two different files with the same CSV rows but different byte hashes must still reuse candidate fingerprints; Task 3 tests candidate reuse independently from batch hash idempotency.
- A valid result JSON containing one unexpected or missing `article_id` must cause zero content writes; Task 4 tests exact membership and rollback.
- A second fact-check round must not accept a result from the first request ID; Task 5 tests request history and mismatch rejection.
- An image candidate with a plausible license name but no original rights page must be rejected, and a candidate from another article must not be registrable; Tasks 1 and 6 test both boundaries.

---

### Task 1: Strict Batch Interchange Models

**Files:**
- Create: `src/newsroom/batch_models.py`
- Create: `tests/test_batch_models.py`

**Interfaces:**
- Produces: `candidate_fingerprint(values: Mapping[str, Any]) -> str`
- Produces: `BatchContentRequest`, `BatchContentResult`, `BatchFactCheckRequest`, `BatchFactCheckResult`, `BatchRevisionRequest`, `BatchRevisionResult`
- Produces: `ImageCandidateResult` with rights validation and `ContentResultItem` / `FactCheckResultItem` item models

- [ ] **Step 1: Write failing fingerprint and strict-schema tests**

```python
def test_candidate_fingerprint_normalizes_case_whitespace_and_trailing_slash():
    first = candidate_values(company=" OpenAI ", primary_source="https://openai.com/news/")
    second = candidate_values(company="openai", primary_source="https://openai.com/news")
    assert candidate_fingerprint(first) == candidate_fingerprint(second)


def test_content_result_forbids_unknown_fields():
    payload = valid_content_result()
    payload["items"][0]["surprise"] = True
    with pytest.raises(ValidationError, match="surprise"):
        BatchContentResult.model_validate(payload)
```

- [ ] **Step 2: Run the new model tests and verify failure**

Run: `uv run pytest tests/test_batch_models.py -q`

Expected: collection fails because `newsroom.batch_models` does not exist.

- [ ] **Step 3: Implement normalized fingerprints and common base models**

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def candidate_fingerprint(values: Mapping[str, Any]) -> str:
    canonical = {
        "event_date": str(values["event_date"]),
        "company": " ".join(str(values["company"]).casefold().split()),
        "product": " ".join(str(values["product"]).casefold().split()),
        "event_type": " ".join(str(values["event_type"]).casefold().split()),
        "core_fact": " ".join(str(values["core_fact"]).casefold().split()),
        "primary_source": str(values["primary_source"]).rstrip("/"),
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Implement content, fact-check, revision, and image candidate models**

Use `Literal[1]` for `schema_version`, `Literal["PASS", "FAIL"]` for outcomes, `HttpUrl` for URLs, `Field(min_length=20)` for Markdown, `Field(max_length=3)` for image candidate lists, and a slug pattern of `^[a-z0-9]+(?:-[a-z0-9]+)*$`.

The image validator must require `rights_url` and `rights_statement` for every candidate, require `creator` plus `license_name` for `OPEN_LICENSE`, and require `attribution_text` when `attribution_required` is true.

- [ ] **Step 5: Add rights and membership-shape tests**

```python
def test_image_candidate_requires_original_rights_page():
    payload = valid_image_candidate()
    payload["rights_url"] = None
    with pytest.raises(ValidationError, match="rights_url"):
        ImageCandidateResult.model_validate(payload)


def test_open_license_requires_creator_and_license_name():
    payload = valid_image_candidate(rights_basis="OPEN_LICENSE", creator=None)
    with pytest.raises(ValidationError, match="creator"):
        ImageCandidateResult.model_validate(payload)
```

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/test_batch_models.py -q`

Expected: all tests pass.

```bash
git add src/newsroom/batch_models.py tests/test_batch_models.py
git commit -m "feat: add strict batch interchange models"
```

### Task 2: Schema Version 3 and Safe Migration

**Files:**
- Modify: `src/newsroom/db.py`
- Modify: `tests/test_domain.py`

**Interfaces:**
- Consumes: `candidate_fingerprint(values)` from Task 1
- Produces: version 3 columns and tables `batches`, `batch_requests`, `batch_items`, and `image_candidates`
- Produces: `Database.initialize(applied_at: str) -> None` with a one-time pre-migration backup for existing version 1/2 databases

- [ ] **Step 1: Add a failing version 2 migration test**

```python
def test_init_migrates_v2_to_v3_and_creates_backup(tmp_path: Path):
    db_path = build_v2_database(tmp_path)
    Database(db_path).initialize("2026-10-05T00:00:00+00:00")
    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        candidate_columns = {row[1] for row in connection.execute("PRAGMA table_info(candidates)")}
        article_columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
        image_columns = {row[1] for row in connection.execute("PRAGMA table_info(images)")}
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version ORDER BY version")]
    assert {"batches", "batch_requests", "batch_items", "image_candidates"} <= tables
    assert "candidate_fingerprint" in candidate_columns
    assert {"fact_check_outcome", "revision_number"} <= article_columns
    assert {"image_candidate_id", "rights_basis", "rights_statement"} <= image_columns
    assert versions == [1, 2, 3]
    assert len(list((tmp_path / "data" / "backups").glob("newsroom-v2-*.db"))) == 1
```

- [ ] **Step 2: Run the migration test and verify failure**

Run: `uv run pytest tests/test_domain.py::test_init_migrates_v2_to_v3_and_creates_backup -q`

Expected: FAIL because version 3 tables and columns are absent.

- [ ] **Step 3: Implement additive schema and migration**

Add version 3 definitions to `SCHEMA`, additive `ALTER TABLE` checks for existing databases, `images.image_candidate_id`, `images.rights_basis`, `images.rights_statement`, indexes on batch and article foreign keys, and backfill fingerprints inside the migration transaction. Before opening a write transaction on a version 1/2 database, copy it with `shutil.copy2` to `data/backups/newsroom-v{version}-{UTC_TIMESTAMP}.db`.

`batch_requests` must contain immutable `request_id`, `batch_id`, `request_type`, `revision_number`, request/result paths and hash, status, and timestamps. `batches.content_request_id` and `batches.fact_check_request_id` point to the latest request.

- [ ] **Step 4: Add collision and repeated-init tests**

```python
def test_migration_reports_fingerprint_collisions_without_merging(tmp_path: Path):
    db_path = build_v2_database_with_duplicate_candidates(tmp_path)
    with pytest.raises(ValueError, match="candidate fingerprint collision"):
        Database(db_path).initialize("2026-10-05T00:00:00+00:00")
    assert len(list((tmp_path / "data" / "backups").glob("*.db"))) == 1


def test_version_3_init_is_idempotent_and_does_not_create_extra_backup(tmp_path: Path):
    room = Newsroom(tmp_path)
    room.init()
    room.init()
    assert list((tmp_path / "data" / "backups").glob("*.db")) == []
```

- [ ] **Step 5: Run migration and existing tests, then commit**

Run: `uv run pytest tests/test_domain.py -q`

Expected: all domain tests pass with version assertions updated to `[1, 2, 3]`.

```bash
git add src/newsroom/db.py tests/test_domain.py
git commit -m "feat: migrate newsroom database to batch schema"
```

### Task 3: Batch Preparation, Candidate Reuse, and Automatic Deduplication

**Files:**
- Create: `src/newsroom/batch.py`
- Modify: `src/newsroom/service.py`
- Create: `tests/test_batch.py`

**Interfaces:**
- Produces: `BatchWorkflow(room: Newsroom)`
- Produces: `prepare(csv_path: Path) -> dict[str, Any]`
- Produces: `status(batch_id: str) -> dict[str, Any]`
- Produces: `resolve(batch_id: str, candidate_id: str, decision: str, actor: str, reason: str) -> dict[str, Any]`
- Produces: `Newsroom.find_candidate_by_fingerprint(fingerprint: str) -> dict[str, Any] | None`

- [ ] **Step 1: Write failing prepare tests for NEW, non-NEW, and old-workflow reuse**

```python
def test_prepare_auto_selects_new_and_pauses_non_new(newsroom: Newsroom, tmp_path: Path):
    csv_path = write_batch_csv(tmp_path, [anchor_row(), duplicate_row(), unrelated_row()])
    result = BatchWorkflow(newsroom).prepare(csv_path)
    status = BatchWorkflow(newsroom).status(result["batch_id"])
    assert status["counts"]["READY_FOR_CONTENT"] == 2
    assert status["counts"]["WAITING_DUPLICATE"] == 1


def test_prepare_reuses_old_candidate_and_completed_article(newsroom: Newsroom, tmp_path: Path):
    candidate, article = prepare_approved_old_workflow_article(newsroom, tmp_path)
    csv_path = write_batch_csv(tmp_path, [row_for(candidate)])
    result = BatchWorkflow(newsroom).prepare(csv_path)
    item = BatchWorkflow(newsroom).status(result["batch_id"])["items"][0]
    assert item["candidate_id"] == candidate["candidate_id"]
    assert item["article_id"] == article["article_id"]
    assert item["item_status"] == "REUSED_COMPLETE"
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 1
```

- [ ] **Step 2: Run prepare tests and verify failure**

Run: `uv run pytest tests/test_batch.py -k prepare -q`

Expected: collection fails because `BatchWorkflow` does not exist.

- [ ] **Step 3: Refactor candidate insertion to persist fingerprints**

Update `Newsroom.add_candidate` to compute and store `candidate_fingerprint`, and add a parameterized lookup that raises `WorkflowError` if more than one row has the same fingerprint. Keep the public `add_candidate(**values)` signature backward compatible.

- [ ] **Step 4: Implement complete CSV pre-validation and batch idempotency**

Read with `utf-8-sig`, validate every row into `CandidateInput`, and reject the file before mutation if any row is invalid. Hash the original bytes. If a batch with that SHA-256 exists, return its current summary instead of creating another batch.

- [ ] **Step 5: Implement deterministic processing and content request generation**

For each row, reuse or add the candidate, run or reuse its duplicate result, auto-select only `NEW`, create an article if absent, and insert a batch item. Generate one `BatchContentRequest` using `prompts/claude_batch_content.md`; write JSON with `ensure_ascii=False`, indentation, and an immutable `req-...` ID to `output/batches/BATCH_ID/`.

- [ ] **Step 6: Test byte-different CSV reuse and explicit duplicate resolution**

```python
def test_byte_different_csv_reuses_candidates_but_creates_new_batch(newsroom, tmp_path):
    first, second = write_equivalent_csv_files_with_different_bytes(tmp_path)
    one = BatchWorkflow(newsroom).prepare(first)
    two = BatchWorkflow(newsroom).prepare(second)
    assert one["batch_id"] != two["batch_id"]
    assert newsroom.db.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 1


def test_resolve_requires_override_for_possible_duplicate(newsroom, tmp_path):
    batch_id, candidate_id = prepare_possible_duplicate(newsroom, tmp_path)
    with pytest.raises(WorkflowError, match="duplicate override"):
        BatchWorkflow(newsroom).resolve(batch_id, candidate_id, "include", "editor", "include it")
```

- [ ] **Step 7: Run batch tests and commit**

Run: `uv run pytest tests/test_batch.py -k 'prepare or resolve' -q`

Expected: all selected tests pass.

```bash
git add src/newsroom/batch.py src/newsroom/service.py tests/test_batch.py prompts/claude_batch_content.md
git commit -m "feat: prepare durable Claude content batches"
```

### Task 4: Atomic Content Result Import and Fact-check Request

**Files:**
- Modify: `src/newsroom/batch.py`
- Modify: `tests/test_batch.py`
- Create: `prompts/claude_batch_fact_check.md`

**Interfaces:**
- Produces: `import_content(batch_id: str, result_path: Path) -> dict[str, Any]`
- Produces: `_write_fact_check_request(connection, batch_id: str, article_ids: list[str], revision_number: int) -> Path`

- [ ] **Step 1: Write failing happy-path and rollback tests**

```python
def test_content_import_registers_every_item_and_creates_fact_check_request(prepared_batch, tmp_path):
    result_path = write_valid_content_result(prepared_batch, tmp_path)
    result = prepared_batch.workflow.import_content(prepared_batch.batch_id, result_path)
    assert result["imported"] == len(prepared_batch.article_ids)
    for article_id in prepared_batch.article_ids:
        article = prepared_batch.room.get_article(article_id)
        assert article["status"] == "DRAFTED"
        assert Path(prepared_batch.room.root, article["draft_path"]).name == "article-v1.md"
    assert Path(result["fact_check_request_path"]).exists()


def test_one_missing_article_rolls_back_entire_content_import(prepared_batch, tmp_path):
    result_path = write_valid_content_result(prepared_batch, tmp_path, omit_last=True)
    with pytest.raises(WorkflowError, match="missing article IDs"):
        prepared_batch.workflow.import_content(prepared_batch.batch_id, result_path)
    assert all(prepared_batch.room.get_article(article_id)["status"] == "SELECTED" for article_id in prepared_batch.article_ids)
```

- [ ] **Step 2: Run content-import tests and verify failure**

Run: `uv run pytest tests/test_batch.py -k content_import -q`

Expected: FAIL because `import_content` is absent.

- [ ] **Step 3: Implement request identity, exact membership, and slug checks**

Load the immutable open `CONTENT` request row, validate `BatchContentResult`, compare `batch_id`, `request_id`, `output_file`, and exact article ID sets, then check slugs against both the result set and existing articles outside the batch.

- [ ] **Step 4: Implement staged artifact writes and one transaction**

Write every research/article Markdown file to a staging directory beneath the batch directory. Within one SQLite transaction, move them to versioned canonical paths, update articles through `RESEARCHED` and `DRAFTED`, insert transitions, insert image candidates, update batch items, and complete the request with its result hash/path. On an exception, roll back and remove files created by this attempt.

- [ ] **Step 5: Generate the fact-check request automatically**

The request embeds each canonical article, Research Brief, source URLs, and strict instructions from `prompts/claude_batch_fact_check.md`. It must require a new conversation, one `PASS`/`FAIL` per article, claim-level reporting, and no image task.

- [ ] **Step 6: Add request mismatch, duplicate slug, rights, and idempotency tests**

```python
@pytest.mark.parametrize("mutation, message", [
    ("wrong_request_id", "request_id"),
    ("extra_article", "unexpected article IDs"),
    ("duplicate_slug", "duplicate slug"),
    ("missing_rights_url", "rights_url"),
])
def test_content_import_rejects_invalid_batch_result(prepared_batch, tmp_path, mutation, message):
    path = write_content_result(prepared_batch, tmp_path, mutation=mutation)
    with pytest.raises((WorkflowError, ValidationError), match=message):
        prepared_batch.workflow.import_content(prepared_batch.batch_id, path)


def test_reimporting_identical_content_result_is_idempotent(prepared_batch, tmp_path):
    path = write_valid_content_result(prepared_batch, tmp_path)
    first = prepared_batch.workflow.import_content(prepared_batch.batch_id, path)
    second = prepared_batch.workflow.import_content(prepared_batch.batch_id, path)
    assert first["request_id"] == second["request_id"]
```

- [ ] **Step 7: Run tests and commit**

Run: `uv run pytest tests/test_batch.py -k 'content_import or content_result' -q`

Expected: all selected tests pass.

```bash
git add src/newsroom/batch.py tests/test_batch.py prompts/claude_batch_fact_check.md
git commit -m "feat: import Claude batch content atomically"
```

### Task 5: Independent Fact-check and Revision Cycles

**Files:**
- Modify: `src/newsroom/batch.py`
- Modify: `tests/test_batch.py`
- Create: `prompts/claude_batch_revision.md`

**Interfaces:**
- Produces: `import_fact_checks(batch_id: str, result_path: Path) -> dict[str, Any]`
- Produces: `create_revision_request(batch_id: str) -> Path`
- Produces: `import_revisions(batch_id: str, result_path: Path) -> dict[str, Any]`

- [ ] **Step 1: Write failing PASS/FAIL routing tests**

```python
def test_fact_check_import_routes_pass_and_fail(content_batch, tmp_path):
    path = write_fact_check_result(content_batch, tmp_path, outcomes=["PASS", "FAIL"])
    result = content_batch.workflow.import_fact_checks(content_batch.batch_id, path)
    assert result["passed"] == 1
    assert result["needs_revision"] == 1
    assert content_batch.room.get_article(content_batch.article_ids[0])["status"] == "FACT_CHECKED"
    assert content_batch.room.get_article(content_batch.article_ids[1])["status"] == "NEEDS_REVISION"
```

- [ ] **Step 2: Run fact-check tests and verify failure**

Run: `uv run pytest tests/test_batch.py -k fact_check -q`

Expected: FAIL because fact-check batch import is absent.

- [ ] **Step 3: Implement whole-document validation and transactional routing**

Require exact membership for the current immutable `FACT_CHECK` request. Save `fact-check-vN.md`, set `fact_check_outcome`, advance each article to `FACT_CHECKED` or `NEEDS_REVISION`, and retain `FAIL` as valid batch data. Re-importing the same hash is idempotent.

- [ ] **Step 4: Implement versioned revision requests and imports**

Generate a revision request only for `NEEDS_REVISION` items, including their article, Research Brief, and blocking issues. Revision results contain revised `research_brief_md`, title, slug, and article Markdown. Import to `research-brief-v{N}.md` and `article-v{N}.md`, increment `revision_number`, return the item to `DRAFTED`, clear the current outcome, and automatically create a new immutable fact-check request.

- [ ] **Step 5: Add stale-request and version-preservation tests**

```python
def test_second_round_rejects_first_round_fact_check_result(revised_batch, tmp_path):
    stale = write_first_round_fact_check_result(revised_batch, tmp_path)
    with pytest.raises(WorkflowError, match="request_id"):
        revised_batch.workflow.import_fact_checks(revised_batch.batch_id, stale)


def test_revision_preserves_v1_and_writes_v2(revised_batch):
    article_id = revised_batch.article_ids[0]
    article_dir = revised_batch.room.root / "articles" / article_id
    assert (article_dir / "article-v1.md").exists()
    assert (article_dir / "article-v2.md").exists()
    assert revised_batch.room.get_article(article_id)["revision_number"] == 2
```

- [ ] **Step 6: Run tests and commit**

Run: `uv run pytest tests/test_batch.py -k 'fact_check or revision' -q`

Expected: all selected tests pass.

```bash
git add src/newsroom/batch.py tests/test_batch.py prompts/claude_batch_revision.md
git commit -m "feat: add batch fact-check and revision cycles"
```

### Task 6: Image Candidate Review and Rights-confirmed Registration

**Files:**
- Modify: `src/newsroom/models.py`
- Modify: `src/newsroom/service.py`
- Modify: `src/newsroom/cli.py`
- Modify: `tests/test_domain.py`
- Create: `tests/test_batch_cli.py`

**Interfaces:**
- Produces: `Newsroom.list_image_candidates(article_id: str) -> list[dict[str, Any]]`
- Extends: `Newsroom.register_image(..., image_candidate_id: str | None = None, rights_confirmed: bool = False) -> str`
- CLI: `newsroom image candidates ARTICLE_ID`
- CLI: `newsroom image register ARTICLE_ID --candidate IMAGE_CANDIDATE_ID --file FILE --rights-confirmed`

- [ ] **Step 1: Write failing rights-confirmation and cross-article tests**

```python
def test_candidate_image_requires_explicit_rights_confirmation(fact_checked_batch, tmp_path):
    candidate = first_image_candidate(fact_checked_batch)
    image = tmp_path / "hero.jpg"
    image.write_bytes(b"jpeg")
    with pytest.raises(WorkflowError, match="rights-confirmed"):
        fact_checked_batch.room.register_image(
            candidate["article_id"], image_file=image,
            image_type="OPEN_LICENSE", image_candidate_id=candidate["image_candidate_id"],
        )


def test_candidate_image_cannot_be_registered_for_another_article(fact_checked_batch, tmp_path):
    candidate = first_image_candidate(fact_checked_batch)
    other_article = fact_checked_batch.article_ids[1]
    with pytest.raises(WorkflowError, match="different article"):
        register_candidate_image(fact_checked_batch.room, other_article, candidate, tmp_path)
```

- [ ] **Step 2: Run image candidate tests and verify failure**

Run: `uv run pytest tests/test_domain.py tests/test_batch_cli.py -k image_candidate -q`

Expected: FAIL because the interfaces do not exist.

- [ ] **Step 3: Implement candidate listing and metadata transfer**

When `image_candidate_id` is provided, load it with a parameterized query, verify its article, require `rights_confirmed`, copy landing-page/creator/license/rights/attribution metadata into `images`, mark the chosen candidate `SELECTED`, and leave all network/download work to the human. Preserve the existing manual licensed-image and legacy AI-image paths.

- [ ] **Step 4: Add Typer commands and CLI tests**

```python
def test_cli_lists_and_registers_image_candidate(batch_workspace, tmp_path):
    listed = runner.invoke(app, ["--root", str(batch_workspace.root), "image", "candidates", batch_workspace.article_id])
    assert listed.exit_code == 0
    candidate_id = json.loads(listed.output)[0]["image_candidate_id"]
    registered = runner.invoke(app, [
        "--root", str(batch_workspace.root), "image", "register", batch_workspace.article_id,
        "--candidate", candidate_id, "--file", str(batch_workspace.image_file), "--rights-confirmed",
    ])
    assert registered.exit_code == 0, registered.output
```

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_domain.py tests/test_batch_cli.py -q`

Expected: all selected tests pass.

```bash
git add src/newsroom/models.py src/newsroom/service.py src/newsroom/cli.py tests/test_domain.py tests/test_batch_cli.py
git commit -m "feat: register rights-confirmed image candidates"
```

### Task 7: Batch CLI Commands and Actionable Status Output

**Files:**
- Modify: `src/newsroom/cli.py`
- Modify: `tests/test_batch_cli.py`

**Interfaces:**
- CLI group: `batch_app`
- Commands: `prepare`, `status`, `resolve`, `content-import`, `fact-check-import`, `revision-request`, `revision-import`

- [ ] **Step 1: Write failing command exposure and end-to-end CLI tests**

```python
def test_typer_exposes_batch_commands():
    for command in ("prepare", "status", "resolve", "content-import", "fact-check-import", "revision-request", "revision-import"):
        result = runner.invoke(app, ["batch", command, "--help"])
        assert result.exit_code == 0, result.output


def test_cli_prepare_outputs_one_claude_request(batch_csv, tmp_path):
    runner.invoke(app, ["--root", str(tmp_path), "init"])
    result = runner.invoke(app, ["--root", str(tmp_path), "batch", "prepare", str(batch_csv)])
    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert Path(payload["content_request_path"]).exists()
    assert payload["claude_instruction"] == "严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。"
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run: `uv run pytest tests/test_batch_cli.py -q`

Expected: FAIL because the batch Typer group is absent.

- [ ] **Step 3: Implement thin Typer adapters**

Instantiate `BatchWorkflow(_room(ctx))`, keep business logic outside CLI functions, use `_run` for consistent exit code 2, and print JSON with the request path plus the one-line Claude instruction after prepare/content-import/revision-import.

- [ ] **Step 4: Test errors and status readability**

Add tests that invalid JSON prints an exact field path, missing batches return `batch not found`, and `batch status` lists `WAITING_DUPLICATE`, `NEEDS_REVISION`, and image-pending article IDs.

- [ ] **Step 5: Run all CLI tests and commit**

Run: `uv run pytest tests/test_cli.py tests/test_batch_cli.py -q`

Expected: all CLI tests pass.

```bash
git add src/newsroom/cli.py tests/test_batch_cli.py
git commit -m "feat: expose Claude batch workflow commands"
```

### Task 8: Publication Compatibility, Prompts, Documentation, and Offline Demo

**Files:**
- Modify: `src/newsroom/service.py`
- Modify: `tests/test_domain.py`
- Modify: `scripts/demo.sh`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/DATA_MODEL.md`
- Modify: `docs/ROADMAP.md`
- Modify: `docs/WORKFLOW.md`
- Modify: `.gitignore` only if the demo introduces a new generated path outside existing ignored directories

**Interfaces:**
- Publication package metadata includes `revision_number` and selected image candidate provenance.
- Demo exercises prepare → content import → fact-check import → image register → review → approve → package → publish with local fixtures only.

- [ ] **Step 1: Write a failing publication regression test**

```python
def test_batch_article_publication_package_uses_current_version_and_rights(batch_published_article):
    package = batch_published_article.package
    metadata = json.loads((package / "metadata.json").read_text())
    rights = json.loads((package / "image-license.json").read_text())
    assert metadata["revision_number"] == batch_published_article.expected_revision
    assert rights["image_candidate_id"] == batch_published_article.image_candidate_id
    assert (package / "article.md").read_text() == batch_published_article.current_article_md
```

- [ ] **Step 2: Run the regression test and verify failure**

Run: `uv run pytest tests/test_domain.py -k batch_article_publication -q`

Expected: FAIL because candidate provenance/current revision metadata is absent.

- [ ] **Step 3: Update package generation and the offline demo**

Preserve the publication package filenames while copying the latest version paths. Rewrite `scripts/demo.sh` to call `batch prepare`, synthesize schema-valid result JSON from the generated request with an inline Python block, import both Claude results, register fixture images with `--rights-confirmed`, then complete publication.

- [ ] **Step 4: Update all live documentation**

Document Phase 1.1 provider boundaries, commands, JSON files, two one-message Claude windows, PASS/FAIL behavior, image rights verification, remaining human steps, and migration. Do not alter `docs/archive/WORKFLOW_PHASE1_ORIGINAL.md`.

- [ ] **Step 5: Run documentation and demo checks**

Run:

```bash
git diff --check
bash scripts/demo.sh
```

Expected: no whitespace errors; demo prints batch counts and at least one published article without network access.

- [ ] **Step 6: Commit**

```bash
git add src/newsroom/service.py tests/test_domain.py scripts/demo.sh docs/ARCHITECTURE.md docs/DATA_MODEL.md docs/ROADMAP.md docs/WORKFLOW.md
git commit -m "docs: make Claude batches the default workflow"
```

### Task 9: Full Verification and Existing CSV Acceptance

**Files:**
- Verify only; fix files from the owning task if a check fails

**Interfaces:**
- Consumes the entire implementation and the user's existing `Tech Newsroom Candidates - read-to-import.csv`.
- Produces test, demo, migration, and real-data acceptance evidence.

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Run the isolated offline demo**

Run: `bash scripts/demo.sh`

Expected: exit code 0, no network/API calls, published demo output present.

- [ ] **Step 3: Back up and initialize the real workspace**

Run:

```bash
uv run newsroom init
```

Expected: version 2 database is backed up once and migrated to version 3 without changing existing article content.

- [ ] **Step 4: Prepare the user's existing CSV**

Run:

```bash
uv run newsroom batch prepare \
  "$HOME/Downloads/Tech Newsroom Candidates - read-to-import.csv"
```

Expected: the existing six candidates are reused, no seventh candidate is created, the approved Dots article is `REUSED_COMPLETE`, and eligible remaining candidates appear in one content request.

- [ ] **Step 5: Verify repository cleanliness and inspect changes**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -10
```

Expected: only intentional source, test, prompt, docs, and plan changes are tracked; local databases, batch output, research, article, and image artifacts remain untracked or ignored and are not staged.
