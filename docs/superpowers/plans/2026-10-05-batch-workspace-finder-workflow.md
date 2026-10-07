# Batch Workspace and Finder Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate every active batch artifact beneath one Finder-visible workspace, add batch-only lifecycle operations and double-click macOS actions, safely migrate existing data, and retire supported single-article content commands.

**Architecture:** SQLite remains authoritative. `BatchWorkspace` derives canonical paths and regenerates human and JSON dashboards; `BatchWorkflow` owns Claude exchange and versioned content; `BatchReleaseWorkflow` owns atomic image, review, approval, package, and publication operations; `WorkspaceMigrator` performs manifest-driven backup and migration; `MacOSBatchController` adapts those services to Finder-launched scripts and `osascript` dialogs.

**Tech Stack:** Python 3.12+, standard library (`pathlib`, `sqlite3`, `hashlib`, `tarfile`, `subprocess`, `shutil`, `json`), Pydantic v2, Typer, pytest, macOS `open` and `osascript`.

**Spec:** `docs/superpowers/specs/2026-10-05-batch-workspace-finder-workflow-design.md`

## Global Constraints

- Remain in Phase 1.1: local, human-driven, and free of paid APIs.
- Support macOS only for Finder actions; core services remain deterministic Python.
- Do not add a web UI, local HTTP service, scheduler, queue, Docker, Redis, cloud database, or automatic image downloader.
- SQLite is canonical; `README.md` and `batch-state.json` are rebuildable views.
- Every post-writing gate advances the complete included batch together; `EXCLUDED` and `REUSED_COMPLETE` items do not participate.
- Preserve immutable Claude request/result identity, hashes, content versions, image provenance, and audit transitions.
- Use four-space indentation, public type hints, parameterized SQL, UTC ISO-8601 timestamps, lowercase ASCII IDs, and relative database paths.
- Use TDD for every behavior change. Never execute migration against real data before copied-data rehearsal passes.
- Keep generated workspaces, databases, backups, downloaded images, and publication packages out of Git.

## Review Focus

1. **Repository paths containing spaces or non-ASCII characters:** generated `.command` files must locate the repository and pass paths without shell expansion or corruption; Task 2 pins this with a relocated Unicode-path test.
2. **Mixed batch membership:** `EXCLUDED` and `REUSED_COMPLETE` items must not block gates or receive duplicate packages, while every active item must participate; Tasks 4 and 5 cover mixed fixtures.
3. **Stale or wrong Claude result files:** inbox discovery must never import a valid-looking result for another request, revision, or batch; Tasks 3 and 7 cover identity rejection before mutation.
4. **Interrupted image or migration work:** cancellation and exceptions must preserve resumable drafts or verified backups without partial database state; Tasks 4 and 9 exercise interruption points.
5. **Missing original CSV or unclaimed legacy files:** migration must preserve source metadata and archive unclaimed content without silently inventing or deleting source data; Tasks 8 and 9 cover both cases.

---

### Task 1: Schema v4 and Canonical Batch Ownership

**Files:**
- Modify: `src/newsroom/db.py`
- Modify: `src/newsroom/service.py`
- Modify: `src/newsroom/batch.py`
- Modify: `docs/DATA_MODEL.md`
- Test: `tests/test_domain.py`
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: existing `Database.initialize(applied_at)`, `Newsroom.create_brief(candidate_id)`, and `BatchWorkflow.prepare/resolve`.
- Produces: `Newsroom.create_brief(candidate_id: str, owning_batch_id: str | None = None) -> dict[str, Any]`; `articles.owning_batch_id`; `batches.workspace_version`.

- [ ] **Step 1: Write failing schema-upgrade and ownership tests**

```python
def test_v3_upgrade_adds_workspace_columns_and_backup(tmp_path: Path):
    db_path = build_v3_database(tmp_path)
    Database(db_path).initialize("2026-10-05T00:00:00+00:00")
    article_columns = Database(db_path).all("PRAGMA table_info(articles)")
    batch_columns = Database(db_path).all("PRAGMA table_info(batches)")
    assert "owning_batch_id" in {row["name"] for row in article_columns}
    assert "workspace_version" in {row["name"] for row in batch_columns}
    assert list((tmp_path / "backups").glob("newsroom-v3-*.db"))


def test_batch_created_article_records_owning_batch(newsroom: Newsroom, tmp_path: Path):
    prepared = BatchWorkflow(newsroom).prepare(write_batch_csv(tmp_path / "batch.csv", [candidate_row()]))
    article_id = prepared["items"][0]["article_id"]
    article = newsroom.db.one("SELECT owning_batch_id FROM articles WHERE article_id=?", (article_id,))
    assert article["owning_batch_id"] == prepared["batch_id"]
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_domain.py::test_v3_upgrade_adds_workspace_columns_and_backup tests/test_batch.py::test_batch_created_article_records_owning_batch -q`

Expected: FAIL because both columns and the new argument are absent.

- [ ] **Step 3: Implement schema v4 and ownership propagation**

Add to `SCHEMA`:

```sql
owning_batch_id TEXT REFERENCES batches(batch_id)
```

on `articles`, and:

```sql
workspace_version INTEGER NOT NULL DEFAULT 0
```

on `batches`. Upgrade version 3 with the existing timestamped backup mechanism, add both columns idempotently, then insert schema version 4. Change `create_brief` to insert `owning_batch_id`, and pass the current batch ID from both `prepare` and `resolve(include)`.

- [ ] **Step 4: Run focused and full tests**

Run: `uv run pytest tests/test_domain.py tests/test_batch.py -q`

Expected: PASS.

- [ ] **Step 5: Update the data model and commit**

Document schema version 4, immutable ownership, nullable legacy ownership, and workspace version.

```bash
git add src/newsroom/db.py src/newsroom/service.py src/newsroom/batch.py tests/test_domain.py tests/test_batch.py docs/DATA_MODEL.md
git commit -m "feat: record canonical batch ownership"
```

