# Batch Workspace and Finder Workflow Design

**Date:** 2026-10-05

**Phase:** Phase 1.1 — Human-in-the-loop batch workflow

**Status:** Approved

## Context

The current batch workflow stores Claude request/result JSON under `output/batches/`, Research Briefs under `research/`, article and fact-check Markdown under `articles/`, images under `images/`, and publication packages under `output/ARTICLE_ID/`. SQLite connects these paths, but a person using Finder cannot see one batch and all of its material in one place. Most state-changing actions also require copied terminal commands.

The replacement remains local and human-driven. Gemini ends at the reviewed CSV. Claude App receives and returns files manually. SQLite remains the canonical state store. No model API, web UI, scheduler, queue, automatic image download, or automatic CMS publication is introduced.

## Goals

1. Give each batch one Finder-visible workspace containing every active artifact for that batch.
2. Keep each article's research, current and historical drafts, fact-checks, image candidates, selected image, and package together inside the batch.
3. Let a macOS user advance the batch by double-clicking generated `.command` files instead of typing commands.
4. Apply review, approval, and package generation to the complete batch rather than to individual articles.
5. Migrate existing batches, preserve audit history, verify file hashes and database references, and remove obsolete artifact directories only after successful verification.
6. Archive legacy single-article material without exposing a continuing single-article workflow.
7. Keep the Python service layer deterministic and testable independently of Finder scripts.

## Non-goals

- A web UI or local HTTP service.
- A native macOS application in this implementation. A Tkinter `Newsroom.app` will be explored later in an isolated worktree against copied demo data.
- Windows or Linux support for generated Finder actions.
- Model API calls, background processing, automatic image download, or CMS automation.
- Editing article content inside the launcher.
- Continuing the old single-article content lifecycle as a supported user path.

## Selected Approach

Use a generated batch workspace, a generated Markdown dashboard, and Finder-launchable `.command` files. The `.command` files contain only stable bootstrap logic: locate the repository, activate the `uv` environment, pass the batch ID and action name to one Python macOS controller, and display its exit result. Business rules remain in Python services.

Terminal may appear briefly when a `.command` file runs. The user does not type commands. File selection, confirmation, cancellation, and actionable errors use `osascript` dialogs. Finder and the user's default Markdown editor display artifacts.

## Canonical Directory Layout

```text
workspace/
├── README.md
├── batches/
│   └── BATCH_ID/
│       ├── README.md
│       ├── OPEN_BATCH.command
│       ├── actions/
│       │   ├── 01-打开下一个Claude任务.command
│       │   ├── 02-导入Claude返回文件.command
│       │   ├── 03-打开整批审核材料.command
│       │   ├── 04-登记整批图片.command
│       │   ├── 05-完成整批审核.command
│       │   ├── 06-批准整个批次.command
│       │   ├── 07-生成全部发布包.command
│       │   └── 08-刷新批次视图.command
│       ├── input/
│       │   ├── candidates.csv
│       │   └── source-metadata.json
│       ├── claude/
│       │   ├── requests/
│       │   │   ├── content-request.json
│       │   │   ├── fact-check-request-v1.json
│       │   │   └── revision-request-v2.json
│       │   ├── inbox/
│       │   │   └── 将Claude返回的JSON放在这里.txt
│       │   └── archive/
│       │       ├── content-result.json
│       │       └── fact-check-result-v1.json
│       ├── articles/
│       │   └── ARTICLE_ID/
│       │       ├── research-brief-v1.md
│       │       ├── article-v1.md
│       │       ├── fact-check-v1.md
│       │       ├── research-brief-v2.md
│       │       ├── article-v2.md
│       │       ├── fact-check-v2.md
│       │       ├── image-candidates.json
│       │       └── hero.EXT
│       ├── publish/
│       │   ├── ARTICLE_ID/
│       │   └── batch-manifest.json
│       └── batch-state.json
└── archive/
    ├── legacy-single-articles/
    └── unclaimed-files/
```

`workspace/batches/BATCH_ID/` is the only active artifact root for a batch. Article directories contain artifacts but no per-article action scripts. Request and result filenames are human-readable; immutable request IDs and hashes remain in SQLite and inside the JSON documents.

An article already completed in an earlier batch remains canonical in its original batch. A later `REUSED_COMPLETE` item appears in the later README with a relative link to its owner batch and is excluded from editing, approval, and package generation in the later batch.

## Generated Dashboards

The root `workspace/README.md` lists batches, aggregate state, progress, last update, and relative links.

Each batch `README.md` contains:

- batch ID, source CSV, timestamps, and aggregate status;
- one explicit next action;
- counts by item state;
- an article table with title, article ID, version, fact-check outcome, image state, approval state, and package link;
- links to the current Claude request and archived results;
- unresolved duplicate, revision, image, and validation issues;
- an explanation of the generated action scripts.

`batch-state.json` exposes the same derived status in a machine-readable format. Both files are regenerated from SQLite and the canonical workspace. They are never authoritative and may be deleted and rebuilt.

## macOS Action Controller

All generated scripts dispatch to one controller through this fixed interface:

```text
newsroom macos-action BATCH_ID ACTION
```

The controller owns dialog presentation and calls batch service methods. It does not parse human-facing CLI text. Generated scripts derive the repository root by walking upward to `pyproject.toml`, so moving the repository does not embed stale absolute paths. Scripts are created with executable permissions and contain no candidate, article, source, or license data.

### Open batch

`OPEN_BATCH.command` opens the batch directory and README, then reveals the current Claude request when one is pending. A dialog states the current aggregate status and next action.

### Import Claude result

The import action determines the expected result type from SQLite. It first checks `claude/inbox/`:

- one matching JSON: offer it for confirmation;
- no matching JSON: show a file picker;
- multiple matching JSON files: show a chooser;
- wrong request ID, batch ID, output filename, schema, or membership: reject without mutation.

Successful import archives the original result, promotes generated artifacts, creates the next request when the resulting workflow state requires one, and refreshes both dashboards. Existing request/result idempotency and full-document validation remain mandatory.

### Open review material

The review-material action opens the batch article directory and README. It never changes state.

### Register batch images

The image action is available only after every included article has a `PASS` fact-check outcome. It iterates all articles without a registered image and presents stored candidates. For each selection it opens the original landing and rights URLs, asks the user to select a downloaded local image, and records an explicit rights confirmation.

Selections are first written to a resumable `image-selections.json` draft. No article state changes until every required selection is present and the complete plan validates. Images are copied through staging, database updates occur in one transaction, and failures remove staged copies. Cancellation preserves the draft but does not partially register images.

### Review, approve, and package

Batch review is allowed only when every included, non-reused article is `IMAGE_READY`. One human reason is recorded for the batch action and copied to each audited article transition inside one transaction.

Batch approval is allowed only when every included article is `REVIEWED`. Package generation is allowed only when every included article is `APPROVED`. Packages are built and validated under a batch staging directory, then promoted together to `publish/`. A failure leaves the existing publish directory unchanged.

External CMS upload remains manual. Publication URL registration remains in Phase 1.1 and accepts one batch result file mapping every article ID to its published URL; there is no supported per-article publish command.

## Aggregate Gates and State

The batch advances consistently:

```text
READY_FOR_CONTENT
→ WAITING_CONTENT_RESULT
→ WAITING_FACT_CHECK
→ NEEDS_REVISION (when any article fails)
→ WAITING_FACT_CHECK (after revision import)
→ READY_FOR_IMAGES (only when all included articles pass)
→ READY_FOR_REVIEW
→ REVIEWED
→ APPROVED
→ PACKAGED
→ PUBLISHED (after all publication URLs are registered)
```

Revision requests contain only failed articles, but a batch does not enter image, review, approval, or packaging stages until all included articles pass. `REUSED_COMPLETE` and `EXCLUDED` items do not participate in these gates.

Candidate discovery, CSV preparation, and ambiguous duplicate resolution remain candidate-level inputs because they occur before a complete article batch exists. Supported post-selection operations are batch-only.

## Supported Command Surface

Public batch commands back the Finder actions and automated tests:

```text
newsroom batch prepare CSV_FILE
newsroom batch status BATCH_ID
newsroom batch resolve BATCH_ID CANDIDATE_ID ...
newsroom batch content-import BATCH_ID RESULT_JSON
newsroom batch fact-check-import BATCH_ID RESULT_JSON
newsroom batch revision-request BATCH_ID
newsroom batch revision-import BATCH_ID RESULT_JSON
newsroom batch image-plan BATCH_ID PLAN_JSON
newsroom batch review BATCH_ID ...
newsroom batch approve BATCH_ID ...
newsroom batch publish-packages BATCH_ID
newsroom batch publish-register BATCH_ID URLS_JSON
newsroom batch refresh-workspace BATCH_ID
newsroom workspace migrate
```

Legacy single-article content commands cease to be documented or exposed as supported CLI entry points: `brief create`, `prompt create`, `research register`, `article register`, `fact-check register`, `image register`, top-level `review`, `approve`, `publish-package`, and `publish`. Their underlying focused service operations remain private building blocks used only inside batch transactions.

