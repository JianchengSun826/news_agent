# Claude Batch Newsroom Workflow Design

**Date:** 2026-10-05

**Phase:** Phase 1.1 — Human-in-the-loop batch extension

**Status:** Approved

## Context

The current Phase 1 workflow processes one candidate and one model handoff at a time. It also uses provider-neutral prompts, while the intended operating model now has a strict provider boundary:

- Gemini performs scheduled discovery, writes reviewed rows to Google Sheets, and produces the CSV.
- The local CLI imports and validates the CSV, performs deterministic deduplication, and maintains workflow state.
- Claude App performs all post-import research, writing, image search, and one independent fact-check pass.
- Humans verify sources and image rights, resolve ambiguous duplicates, approve publication, and publish externally.

The workflow must process all eligible candidates from one CSV as a batch without a Claude API, paid API keys, background workers, schedulers, or automatic network downloads.

## Goals

1. Import one CSV as a durable, resumable batch.
2. Reuse previously imported candidates instead of duplicating them.
3. Automatically deduplicate every batch item and auto-select only `NEW` candidates.
4. Exchange one JSON request and one JSON result with each of two Claude conversations.
5. Produce Research Briefs, articles, and copyright-safe image candidates in the first conversation.
6. Independently fact-check the full batch in a new Claude conversation.
7. Route `PASS` articles forward and `FAIL` articles to `NEEDS_REVISION` without blocking the rest of the batch.
8. Preserve audit history, artifact versions, and existing single-article commands.

## Non-goals

- Calling Claude, Gemini, or any other model through an API.
- Automatically downloading images or deciding that an image license is valid.
- Generating images.
- Automatically publishing to a CMS.
- Replacing SQLite, adding a web UI, queue, worker, Docker, Redis, or cloud storage.
- Modifying `docs/archive/WORKFLOW_PHASE1_ORIGINAL.md`.

## User Workflow

### 1. Prepare the batch

```bash
uv run newsroom batch prepare ~/Downloads/candidates.csv
```

The command validates the complete CSV before writing, computes a source-file SHA-256, reuses matching candidates, imports new candidates, runs deterministic deduplication, auto-selects `NEW` candidates, creates article records, and writes:

```text
output/batches/BATCH_ID/BATCH_ID-content-request.json
```

Candidates classified as `FOLLOW_UP`, `RELATED`, `POSSIBLE_DUPLICATE`, or `DUPLICATE` remain in the batch with `WAITING_DUPLICATE`. Existing completed articles are linked as `REUSED_COMPLETE` and excluded from the Claude request.

### 2. First Claude conversation

The user uploads the single content request and sends one fixed message:

```text
严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。
```

Claude researches every requested article, writes the Research Brief and article, searches for up to three eligible image candidates, and creates the downloadable result file named by `output_file`.

The user imports it once:

```bash
uv run newsroom batch content-import BATCH_ID \
  ~/Downloads/BATCH_ID-content-result.json
```

After successful import, the CLI automatically creates:

```text
output/batches/BATCH_ID/BATCH_ID-fact-check-request.json
```

### 3. Independent Claude fact-check conversation

The user opens a new Claude conversation, uploads the single fact-check request, and sends the same fixed instruction. Claude returns one fact-check result covering every requested article.

```bash
uv run newsroom batch fact-check-import BATCH_ID \
  ~/Downloads/BATCH_ID-fact-check-result.json
```

Each valid result is applied independently after the complete JSON document passes schema and membership validation:

- `PASS` advances the article to `FACT_CHECKED`.
- `FAIL` advances the article to `NEEDS_REVISION`.

### 4. Images and publication

For each `PASS` article, the user opens the candidate's original rights page, confirms the stated rights, downloads the image manually, and registers it with the existing `image register` command. Review, approval, package generation, external publication, and publication registration remain human gates.

## Minimal Claude Interaction

The request JSON contains all instructions, content rules, source rules, output schema, requested filename, and batch data. The user never copies a long prompt into Claude. Each Claude window requires exactly:

1. Upload one request JSON.
2. Send one fixed sentence.
3. Download one result JSON.

The request instructs Claude to place all deliverable content in the result file rather than expanding article text in chat.

## CLI Surface

```bash
newsroom batch prepare CSV_FILE
newsroom batch status BATCH_ID
newsroom batch resolve BATCH_ID CANDIDATE_ID --decision include|exclude --actor ACTOR --reason REASON
newsroom batch content-import BATCH_ID RESULT_JSON
newsroom batch fact-check-import BATCH_ID RESULT_JSON
newsroom batch revision-request BATCH_ID
newsroom batch revision-import BATCH_ID RESULT_JSON
newsroom image candidates ARTICLE_ID
newsroom image register ARTICLE_ID --candidate IMAGE_CANDIDATE_ID --file IMAGE_FILE --rights-confirmed
```