### Task 2: Workspace Paths, Dashboards, and Finder Scripts

**Files:**
- Create: `src/newsroom/workspace.py`
- Create: `tests/test_workspace.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `Newsroom.root`, `Newsroom.db`, batch/article/request/image rows.
- Produces: `BatchWorkspace(room: Newsroom)`; `batch_root(batch_id: str) -> Path`; `article_root(batch_id: str, article_id: str) -> Path`; `ensure_layout(batch_id: str) -> Path`; `refresh(batch_id: str) -> dict[str, Any]`; `install_root_actions() -> Path`.

- [ ] **Step 1: Write failing layout and relocation tests**

```python
def test_workspace_generates_dashboard_and_relocatable_actions(prepared_batch, tmp_path: Path):
    workspace = BatchWorkspace(prepared_batch.room)
    root = workspace.ensure_layout(prepared_batch.batch_id)
    state = workspace.refresh(prepared_batch.batch_id)
    assert root == tmp_path / "workspace" / "batches" / prepared_batch.batch_id
    assert state["batch_id"] == prepared_batch.batch_id
    assert (root / "README.md").is_file()
    action = root / "actions" / "02-导入Claude返回文件.command"
    assert action.stat().st_mode & stat.S_IXUSR
    text = action.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "pyproject.toml" in text


def test_action_bootstrap_survives_unicode_repository_path(tmp_path: Path):
    root = tmp_path / "新闻 编辑室"
    root.mkdir()
    action = render_action_script("batch-abc123", "import-result")
    assert '"$repo_root"' in action
    assert '"batch-abc123"' in action
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_workspace.py -q`

Expected: ERROR because `newsroom.workspace` does not exist.

- [ ] **Step 3: Implement the focused workspace module**

Define immutable paths:

```python
@dataclass(frozen=True)
class BatchWorkspacePaths:
    root: Path

    @property
    def requests(self) -> Path: return self.root / "claude" / "requests"
    @property
    def inbox(self) -> Path: return self.root / "claude" / "inbox"
    @property
    def archive(self) -> Path: return self.root / "claude" / "archive"
    @property
    def articles(self) -> Path: return self.root / "articles"
    @property
    def publish(self) -> Path: return self.root / "publish"
```

`refresh` must query canonical state, write deterministic UTF-8 Markdown with relative links, and atomically replace `batch-state.json`. Generate scripts with fixed action names and this bootstrap contract:

```bash
#!/bin/zsh
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
search_dir="$script_dir"
while [[ "$search_dir" != "/" && ! -f "$search_dir/pyproject.toml" ]]; do
  search_dir="$(dirname "$search_dir")"
done
exec uv run --project "$search_dir" newsroom --root "$search_dir" macos-action "BATCH_ID" "ACTION"
```

The generated form must shell-quote fixed ID/action literals and must never interpolate database content.

- [ ] **Step 4: Run tests and inspect generated files**

Run: `uv run pytest tests/test_workspace.py -q`

Expected: PASS. Also run `zsh -n` against every generated `.command` fixture.

- [ ] **Step 5: Ignore runtime workspace and commit**

Add `workspace/` to `.gitignore`.

```bash
git add src/newsroom/workspace.py tests/test_workspace.py .gitignore
git commit -m "feat: generate batch Finder workspaces"
```

### Task 3: Store New Batch and Claude Artifacts in the Workspace

**Files:**
- Modify: `src/newsroom/batch.py`
- Modify: `src/newsroom/workspace.py`
- Modify: `tests/test_batch.py`
- Modify: `tests/test_batch_cli.py`

**Interfaces:**
- Consumes: `BatchWorkspace.ensure_layout`, `BatchWorkspacePaths`.
- Produces: new batches whose CSV, requests, results, research, drafts, fact-checks, and image candidate JSON all live beneath `workspace/batches/BATCH_ID/`.

- [ ] **Step 1: Write failing canonical-path tests**

```python
def test_prepare_copies_csv_and_writes_human_named_content_request(newsroom: Newsroom, tmp_path: Path):
    source = write_batch_csv(tmp_path / "reviewed.csv", [candidate_row()])
    result = BatchWorkflow(newsroom).prepare(source)
    root = tmp_path / "workspace" / "batches" / result["batch_id"]
    assert Path(result["content_request_path"]) == root / "claude" / "requests" / "content-request.json"
    assert (root / "input" / "candidates.csv").read_bytes() == source.read_bytes()


