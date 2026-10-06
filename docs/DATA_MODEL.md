# Data Model

SQLite schema version 4 is created or upgraded by `newsroom init`. Existing v1/v2/v3 databases are copied to `data/backups/` before migration. SQLite remains canonical; CSV, JSON, and Markdown are interchange or artifact formats.

Fresh initialization creates `data/` and its database/coordination files only. Batch preparation creates the current workspace directly; it does not require or create top-level `research/`, `articles/`, `images/`, or `output/`. This directory behavior changes no schema.

## Core records

`candidates` stores discovery metadata, normalized event fields, source URLs, duplicate state, selection state, audit timestamps, and `candidate_fingerprint`. The SHA-256 fingerprint canonicalizes event date, company, product, event type, core fact, and primary URL so a new CSV can reuse an old candidate.

`articles` links one candidate to at most one article. It stores workflow status, event metadata, current artifact paths, title/slug, `fact_check_outcome`, `revision_number`, selected `image_id`, publication data, and timestamps. `owning_batch_id` references the batch that first created an article and is immutable through the workflow. New articles created during batch preparation or duplicate inclusion store that batch ID; reusing an article never transfers ownership. The additive v3-to-v4 schema upgrade leaves this column null. Workspace migration may backfill it only when exactly one non-`REUSED_COMPLETE`, non-`EXCLUDED` `batch_items` relationship proves the historical owner. Zero eligible relationships remain unowned; multiple relationships or a conflict with a recorded owner block migration. Research, article, and fact-check files use `-vN.md`; current paths point to the active version.

`images` is the permanent publication record. Legacy manual/AI provenance is retained for recovery and archives. The supported batch path registers one selected local image per article. Batch-selected images copy `image_candidate_id`, rights basis, rights statement, landing page, creator, license, rights URL, and attribution so a publication package does not depend on transient model output.

`sources` is the domain reliability registry. `state_transitions` is the append-only actor/reason audit log.

## Batch records

`batches` stores `batch_id`, original filename and byte SHA-256, aggregate status, latest content request ID, latest fact-check request ID, and timestamps. `workspace_version` is a non-null integer recording the batch workspace layout version. Its schema default remains 0 for legacy or manually inserted rows requiring migration; `BatchWorkflow.prepare` explicitly records 1 for new batches created directly in the canonical Scheme C layout. Verified migration upgrades existing batches to 1 without changing workflow states. An identical file hash returns the existing batch.

`batch_items` links each batch candidate to its reused or new article. It records deterministic duplicate classification, batch-specific status, content version, and the latest actionable error. Important statuses are `WAITING_DUPLICATE`, `READY_FOR_CONTENT`, `CONTENT_IMPORTED`, `FACT_CHECKED`, `NEEDS_REVISION`, `REUSED_COMPLETE`, and `EXCLUDED`.

`batch_requests` is immutable handoff history. Each `req-...` stores type (`CONTENT`, `FACT_CHECK`, or `REVISION`), revision, request path, expected result filename, imported result path/hash, status, and timestamps. Identity plus exact membership prevents a stale or partial JSON from being applied.

`image_candidates` stores Claude search suggestions: article/batch IDs, landing and optional direct image URLs, creator, rights basis, license, original rights URL and statement, attribution, relevance, and review status. Allowed rights bases are `PUBLIC_DOMAIN`, `OPEN_LICENSE`, `OFFICIAL_PRESS_LICENSE`, and `OWNED`.

## Canonical workspace and ownership

Active batch-owned artifact and request/result paths remain repository-relative and point beneath `workspace/batches/OWNING_BATCH_ID/`; migrated unowned legacy paths point into `workspace/archive/`. Research, drafts, and fact-checks live in `articles/ARTICLE_ID/` as `research-brief-vN.md`, `article-vN.md`, and `fact-check-vN.md`; selected image bytes live alongside them as `hero.EXT`. Candidate metadata and image selections are retained in SQLite. `image-candidates.json`, batch/root README, and `batch-state.json` are derived views and can be rebuilt by `batch refresh-workspace`.

Requests use `claude/requests/content-request.json`, `fact-check-request-vN.json`, and `revision-request-vN.json`. Imported originals use corresponding `*-result*.json` names in `claude/archive/`; historical superseded requests may have an additional immutable request-ID directory. The request JSON's `output_file` is the result filename contract; inbox matching uses its declared identity, not merely the selected local filename. `claude/inbox/` is a manual drop location with no watcher or background imports.

`input/candidates.csv` preserves the original bytes and `source-metadata.json` its filename/hash. During migration the CSV copy is optional and must match the stored hash. `image-selections.json` is a resumable incomplete draft, not a canonical registration or approval. Only a complete, validated `BatchImagePlan` can advance every participating article in one transaction. `publish/ARTICLE_ID/` contains final package material; `publish/batch-manifest.json` maps the exact active article set to package paths and revision numbers.

`EXCLUDED` and `REUSED_COMPLETE` items are omitted from image/review/approval/publication membership. They are also ineligible evidence for migration ownership and never acquire or transfer ownership. Reused articles with non-null `owning_batch_id` retain that owner's files, and later dashboards link to them without transferring ownership. Legacy articles with no eligible active relationship retain null ownership and become read-only material under `workspace/archive/legacy-single-articles/`; unclaimed files are inventoried under `workspace/archive/unclaimed-files/` during migration.

## Whole-batch gates and durable recovery

Batch statuses include `WAITING_DUPLICATE`, `READY_FOR_CONTENT`, `WAITING_FACT_CHECK`, `NEEDS_REVISION`, `WAITING_REVISION`, `READY_FOR_IMAGE`, `REVIEWED`, `APPROVED`, `PACKAGED`, and `PUBLISHED`; a batch with only excluded/completed reused items may be `COMPLETE`. Fact-check PASS/FAIL remains per article. All participating articles must have PASS and be `FACT_CHECKED` before image registration; all must be `IMAGE_READY` for review, `REVIEWED` for approval, and `APPROVED` for packages or publication registration. Image registration sets article states to `IMAGE_READY`; the batch aggregate is not a replacement for these member checks. Packaging sets batch `PACKAGED` but retains article `APPROVED`.

Actor/reason transitions, URLs, and publication timestamps commit for the whole active set together. JSON publication results require `schema_version: 1`, the batch ID, and exactly one HTTP(S) `article_url` per active article. Request status is `PENDING`, `COMPLETED`, or `SUPERSEDED`; immutable request/result hashes distinguish idempotent imports from stale or altered input.

Workspace migration also produces local file records outside SQLite: a deterministic preflight, verified SQLite/archive/manifest backups, journals, a persistent access barrier, retained recovery inventories, and a success receipt or verified rollback proof. The saved plan records every article's projected owner, including explicit nulls; the manifest, journal, and receipt bind its digest. Execute rejects any database or relationship change after preflight and applies inferred owners in the same transaction as path and `workspace_version` updates. These records also bind database identity, canonical paths, file types/permissions/hashes, and complete old trees. No editorial state is advanced by migration. Terminal access revalidates the projected owner identity and retained snapshots, while active material is allowed to evolve under ordinary batch gates. See [migration and recovery](WORKFLOW.md#9-旧工作区迁移与恢复) before handling these files; they are local recovery evidence, not generated dashboards.

## Identifiers and formats

Random IDs use `cand-...`, `art-...`, `batch-...`, `req-...`, `imgcand-...`, and `img-...`. `event_id` and slugs use lowercase ASCII with hyphens. Times are UTC ISO-8601. JSON model boundaries reject extra fields. CSV keywords and secondary sources use `|` separators.