## Migration Design

Migration is launched through a root-level `Migrate Newsroom.command`, backed by a deterministic migration service. It has a mandatory read-only preflight and an explicit confirmation before mutation.

### Preflight

Preflight inventories:

- database rows and every referenced path;
- files in `research/`, `articles/`, `images/`, `output/batches/`, `output/ARTICLE_ID/`, and `output/prompts/`;
- batch ownership and `REUSED_COMPLETE` relationships;
- missing references, unclaimed files, duplicate destinations, and expected backup size.

Any missing referenced file, conflicting destination, ambiguous ownership, or unreadable database blocks migration. Ownership inference collects every article and batch identifier matched structurally, by path component, and by the applicable filename stem or prefix, using Unicode-normalized case-insensitive comparison for macOS. More than one article or batch candidate, or a batch candidate inconsistent with the matched article's recorded owner, is blocking and is reported deterministically before collision handling.

The v3-to-v4 schema upgrade deliberately leaves `articles.owning_batch_id` null. Migration preflight projects an owner from `batch_items`: exactly one relationship whose status is neither `REUSED_COMPLETE` nor `EXCLUDED` claims a null article; zero leaves a true legacy single article unowned; two or more eligible rows block even when they name the same batch. A recorded non-null owner must have exactly one eligible row and that row must name the owner, or preflight blocks. Inactive reused/excluded relationships never acquire or transfer ownership. The complete projection, including explicit nulls, is saved in the deterministic plan and backup manifest. Any relationship change alters the database snapshot and invalidates execute. Current and superseded request/result documents are routed and validated with the projection.

All destination conflicts block except one narrow archive-only case: a uniquely owned legacy article has exactly one database-bound artifact assigned to the preferred archive target and one or more unbound legacy extras project to that same target. The database-bound artifact keeps the canonical name. Every extra is preserved separately beneath that article's `legacy-files/` hierarchy using its original relative directory and a deterministic suffix derived from the exact UTF-8 source path, so casefolding and NFC-equivalent spellings still receive distinct target leaves on macOS. The destination leaf is bounded by the actual destination filesystem's `PC_NAME_MAX`, measured in UTF-8 bytes; overlong stems and pathological extensions are truncated only at valid UTF-8 boundaries while retaining the discriminator and a useful extension when feasible. Every copied source-parent component must already be valid UTF-8 and fit the same destination limit; parent directories are never silently renamed. An unavailable/unusable limit or a limit too small for the mandatory discriminator or a copied parent component blocks preflight. Two database-bound entries remain blocking even when both database rows reference the identical source, target and field; conflicts containing only unbound files, ambiguous ownership, file/ancestor conflicts, symlinks, traversal, and existing destination conflicts also block. An existing destination is exempt only for the same validated entry whose source and target are identical; another planned source moving away from that path does not make it available for reuse. No source is overwritten, deleted, or content-deduplicated; all safely mapped sources remain in the manifest and verified backup, while SQLite path projection still applies only to database-bound entries. The report is saved for review.

The original CSV is supporting input rather than a current database path. Migration copies it only when the user selects a file whose SHA-256 matches `batches.source_sha256`. A missing original CSV does not block migration; `input/source-metadata.json` preserves its recorded filename and hash, and the README marks the source copy unavailable.

### Backup

Before mutation, create:

```text
data/backups/workspace-migration-YYYYMMDD-HHMMSS/
├── newsroom.db
├── artifacts.tar.gz
└── manifest.json
```

The manifest records original path, target path, size, SHA-256, owning batch/article, and database field. The database and archive are reopened and verified before migration continues.

### Copy, verify, switch, clean

1. Copy all claimed files into a staging workspace.
2. Compare every staged SHA-256 with the migration manifest.
3. Generate dashboards and action scripts in staging.
4. Validate article version sets, current database pointers, request/result identities, images, and existing packages.
5. In one SQLite transaction, backfill projected null owners, update canonical artifact and request/result paths, and record the workspace migration version.
6. Atomically promote staging to `workspace/`.
7. Reopen SQLite and verify every new reference.
8. Generate publication packages from migrated data in a disposable validation location.
9. Move unclaimed but nonempty files to `workspace/archive/unclaimed-files/`.
10. Atomically retain complete obsolete trees in journal-bound local recovery storage after all checks pass.

Failure before database commit leaves the original database and directories untouched. Failure after database commit triggers restoration from the verified backup and records a failure report. Re-running a completed migration is idempotent.