def test_content_and_fact_check_import_use_article_workspace(prepared_batch, tmp_path: Path):
    imported = prepared_batch.workflow.import_content(
        prepared_batch.batch_id,
        write_content_result(prepared_batch, tmp_path / "content.json"),
    )
    article = prepared_batch.room.get_article(prepared_batch.article_ids[0])
    prefix = f"workspace/batches/{prepared_batch.batch_id}/articles/{prepared_batch.article_ids[0]}"
    assert article["research_brief_path"] == f"{prefix}/research-brief-v1.md"
    assert article["draft_path"] == f"{prefix}/article-v1.md"
    assert Path(imported["fact_check_request_path"]).name == "fact-check-request-v1.json"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest tests/test_batch.py -k 'canonical_path or article_workspace or copies_csv' -q`

Expected: FAIL with current `output/`, `research/`, and `articles/` paths.

- [ ] **Step 3: Replace path construction with `BatchWorkspace` paths**

Use these exact naming rules:

```text
claude/requests/content-request.json
claude/requests/fact-check-request-vN.json
claude/requests/revision-request-vN.json
claude/archive/content-result.json
claude/archive/fact-check-result-vN.json
claude/archive/revision-result-vN.json
articles/ARTICLE_ID/research-brief-vN.md
articles/ARTICLE_ID/article-vN.md
articles/ARTICLE_ID/fact-check-vN.md
articles/ARTICLE_ID/image-candidates.json
```

Keep request IDs inside JSON and SQLite. Use staging inside the batch root. After every successful state change, call `BatchWorkspace.refresh(batch_id)`. Copy the source CSV only after its already-computed hash matches `source_sha256`.

- [ ] **Step 4: Add stale-result and idempotency coverage**

Add a test that places another batch's valid content result in the current inbox and asserts `WorkflowError` with zero article/path mutations. Retain same-hash re-import behavior.

- [ ] **Step 5: Run batch tests and commit**

Run: `uv run pytest tests/test_batch.py tests/test_batch_cli.py tests/test_workspace.py -q`

```bash
git add src/newsroom/batch.py src/newsroom/workspace.py tests/test_batch.py tests/test_batch_cli.py
git commit -m "feat: consolidate batch artifacts"
```

### Task 4: Atomic Batch Image Planning

**Files:**
- Modify: `src/newsroom/batch_models.py`
- Create: `src/newsroom/batch_release.py`
- Create: `tests/test_batch_release.py`
- Modify: `src/newsroom/workspace.py`

**Interfaces:**
- Consumes: fact-checked batch articles, `image_candidates`, local files, and `BatchWorkspace.article_root`.
- Produces: Pydantic `BatchImageSelection` and `BatchImagePlan`; `BatchReleaseWorkflow.save_image_draft(batch_id: str, plan: BatchImagePlan) -> Path`; `apply_image_plan(batch_id: str, plan_path: Path) -> dict[str, Any]`.

- [ ] **Step 1: Write failing gate, atomicity, and resume tests**

```python
def test_image_plan_requires_every_active_article_to_pass(content_batch, tmp_path: Path):
    import_fact_checks(content_batch, outcomes=["PASS", "FAIL"])
    with pytest.raises(WorkflowError, match="all included articles must pass"):
        BatchReleaseWorkflow(content_batch.room).apply_image_plan(
            content_batch.batch_id, tmp_path / "plan.json"
        )


def test_invalid_second_image_leaves_first_article_unchanged(all_pass_batch, tmp_path: Path):
    plan = write_image_plan(all_pass_batch, tmp_path, missing_file_for_index=1)
    with pytest.raises(WorkflowError, match="image file not found"):
        BatchReleaseWorkflow(all_pass_batch.room).apply_image_plan(all_pass_batch.batch_id, plan)
    assert all(all_pass_batch.room.get_article(article_id)["status"] == "FACT_CHECKED" for article_id in all_pass_batch.article_ids)
    assert all_pass_batch.room.db.one("SELECT COUNT(*) AS count FROM images")["count"] == 0


def test_cancelled_selection_draft_is_resumable(all_pass_batch, tmp_path: Path):
    workflow = BatchReleaseWorkflow(all_pass_batch.room)
    path = workflow.save_image_draft(all_pass_batch.batch_id, partial_image_plan(all_pass_batch))
    assert path.name == "image-selections.json"
    assert workflow.load_image_draft(all_pass_batch.batch_id).items[0].article_id == all_pass_batch.article_ids[0]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_batch_release.py -k image -q`

Expected: ERROR because models and workflow do not exist.

- [ ] **Step 3: Define strict image plan models**

```python
class BatchImageSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    article_id: str
    image_candidate_id: str
    image_file: Path
    rights_confirmed: Literal[True]


class BatchImagePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    batch_id: str
    items: list[BatchImageSelection]
```

Require exact active article membership, one selection per article, candidate ownership, unselected candidate, local regular file, and all-PASS gate before mutation.

- [ ] **Step 4: Implement staged copies and one database transaction**

Copy each selected image to a staging directory using `hero` plus the lowercase source suffix. Only after all copies succeed, insert all image rows, mark candidates selected, advance all active articles to `IMAGE_READY`, and record transitions in one transaction. Promote staged images after validation; on any exception remove staged/promoted files and roll back. Refresh the batch view after success.

- [ ] **Step 5: Run focused tests and commit**

Run: `uv run pytest tests/test_batch_release.py -k image -q`

```bash
git add src/newsroom/batch_models.py src/newsroom/batch_release.py src/newsroom/workspace.py tests/test_batch_release.py
git commit -m "feat: register images by batch"
```

### Task 5: Atomic Batch Review, Approval, Packaging, and Publication

**Files:**
- Modify: `src/newsroom/batch_models.py`
- Modify: `src/newsroom/batch_release.py`
- Modify: `tests/test_batch_release.py`
- Modify: `src/newsroom/service.py`

**Interfaces:**
- Consumes: active batch membership and current article/image paths.
- Produces: `review(batch_id: str, actor: str, reason: str) -> dict[str, Any]`; `approve(...)`; `build_packages(batch_id: str) -> Path`; `register_publications(batch_id: str, result_path: Path, actor: str) -> dict[str, Any]`.

- [ ] **Step 1: Write failing mixed-membership gate tests**

```python
def test_review_ignores_excluded_and_reused_but_requires_all_active_images(mixed_batch):
    with pytest.raises(WorkflowError, match="IMAGE_READY"):
        BatchReleaseWorkflow(mixed_batch.room).review(mixed_batch.batch_id, "editor", "checked batch")
    assert no_articles_have_status(mixed_batch.room, "REVIEWED")


def test_review_and_approval_transition_all_active_articles(atomically_ready_batch):
    workflow = BatchReleaseWorkflow(atomically_ready_batch.room)
    reviewed = workflow.review(atomically_ready_batch.batch_id, "editor", "checked all material")
    approved = workflow.approve(atomically_ready_batch.batch_id, "editor", "approved batch")
    assert reviewed["reviewed"] == approved["approved"] == len(atomically_ready_batch.article_ids)
    assert all(atomically_ready_batch.room.get_article(article_id)["status"] == "APPROVED" for article_id in atomically_ready_batch.article_ids)
