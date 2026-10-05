# Data Model

SQLite schema version 3 is created or upgraded by `newsroom init`. Existing v1/v2 databases are copied to `data/backups/` before migration. SQLite remains canonical; CSV, JSON, and Markdown are interchange or artifact formats.

## Core records

`candidates` stores discovery metadata, normalized event fields, source URLs, duplicate state, selection state, audit timestamps, and `candidate_fingerprint`. The SHA-256 fingerprint canonicalizes event date, company, product, event type, core fact, and primary URL so a new CSV can reuse an old candidate.

`articles` links one candidate to at most one article. It stores workflow status, event metadata, current artifact paths, title/slug, `fact_check_outcome`, `revision_number`, selected `image_id`, publication data, and timestamps. Research, article, and fact-check files use `-vN.md`; current paths point to the active version.

`images` is the permanent publication record. Legacy manual/AI provenance remains supported. Batch-selected images additionally copy `image_candidate_id`, rights basis, rights statement, landing page, creator, license, rights URL, and attribution so a publication package does not depend on transient model output.

`sources` is the domain reliability registry. `state_transitions` is the append-only actor/reason audit log.

## Batch records

`batches` stores `batch_id`, original filename and byte SHA-256, aggregate status, latest content request ID, latest fact-check request ID, and timestamps. An identical file hash returns the existing batch.

`batch_items` links each batch candidate to its reused or new article. It records deterministic duplicate classification, batch-specific status, content version, and the latest actionable error. Important statuses are `WAITING_DUPLICATE`, `READY_FOR_CONTENT`, `CONTENT_IMPORTED`, `FACT_CHECKED`, `NEEDS_REVISION`, `REUSED_COMPLETE`, and `EXCLUDED`.

`batch_requests` is immutable handoff history. Each `req-...` stores type (`CONTENT`, `FACT_CHECK`, or `REVISION`), revision, request path, expected result filename, imported result path/hash, status, and timestamps. Identity plus exact membership prevents a stale or partial JSON from being applied.

`image_candidates` stores Claude search suggestions: article/batch IDs, landing and optional direct image URLs, creator, rights basis, license, original rights URL and statement, attribution, relevance, and review status. Allowed rights bases are `PUBLIC_DOMAIN`, `OPEN_LICENSE`, `OFFICIAL_PRESS_LICENSE`, and `OWNED`.

## Identifiers and formats

Random IDs use `cand-...`, `art-...`, `batch-...`, `req-...`, `imgcand-...`, and `img-...`. `event_id` and slugs use lowercase ASCII with hyphens. Times are UTC ISO-8601. JSON model boundaries reject extra fields. CSV keywords and secondary sources use `|` separators.