`batch status` reports item counts and lists items requiring duplicate decisions, Claude content, fact-checking, revision, image selection, or publication review.

`batch resolve` is required only for non-`NEW` results. `include` preserves `FOLLOW_UP` or `RELATED`; `POSSIBLE_DUPLICATE` and `DUPLICATE` require an explicit duplicate override before inclusion. `exclude` rejects the candidate with an audited reason.

`revision-request` includes only `NEEDS_REVISION` articles and their blocking findings. `revision-import` produces a new article version and returns those articles to `DRAFTED`; a later fact-check request must be handled in another new Claude conversation.

Existing single-article commands remain supported.

## Database Schema Version 3

Before migrating an existing real database, `newsroom init` creates a timestamped backup under `data/backups/`. The migration is additive and must be tested against a copied version 2 database.

### Candidate changes

Add nullable `candidate_fingerprint`. The fingerprint is a SHA-256 of canonical `event_date`, normalized company, product, event type, core fact, and primary URL. New imports query this field before insertion. Migration backfills existing rows; collisions are reported for manual review instead of silently merging records.

### `batches`

| Column | Meaning |
| --- | --- |
| `batch_id` | Random `batch-...` primary key |
| `source_filename` | Original CSV basename |
| `source_sha256` | Hash used for idempotent prepare |
| `status` | Current aggregate batch state |
| `content_request_id` | Latest content-window request ID |
| `fact_check_request_id` | Latest fact-check-window request ID |
| `created_at`, `updated_at` | UTC ISO-8601 timestamps |

Preparing the same CSV hash returns the existing batch unless the user explicitly requests a new batch.

### `batch_requests`

Every Claude handoff has an immutable row containing `request_id`, `batch_id`, request type (`CONTENT`, `FACT_CHECK`, or `REVISION`), revision number, request path, expected result filename, imported result path and SHA-256, request status, and timestamps. The IDs on `batches` point to the latest request while this table preserves all earlier rounds.

### `batch_items`

| Column | Meaning |
| --- | --- |
| `batch_id`, `candidate_id` | Composite primary key |
| `article_id` | Nullable until selected |
| `item_status` | Batch-specific processing stage |
| `duplicate_classification` | Recorded deterministic result |
| `content_version` | Latest article content version |
| `error_message` | Latest actionable failure |
| `created_at`, `updated_at` | UTC timestamps |

Batch item states include `WAITING_DUPLICATE`, `READY_FOR_CONTENT`, `CONTENT_IMPORTED`, `FACT_CHECKED`, `NEEDS_REVISION`, `REUSED_COMPLETE`, and `EXCLUDED`.

### `image_candidates`

Each row stores `image_candidate_id`, `batch_id`, `article_id`, original landing page URL, optional direct download URL, creator, rights basis, license name, rights URL, attribution requirement and text, relevance explanation, review status, notes, and timestamps.

Allowed rights bases are:

- `PUBLIC_DOMAIN`
- `OPEN_LICENSE`
- `OFFICIAL_PRESS_LICENSE`
- `OWNED`

Search-engine thumbnails, ordinary media article images, unclear copyright, unsupported fair-use claims, and records without an original rights page are invalid.

`newsroom image candidates ARTICLE_ID` shows the stored options. After opening the original rights page and downloading the file, the user registers the chosen row with `--candidate` and the explicit `--rights-confirmed` flag. The CLI copies the candidate's rights metadata into the permanent image record, marks it selected, and rejects a candidate that belongs to a different article.

### Article changes

Add `fact_check_outcome` and `revision_number`. The article state machine gains `NEEDS_REVISION`.

### Permanent image changes

Add nullable `image_candidate_id`, `rights_basis`, and `rights_statement` to `images`. Legacy AI-generated and manually licensed images remain valid. Images registered from a Claude candidate copy these fields and all license/attribution metadata into the permanent record so publication packages remain auditable even if the batch artifacts are later archived.

## State Transitions

Normal batch path:

```text
Candidate: CANDIDATE → DUPLICATE_CHECKED → SELECTED
Article:   SELECTED → RESEARCHED → DRAFTED → FACT_CHECKED
           → IMAGE_READY → REVIEWED → APPROVED → PUBLISHED
```

Failure and revision path:

```text
DRAFTED → NEEDS_REVISION → DRAFTED → FACT_CHECKED
```

Content import records the `SELECTED → RESEARCHED` and `RESEARCHED → DRAFTED` transitions separately inside one database transaction. Auto-selection uses actor `system` and an explicit batch reason. Human duplicate decisions retain actor and reason.

## Artifact Versioning