```

- [ ] **Step 2: Write failing all-or-nothing package test**

```python
def test_package_failure_preserves_previous_publish_directory(approved_batch, tmp_path: Path):
    publish = approved_batch.root / "workspace" / "batches" / approved_batch.batch_id / "publish"
    publish.mkdir(parents=True)
    (publish / "sentinel.txt").write_text("old", encoding="utf-8")
    missing = approved_batch.room.root / approved_batch.room.get_article(approved_batch.article_ids[1])["draft_path"]
    missing.unlink()
    with pytest.raises(WorkflowError, match="article file not found"):
        BatchReleaseWorkflow(approved_batch.room).build_packages(approved_batch.batch_id)
    assert (publish / "sentinel.txt").read_text(encoding="utf-8") == "old"
```

- [ ] **Step 3: Run focused tests and verify RED**

Run: `uv run pytest tests/test_batch_release.py -k 'review or approval or package or publication' -q`

- [ ] **Step 4: Implement batch gates and packages**

Add strict publication mapping:

```python
class PublicationResultItem(BaseModel):
    article_id: str
    article_url: HttpUrl


class BatchPublicationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    batch_id: str
    items: list[PublicationResultItem]
```

Review and approval validate every active article before opening one transaction. Packages stage complete article, research, fact-check, hero image, metadata, license, sources, and checklist files, plus `batch-manifest.json`; atomically replace `publish/` only after validating all staged packages. Publication registration requires an exact active article set and updates every URL/status in one transaction.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_batch_release.py tests/test_domain.py -q`

```bash
git add src/newsroom/batch_models.py src/newsroom/batch_release.py src/newsroom/service.py tests/test_batch_release.py
git commit -m "feat: add batch publication gates"
```

### Task 6: Batch-Only CLI and Removal of Legacy User Entry Points

**Files:**
- Modify: `src/newsroom/cli.py`
- Modify: `tests/test_batch_cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `BatchReleaseWorkflow`, `BatchWorkspace`.
- Produces: `batch image-plan`, `batch review`, `batch approve`, `batch publish-packages`, `batch publish-register`, `batch refresh-workspace`; no registered single-article content commands.

- [ ] **Step 1: Write failing CLI-surface tests**

```python
def test_cli_exposes_only_batch_post_selection_workflow():
    help_text = runner.invoke(app, ["--help"]).output
    for removed in ("brief", "prompt", "research", "article", "fact-check", "image"):
        assert removed not in top_level_commands(help_text)
    for command in ("image-plan", "review", "approve", "publish-packages", "publish-register", "refresh-workspace"):
        assert runner.invoke(app, ["batch", command, "--help"]).exit_code == 0
```

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/test_batch_cli.py::test_cli_exposes_only_batch_post_selection_workflow -q`

- [ ] **Step 3: Register batch commands and remove legacy Typer registrations**

Keep candidate discovery/list/show/import/export, duplicate check, batch prepare/status/resolve, and statistics. Remove only the old post-selection single-article command groups and top-level review/approve/package/publish registrations. Wire new commands directly to typed batch services and return JSON.

- [ ] **Step 4: Add command integration tests**

Exercise image-plan through publish-register against a temporary SQLite database and real fixture files. Assert command errors use exit code 2 and leave state unchanged.

- [ ] **Step 5: Run CLI tests and commit**

Run: `uv run pytest tests/test_cli.py tests/test_batch_cli.py -q`

```bash
git add src/newsroom/cli.py tests/test_cli.py tests/test_batch_cli.py
git commit -m "feat: make publication workflow batch only"
```

### Task 7: macOS Controller and Finder Dialog Flow

**Files:**
- Create: `src/newsroom/macos.py`
- Create: `tests/test_macos.py`
- Modify: `src/newsroom/cli.py`
- Modify: `src/newsroom/workspace.py`

**Interfaces:**
- Consumes: batch status, requests/inbox, `BatchWorkflow`, `BatchReleaseWorkflow`, and workspace refresh.
- Produces: `DialogPort` protocol; `OsaScriptDialogs`; `MacOSBatchController.run(batch_id: str, action: str) -> int`; hidden top-level `macos-action BATCH_ID ACTION` command used by generated scripts.

- [ ] **Step 1: Write failing controller tests with a fake dialog port**

```python
def test_import_action_rejects_wrong_request_without_mutation(waiting_content_batch, tmp_path: Path):
    dialogs = FakeDialogs(selected_file=valid_result_for_other_batch(tmp_path))
    before = snapshot_database(waiting_content_batch.room)
    code = MacOSBatchController(waiting_content_batch.room, dialogs).run(
        waiting_content_batch.batch_id, "import-result"
    )
    assert code == 2
    assert snapshot_database(waiting_content_batch.room) == before
    assert dialogs.errors[-1].startswith("所选文件不属于当前批次")


def test_cancelled_file_picker_is_noop(waiting_content_batch):
    dialogs = FakeDialogs(selected_file=None)
    assert MacOSBatchController(waiting_content_batch.room, dialogs).run(
        waiting_content_batch.batch_id, "import-result"
    ) == 0
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_macos.py -q`

- [ ] **Step 3: Implement dialogs without shell interpolation**

Use `subprocess.run(["/usr/bin/osascript", "-e", script, *args], ...)` and read user values from AppleScript `argv`; never embed file paths, titles, or error messages into executable script text. Implement alert, confirm, choose-file, choose-list, reveal-file, open-file, and open-URL operations.

- [ ] **Step 4: Implement action routing**

Allowed fixed actions are:

```python
ALLOWED_ACTIONS = {
    "open-batch", "open-next-request", "import-result", "open-review",
    "register-images", "review", "approve", "build-packages", "refresh",
}
```

Reject unknown actions. Detect the expected result from the pending request row rather than filename alone. `register-images` builds or resumes `image-selections.json`; it opens candidate URLs and collects local files before calling `apply_image_plan`.

- [ ] **Step 5: Verify scripts and commit**

Run: `uv run pytest tests/test_macos.py tests/test_workspace.py tests/test_batch_cli.py -q`

On macOS, generate one temporary batch workspace and run `zsh -n` on every `.command`. Do not invoke state-changing dialogs in the real workspace.