Legacy single-article artifacts with no batch ownership move to `workspace/archive/legacy-single-articles/ARTICLE_ID/` and become read-only reference material. They do not receive action scripts or active batch state. An unowned article must have no active membership; `REUSED_COMPLETE` and `EXCLUDED` do not claim it. Historical publication packages are copied and hash-bound as archive input rather than regenerated or subjected to current release gates. Present identity fields and comparable canonical bytes must agree. `metadata.json.image_id` is optional for older formats, but when present it must exactly equal the article value, including null. Absent modern fields, optional members, nullable images, historical path values, and old hero naming remain valid. Batch-owned `APPROVED`/`PUBLISHED` articles continue through current PASS, image, review-material, and package validation and must have exactly one active membership row naming their owner.

## Data Model Changes

Add a workspace migration version to metadata and a nullable, immutable `owning_batch_id` on articles. Articles created through a batch receive that batch ID. Pre-batch legacy articles with no eligible active batch relation retain `NULL` and move to the read-only legacy archive. A unique eligible historical relation is backfilled only by verified workspace migration; the saved plan, manifest, journal, and receipt bind that projection. Existing path columns remain relative to the repository root but point beneath `workspace/batches/BATCH_ID/` after migration.

Batch aggregate status becomes an enforced lifecycle rather than a loose summary. Batch operations validate all participating item/article states and record one batch-level audit event plus the necessary article transitions.

The schema documentation, initialization SQL, upgrade backup behavior, and copied-old-database migration tests must change together.

## Error Handling and Recovery

- Dialogs show a short explanation and save full diagnostics under the batch workspace.
- Cancel is a normal outcome and never changes canonical state.
- A selected result with the wrong identity is rejected before file copies or database writes.
- Filesystem mutations use staging and atomic promotion.
- Multi-article state changes use one SQLite transaction.
- Dashboard regeneration is retriable derived work. A dashboard failure does not falsify canonical state.
- A failed package build leaves prior packages unchanged.
- The README always reports an actionable recovery step based on canonical state.

## Security and Privacy

- Generated scripts quote every path and pass fixed action names; user-controlled text is never interpolated as shell code.
- SQL remains parameterized.
- File pickers accept local files only and validate extensions and content contracts.
- The controller never downloads remote images or evaluates result JSON as code.
- Source text, local databases, generated workspaces, downloaded images, and publication packages remain ignored by Git.
- Migration backups remain local and are never uploaded automatically.

## Testing and Acceptance

Implementation follows TDD with temporary directories and real SQLite databases. Required coverage includes:

- layout and relative-link generation;
- executable, relocatable `.command` generation;
- result auto-detection and wrong-file rejection;
- aggregate fact-check, image, review, approval, package, and publication gates;
- resumable image-selection drafts without partial registration;
- atomic multi-article transitions and package staging;
- migration preflight, ownership projection, relationship-change rejection, hash mismatch, missing file, collision, rollback, idempotency, and cleanup;
- separation of owned current release validation from unowned legacy package validation, including exact older package shapes;
- `REUSED_COMPLETE` owner links;
- legacy single-article archival;
- derived dashboard rebuilds;
- migration of a copied real-schema database fixture;
- an offline demo from reviewed CSV through complete batch packages using synthetic Claude JSON and local image fixtures.

Before operating on real data, the migration runs against a copied database and copied artifact tree. Acceptance requires matching manifest hashes, zero broken database references, successful package validation, and a readable Finder workspace for every migrated batch.

## Rollout

1. Implement workspace paths and dashboard generation behind new batch services.
2. Add batch-only image, review, approval, package, and publication operations.
3. Generate and test Finder actions in temporary workspaces.
4. Implement and rehearse migration on copied data.
5. Run all tests and the offline demo.
6. Create the verified backup and migrate the real workspace.
7. Inspect every migrated batch through Finder and verify current work can resume.
8. Remove legacy public command entry points and update workflow documentation.
9. After Scheme C is accepted, create a separate managed Git worktree for the Scheme A Tkinter prototype using copied demo state only.

## Decisions and Assumptions

- macOS is the only supported desktop for this phase.
- Brief Terminal appearance from `.command` launch is acceptable; typed terminal interaction is not required.
- The complete batch advances through post-writing gates together.
- SQLite remains authoritative; README and JSON dashboards are derived.
- Active article artifacts belong to one canonical batch workspace.
- Legacy single-article work is archived rather than converted into a synthetic active batch.
- Scheme A is a separate follow-up project and cannot mutate the production database or workspace during evaluation.