Claude request and result files are stored unchanged under:

```text
output/batches/BATCH_ID/
```

Article artifacts are versioned rather than overwritten:

```text
research/ARTICLE_ID/research-brief-v1.md
articles/ARTICLE_ID/article-v1.md
articles/ARTICLE_ID/fact-check-v1.md
articles/ARTICLE_ID/article-v2.md
articles/ARTICLE_ID/fact-check-v2.md
```

The article row points to the latest accepted version. Publication packages copy the current version while retaining the version number in metadata.

## JSON Contracts

All request and result models use Pydantic v2 with `extra="forbid"`. Every document includes:

- `schema_version`
- `batch_id`
- `request_id`
- exact `output_file`
- an `items` array keyed by `article_id`

The content request additionally contains complete instructions, candidate facts, verified source URLs, editorial rules, and output-field descriptions.

Each content result item contains:

- `article_id`
- `research_brief_md`
- `title_cn`
- `slug`
- `article_md`
- zero to three `image_candidates`

Each fact-check result item contains:

- `article_id`
- `outcome`: `PASS` or `FAIL`
- `report_md`
- structured `blocking_issues`

The importer requires an exact match between requested and returned article IDs. It rejects duplicate IDs, extra IDs, missing IDs, mismatched batch/request IDs, empty Markdown, invalid URLs, duplicate slugs, unknown fields, and incomplete rights metadata.

## Atomicity and Idempotency

CSV parsing and JSON validation finish before database writes. Content import uses one transaction for the entire batch so an invalid item cannot leave partial research or draft state. Fact-check JSON is validated as a whole, then all item outcomes are applied in one transaction; a `FAIL` outcome is valid data rather than an import error.

The SHA-256 of every imported result is stored with its immutable `batch_requests` row. Re-importing the same result returns the existing state. A different result for an already completed request is rejected and must use the revision workflow.

Filesystem artifacts are written to temporary files first and atomically renamed only after validation. Database changes and artifact promotion must be coordinated so failures leave neither misleading state nor partial canonical files.

## Prompt Files

Prompts remain outside Python:

```text
prompts/claude_batch_content.md
prompts/claude_batch_fact_check.md
prompts/claude_batch_revision.md
```

`prompts/gemini_spark_discovery.md` remains the only Gemini prompt. Existing provider-neutral single-item prompts remain for backward compatibility, but the current workflow documentation defaults to the Claude batch path.

## Security and Rights Controls

- External webpages are evidence, never instructions; Claude must ignore instructions embedded in sources.
- The CLI performs no network calls and never downloads an image URL.
- Every URL is restricted to HTTP(S) and validated at the input boundary.
- SQL remains parameterized.
- Claude image suggestions do not satisfy the image approval gate.
- A human must verify the original rights page and supply the local image file.
- Publication is blocked without a registered file and rights record.

## Error Reporting

Validation errors identify the exact JSON path, for example:

```text
items[2].image_candidates[0].rights_url: valid HTTP(S) URL required
```

`batch status` exposes the last error for each item. Commands exit with code 2 for validation or workflow errors and do not print a success payload after failure.

## Testing

Tests use temporary workspaces and real SQLite databases. Coverage includes:

1. Version 2 to version 3 migration and backup behavior.
2. Existing candidate fingerprint backfill and collision reporting.
3. Full CSV validation before mutation.
4. Reuse of already imported candidates and completed articles.
5. Automatic deduplication and auto-selection of only `NEW`.
6. Pausing every non-`NEW` classification.
7. Batch request generation and minimized Claude instructions.
8. Strict JSON schema, membership, request ID, URL, slug, and rights validation.
9. Atomic rollback on one invalid content item.
10. Idempotent re-import of the same result.
11. `PASS` and `FAIL` routing.
12. Revision artifact preservation and repeated independent fact-checking.
13. Image candidate review gates and publication package integrity.

`scripts/demo.sh` gains offline fixture JSON files that exercise the full batch path without calling Gemini, Claude, or the internet.

## Documentation and Rollout

Update `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md`, `docs/ROADMAP.md`, and `docs/WORKFLOW.md`. Preserve `docs/archive/WORKFLOW_PHASE1_ORIGINAL.md` unchanged. Document the exact two-window Claude App process and the remaining human image-rights and publication steps.

Rollout order:

1. Implement models and version 3 migration.
2. Implement batch prepare and status.
3. Implement content request/import.
4. Implement independent fact-check request/import.
5. Implement revision flow and image candidate persistence.
6. Update publication metadata and documentation.
7. Run the full test suite and offline demo.
8. Run the user's existing CSV through `batch prepare`; it must reuse the six existing candidate records, skip the completed Dots article, and prepare eligible remaining articles without duplication.