```bash
git add src/newsroom/macos.py src/newsroom/cli.py src/newsroom/workspace.py tests/test_macos.py
git commit -m "feat: add Finder batch actions"
```

### Task 8: Migration Preflight, Manifest, and Verified Backup

**Files:**
- Create: `src/newsroom/migration.py`
- Create: `tests/test_migration.py`
- Modify: `src/newsroom/cli.py`
- Modify: `src/newsroom/workspace.py`

**Interfaces:**
- Consumes: version 4 database and legacy artifact tree.
- Produces: `MigrationEntry`; `MigrationPlan`; `WorkspaceMigrator.preflight(source_csvs: list[Path] = []) -> MigrationPlan`; `backup(plan: MigrationPlan) -> Path`; `newsroom workspace migrate --preflight`.

- [ ] **Step 1: Write failing inventory and source tests**

```python
def test_preflight_maps_claimed_unclaimed_and_legacy_files(legacy_workspace):
    plan = WorkspaceMigrator(legacy_workspace.room).preflight()
    assert any(entry.database_field == "draft_path" for entry in plan.claimed)
    assert any(entry.source.name == "orphan-notes.md" for entry in plan.unclaimed)
    assert any(entry.target.parts[-3] == "legacy-single-articles" for entry in plan.legacy)


def test_missing_original_csv_preserves_metadata_without_blocking(legacy_workspace):
    plan = WorkspaceMigrator(legacy_workspace.room).preflight()
    assert plan.blocking_errors == []
    metadata = plan.source_metadata[legacy_workspace.batch_id]
    assert metadata["available"] is False
    assert metadata["source_sha256"] == legacy_workspace.source_sha256


def test_selected_csv_must_match_recorded_hash(legacy_workspace, tmp_path: Path):
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("different", encoding="utf-8")
    plan = WorkspaceMigrator(legacy_workspace.room).preflight([wrong])
    assert "does not match any recorded batch SHA-256" in plan.blocking_errors[0]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_migration.py -k 'preflight or csv' -q`

- [ ] **Step 3: Implement strict migration models and inventory**

```python
@dataclass(frozen=True)
class MigrationEntry:
    source: Path
    target: Path
    sha256: str
    size: int
    batch_id: str | None
    article_id: str | None
    database_table: str | None
    database_field: str | None


@dataclass
class MigrationPlan:
    claimed: list[MigrationEntry]
    legacy: list[MigrationEntry]
    unclaimed: list[MigrationEntry]
    source_metadata: dict[str, dict[str, Any]]
    blocking_errors: list[str]
```

Resolve current DB paths, derive destinations, detect collisions by normalized target path, and hash every file. A missing DB-referenced file is blocking; a missing original CSV is not.

- [ ] **Step 4: Implement and test verified backup**

Create `data/backups/workspace-migration-TIMESTAMP/` containing a SQLite backup made through the SQLite backup API, `artifacts.tar.gz`, and deterministic `manifest.json`. Reopen the DB backup, read the tar member list, and verify manifest hashes before returning.

Add a test that corrupts the tar after creation and asserts backup verification fails before any workspace staging directory exists.

- [ ] **Step 5: Add read-only CLI and commit**

Register `workspace migrate --preflight` to print and save the report without mutation.

Run: `uv run pytest tests/test_migration.py -k 'preflight or backup or csv' -q`

```bash
git add src/newsroom/migration.py src/newsroom/cli.py src/newsroom/workspace.py tests/test_migration.py
git commit -m "feat: plan and back up workspace migration"
```

### Task 9: Migration Execution, Rollback, Cleanup, and Root Launcher

**Files:**
- Modify: `src/newsroom/migration.py`
- Modify: `src/newsroom/cli.py`
- Modify: `src/newsroom/workspace.py`
- Modify: `tests/test_migration.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: a clean `MigrationPlan` and verified backup.
- Produces: `WorkspaceMigrator.execute(plan: MigrationPlan, backup_dir: Path) -> dict[str, Any]`; idempotent schema/path migration; `Migrate Newsroom.command`.

- [ ] **Step 1: Write failing hash, rollback, cleanup, and idempotency tests**

```python
def test_hash_change_after_preflight_stops_before_database_update(legacy_workspace):
    migrator = WorkspaceMigrator(legacy_workspace.room)
    plan = migrator.preflight()
    plan.claimed[0].source.write_text("changed", encoding="utf-8")
    with pytest.raises(WorkflowError, match="changed after preflight"):
        migrator.execute(plan, migrator.backup(plan))
    assert legacy_workspace.room.get_article(legacy_workspace.article_id)["draft_path"].startswith("articles/")


def test_database_failure_restores_database_and_legacy_tree(legacy_workspace, monkeypatch):
    migrator = WorkspaceMigrator(legacy_workspace.room)
    plan = migrator.preflight()
    backup = migrator.backup(plan)
    monkeypatch.setattr(migrator, "_promote_workspace", raise_runtime_error)
    with pytest.raises(RuntimeError):
        migrator.execute(plan, backup)
    assert_legacy_workspace_intact(legacy_workspace)


def test_success_archives_unclaimed_and_removes_obsolete_roots(legacy_workspace):
    migrator = WorkspaceMigrator(legacy_workspace.room)
    result = migrator.execute(migrator.preflight(), migrator.backup(migrator.preflight()))
    assert result["status"] == "PASS"
    assert (legacy_workspace.root / "workspace" / "archive" / "unclaimed-files" / "orphan-notes.md").is_file()
    for name in ("research", "articles", "images"):
        assert not (legacy_workspace.root / name).exists()


def test_completed_migration_is_idempotent(legacy_workspace):
    first = execute_clean_migration(legacy_workspace)
    second = WorkspaceMigrator(legacy_workspace.room).execute_from_current_state()
    assert second["status"] == "ALREADY_MIGRATED"
    assert second["manifest_sha256"] == first["manifest_sha256"]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_migration.py -k 'execute or hash_change or failure or cleanup or idempotent' -q`

- [ ] **Step 3: Implement copy, verify, database switch, and cleanup**

Copy to `workspace/.migration-staging-TIMESTAMP`, verify every hash, generate views/scripts, then update all path columns and `workspace_version=1` in one transaction. Promote staging to `workspace/batches` and `workspace/archive`. Reopen every DB path and validate it. Generate packages in a disposable validation directory. Only then archive unclaimed files and remove claimed legacy files/empty roots.

Persist a migration journal with stage names `BACKED_UP`, `STAGED`, `DATABASE_UPDATED`, `PROMOTED`, `VERIFIED`, and `CLEANED`. On failure after `DATABASE_UPDATED`, restore the DB backup and original artifact archive before reporting failure.

- [ ] **Step 4: Generate and test root launcher**

`BatchWorkspace.install_root_actions()` writes `Migrate Newsroom.command`, which runs preflight, opens the report, asks for explicit confirmation via the macOS controller, then calls execute. The script itself contains no destructive shell commands.

- [ ] **Step 5: Run migration tests and commit**

Run: `uv run pytest tests/test_migration.py tests/test_workspace.py -q`

```bash
git add src/newsroom/migration.py src/newsroom/cli.py src/newsroom/workspace.py tests/test_migration.py .gitignore
git commit -m "feat: migrate legacy artifacts safely"
```

### Task 10: Documentation and End-to-End Batch Demo

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/DATA_MODEL.md`
- Modify: `docs/WORKFLOW.md`
- Modify: `docs/WORKFLOW_ZH.md`
- Modify: `docs/ROADMAP.md`
- Modify: `docs/IMAGE_POLICY.md`
- Modify: `README.md`
- Modify: `scripts/demo.sh`
- Modify: `tests/test_batch_cli.py`

**Interfaces:**
- Consumes: all completed batch, workspace, migration, and macOS interfaces.
- Produces: one documented Finder-first workflow and an offline demo that uses only batch operations.

- [ ] **Step 1: Rewrite the demo around batch-only gates**

Replace the per-article loop with generation of one valid `BatchImagePlan`, then call batch image-plan, review, approve, publish-packages, and publish-register. Assert:

```bash
test -f "$demo_root/workspace/batches/$batch_id/README.md"
test -f "$demo_root/workspace/batches/$batch_id/publish/batch-manifest.json"
test ! -d "$demo_root/research"
test ! -d "$demo_root/articles"
test ! -d "$demo_root/images"
```

- [ ] **Step 2: Update user documentation**

Document Finder-first operation, every action filename, Claude inbox behavior, whole-batch gates, migration backup/recovery, and the removal of supported single-article commands. Keep `docs/archive/WORKFLOW_PHASE1_ORIGINAL.md` unchanged.

- [ ] **Step 3: Run full verification**

Run:

```bash
uv run pytest -q
bash scripts/demo.sh
git diff --check
```

Expected: all tests pass; demo reaches `PUBLISHED`; workspace contains complete packages; no legacy runtime roots are created.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/ARCHITECTURE.md docs/DATA_MODEL.md docs/WORKFLOW.md docs/WORKFLOW_ZH.md docs/ROADMAP.md docs/IMAGE_POLICY.md scripts/demo.sh tests/test_batch_cli.py
git commit -m "docs: adopt Finder-first batch workflow"
```

### Task 11: Rehearse and Migrate the Real Workspace

**Files:**
- Runtime only: copied rehearsal directory, `data/backups/`, `workspace/`
- Tracked cleanup after acceptance: `.gitignore`, operator docs, and an obsolete legacy placeholder deletion when migration archives it.

**Interfaces:**
- Consumes: verified migration commands and the current real database/artifact tree.
- Produces: migrated real workspace, verified backup, migration report, and resumable current batch.

- [ ] **Step 1: Create a full rehearsal copy outside the repository**

Quiesce production before copying anything: close every newsroom Terminal/Finder action and every external SQLite writer/editor touching runtime artifacts. The copy process must acquire the repository's exclusive operation lock, inspect SQLite journal mode plus WAL/SHM presence, create the rehearsal database with SQLite's backup API, and copy the other runtime trees while still holding the lock. `copytree(..., symlinks=True)` preserves nested symlinks so migration preflight can reject them instead of silently dereferencing them. Never point rehearsal migration commands at the production root.

```bash
set -euo pipefail
production_root="$PWD"
rehearsal_root="$(mktemp -d /tmp/newsroom-migration-rehearsal.XXXXXX)"
trap 'rm -rf "$rehearsal_root"' ERR
uv run python - "$production_root" "$rehearsal_root" <<'PY'
from pathlib import Path
import json
import os
import shutil
import sqlite3
import sys

from newsroom.coordination import operation_lock

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
source_data = source / "data"
source_database = source_data / "newsroom.db"
if source_data.is_symlink() or source_database.is_symlink():
    raise SystemExit("production data directory and database must not be symlinks")
if not source_database.is_file():
    raise SystemExit(f"missing production database: {source_database}")

with operation_lock(source, exclusive=True):
    source_uri = source_database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection:
        journal_mode = source_connection.execute("PRAGMA journal_mode").fetchone()[0]
        wal = Path(f"{source_database}-wal")
        shm = Path(f"{source_database}-shm")
        print(json.dumps({
            "journal_mode": journal_mode,
            "wal_exists": wal.exists(),
            "wal_size": wal.stat().st_size if wal.exists() else 0,
            "shm_exists": shm.exists(),
        }, sort_keys=True))

        for name in ("data", "workspace", "research", "articles", "images", "output"):
            source_path = source / name
            target_path = target / name
            if not source_path.exists() and not source_path.is_symlink():
                continue
            if source_path.is_symlink():
                os.symlink(os.readlink(source_path), target_path)
                continue

            def ignore_database(directory: str, names: list[str]) -> set[str]:
                if Path(directory) == source_data:
                    return {item for item in names if item in {
                        "newsroom.db", "newsroom.db-wal", "newsroom.db-shm"
                    }}
                return set()

            shutil.copytree(
                source_path,
                target_path,
                symlinks=True,
                ignore=ignore_database if name == "data" else None,
            )

        target_database = target / "data/newsroom.db"
        target_database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target_database) as target_connection:
            source_connection.backup(target_connection)
            integrity = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"rehearsal database backup failed integrity check: {integrity}")
PY
printf '%s\n' "$rehearsal_root" > /tmp/newsroom-migration-rehearsal-path
trap - ERR
```

- [ ] **Step 2: Run preflight and migration against the rehearsal**

Run the executable's read-only snapshot schema guard under the exclusive operation lock before opening the canonical database for checkpoint. It returns 0 only for a missing database or an existing SQLite database with no user tables; those new databases skip `mode=rw` and checkpoint. Existing versions 1, 2, 3, or 4 may then checkpoint, while future or negative versions, empty/invalid version tables, and databases with user tables but no `schema_version` stop before mutation. `Database.initialize` independently revalidates under the same lock before backup, schema changes, upgrade work, or source seeding. `init` does not move article artifacts. For v1-v3, require exactly one new automatic pre-upgrade `copy2` snapshot, then compare its hash, schema, row counts, and integrity result with the pre-init evidence. For v4, require no new upgrade snapshot and no schema change. Reject every other version. The copy2 snapshot is only an upgrade safety copy and never replaces the verified workspace migration backup created below.

After the init safety copy is validated, use the fail-fast sequence from `docs/WORKFLOW.md`: save preflight stdout as JSON, parse its `report_path` into `PLAN`, run `--backup-plan --plan "$PLAN"`, parse the returned JSON `backup_path` into `BACKUP`, independently call `WorkspaceMigrator.verify_backup(BACKUP)`, and only then run `--execute --plan "$PLAN" --backup "$BACKUP"`. Use `set -euo pipefail`; any failed command or JSON/path validation must stop before the next mutation. The exact CLI contract is:

```bash
set -euo pipefail
rehearsal_root="$(cat /tmp/newsroom-migration-rehearsal-path)"
NEWSROOM_ROOT="$rehearsal_root"
DATABASE="$NEWSROOM_ROOT/data/newsroom.db"
INIT_BACKUPS_BEFORE="$(mktemp /tmp/newsroom-init-backups.XXXXXX)"
INIT_EVIDENCE_BEFORE="$(mktemp /tmp/newsroom-init-before.XXXXXX)"
INIT_EVIDENCE_COPY="$(mktemp /tmp/newsroom-init-copy.XXXXXX)"
INIT_EVIDENCE_AFTER="$(mktemp /tmp/newsroom-init-current.XXXXXX)"
EVIDENCE_SCRIPT="$(mktemp /tmp/newsroom-db-evidence.XXXXXX)"
PREFLIGHT_JSON="$(mktemp /tmp/newsroom-preflight.XXXXXX)"
BACKUP_JSON="$(mktemp /tmp/newsroom-backup.XXXXXX)"
trap 'rm -f "$INIT_BACKUPS_BEFORE" "$INIT_EVIDENCE_BEFORE" "$INIT_EVIDENCE_COPY" "$INIT_EVIDENCE_AFTER" "$EVIDENCE_SCRIPT" "$PREFLIGHT_JSON" "$BACKUP_JSON"' EXIT

if [[ -e "${DATABASE}-wal" || -e "${DATABASE}-shm" ]]; then
  echo "Stop: the coherent rehearsal database unexpectedly has WAL/SHM state." >&2
  exit 1
fi
if [[ -d "$NEWSROOM_ROOT/data/backups" ]]; then
  find "$NEWSROOM_ROOT/data/backups" -maxdepth 1 -type f -name 'newsroom-v*.db' -print | sort > "$INIT_BACKUPS_BEFORE"
else
  : > "$INIT_BACKUPS_BEFORE"
fi
cat > "$EVIDENCE_SCRIPT" <<'PY'
from pathlib import Path
import hashlib
import json
import sqlite3
import sys

database = Path(sys.argv[1]).resolve()
with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    version_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    schema_version = (
        connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version_table else None
    )
    schema = connection.execute(
        "SELECT name, type, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    tables = [row[0] for row in schema if row[1] == "table"]
    rows = {
        table: connection.execute(
            'SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"'
        ).fetchone()[0]
        for table in tables
    }
print(json.dumps({
    "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
    "integrity": integrity,
    "journal_mode": journal_mode,
    "schema_version": schema_version,
    "schema": schema,
    "rows": rows,
}, ensure_ascii=False, sort_keys=True))
PY
python3 "$EVIDENCE_SCRIPT" "$DATABASE" > "$INIT_EVIDENCE_BEFORE"
SOURCE_SCHEMA="$(python3 - "$INIT_EVIDENCE_BEFORE" <<'PY'
from pathlib import Path
import json
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["schema_version"]
if value not in {1, 2, 3, 4}:
    raise SystemExit(f"unsupported source schema version: {value!r}")
print(value)
PY
)"

uv run newsroom --root "$NEWSROOM_ROOT" init
INIT_SAFETY_BACKUP="$(python3 - "$NEWSROOM_ROOT" "$INIT_BACKUPS_BEFORE" "$SOURCE_SCHEMA" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
before = set(Path(sys.argv[2]).read_text(encoding="utf-8").splitlines())
current = {str(path.resolve()) for path in (root / "data/backups").glob("newsroom-v*.db")}
created = sorted(current - before)
expected = 1 if int(sys.argv[3]) < 4 else 0
if len(created) != expected:
    raise SystemExit(f"expected {expected} new init safety copies, found {len(created)}")
if created:
    print(created[0])
PY
)"
if (( SOURCE_SCHEMA < 4 )); then
  python3 "$EVIDENCE_SCRIPT" "$INIT_SAFETY_BACKUP" > "$INIT_EVIDENCE_COPY"
  cmp "$INIT_EVIDENCE_BEFORE" "$INIT_EVIDENCE_COPY"
fi
python3 "$EVIDENCE_SCRIPT" "$DATABASE" > "$INIT_EVIDENCE_AFTER"
python3 - "$SOURCE_SCHEMA" "$INIT_EVIDENCE_BEFORE" "$INIT_EVIDENCE_AFTER" <<'PY'
from pathlib import Path
import json
import sys

source_version = int(sys.argv[1])
before = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
after = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
if after["integrity"] != "ok" or after["schema_version"] != 4:
    raise SystemExit("init did not produce a valid schema-v4 database")
if source_version == 4 and before["schema"] != after["schema"]:
    raise SystemExit("schema-v4 init unexpectedly changed database schema")
PY

uv run newsroom --root "$NEWSROOM_ROOT" workspace migrate --preflight > "$PREFLIGHT_JSON"
PLAN="$(python3 - "$NEWSROOM_ROOT" "$PREFLIGHT_JSON" <<'PY'
from pathlib import Path
import json
import sys

root = Path(sys.argv[1]).resolve()
payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
plan = (root / payload["report_path"]).resolve()
expected = (root / "data/workspace-migration-preflight.json").resolve()
if plan != expected or not plan.is_file():
    raise SystemExit("preflight returned an unsafe or missing plan path")
print(plan)
PY
)"

uv run newsroom --root "$NEWSROOM_ROOT" workspace migrate --backup-plan --plan "$PLAN" > "$BACKUP_JSON"
BACKUP="$(python3 - "$NEWSROOM_ROOT" "$BACKUP_JSON" <<'PY'
from pathlib import Path
import json
import sys

root = Path(sys.argv[1]).resolve()
payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
backup = Path(payload["backup_path"]).resolve()
backup_root = (root / "data/backups").resolve()
if not backup.is_relative_to(backup_root) or not backup.is_dir():
    raise SystemExit("backup command returned an unsafe or missing directory")
print(backup)
PY
)"

uv run python - "$NEWSROOM_ROOT" "$BACKUP" <<'PY'
from pathlib import Path
import sys

from newsroom.migration import WorkspaceMigrator
from newsroom.service import Newsroom

WorkspaceMigrator(Newsroom(Path(sys.argv[1]))).verify_backup(Path(sys.argv[2]))
print(f"verified migration backup: {sys.argv[2]}")
PY

uv run newsroom --root "$NEWSROOM_ROOT" workspace migrate --execute --plan "$PLAN" --backup "$BACKUP"
uv run newsroom --root "$rehearsal_root" batch status batch-17b6fd5a5e68
```

Expected: PASS, zero broken paths, the current batch still reports its two revision items, and all action scripts pass `zsh -n`.

- [ ] **Step 3: Verify rehearsal packages and rollback evidence**

Regenerate dashboards, open the copied batch README, validate current Claude requests, and build disposable packages for every eligible copied batch. Confirm the backup DB opens and every manifest hash matches.

- [ ] **Step 4: Run real preflight and inspect the saved report**

Before production init, close every newsroom/Finder action and external writer. Run the ordinary `newsroom init` command from `docs/WORKFLOW.md`; the service holds the exclusive operation lock across migration-access validation, schema inspection, WAL checkpoint, backup, upgrade, lock setup, and source seeding. Do not wrap it in another operation lock or run a manual checkpoint. Require a non-busy complete checkpoint; afterward WAL must be absent or zero bytes and SHM absent. Stop and investigate any failure or remaining sidecar without deleting it manually. Read the source schema version first. For v1-v3, require exactly one new safety snapshot and compare it with the pre-init evidence; for v4, require no new upgrade snapshot and no schema change; reject every other version. Then run the root `Migrate Newsroom.command` or the exact gated CLI sequence in `docs/WORKFLOW.md`. Confirm no blocking errors and that all claimed, legacy, and unclaimed files appear in the saved `PLAN`; create and independently verify the migration backup before `--execute --plan "$PLAN" --backup "$BACKUP"`.

- [ ] **Step 5: Execute the already-authorized real migration**

Execute migration, then verify:

```bash
uv run newsroom --root "$PWD" batch status batch-17b6fd5a5e68
uv run pytest -q
bash scripts/demo.sh
```

Open `workspace/README.md`, the current batch README, the next request, both archived Claude results, the five article directories, and all generated action scripts through Finder.

- [ ] **Step 6: Record acceptance evidence**

Save the preflight report, migration result, backup path, manifest hash, test result, demo result, and current-batch status in `workspace/migration-acceptance.md`. Do not commit runtime evidence.

If migration archived a tracked legacy placeholder such as `output/.gitkeep`, keep its Git deletion and ignore the complete obsolete top-level root. Do not recreate the placeholder after the committed receipt; record the tracked cleanup in a focused code commit without changing recovery or backup data.

### Task 12: Isolated Scheme A Prototype Handoff

**Files:**
- Create later in a separate managed worktree; no Scheme A code belongs in this branch during Scheme C implementation.

**Interfaces:**
- Consumes: the stable Scheme C service interfaces and a copied synthetic workspace.
- Produces: a separate design/spec and managed worktree for a Tkinter `Newsroom.app` prototype.

- [ ] **Step 1: Complete and accept Scheme C before starting**

Require all Scheme C tests, demo, migration acceptance, and user Finder review to pass.

- [ ] **Step 2: Start a new architectural design cycle for Scheme A**

Write a separate spec defining prototype screens, read/write isolation, demo data, `.app` packaging, and evaluation criteria. Do not infer approval of Scheme C as approval of Scheme A implementation details.

- [ ] **Step 3: Create the isolated worktree only after Scheme A spec approval**

Use `superpowers:using-git-worktrees` and attach the worktree to the task. Copy demo data only; enforce a startup check that rejects the production database path.
