# Architecture

## Phase 1.1 components

`newsroom.cli` exposes Typer commands. `newsroom.batch.BatchWorkflow` coordinates durable CSV batches and manual Claude file handoffs. `newsroom.service.Newsroom` owns single-record state transitions, image registration, approval, and publication packages. Pydantic v2 models in `newsroom.models` and `newsroom.batch_models` validate CLI and JSON boundaries with unknown fields rejected. `newsroom.dedup` performs deterministic offline event comparison, and `newsroom.db` manages SQLite schema and migrations.

SQLite at `data/newsroom.db` is the canonical state store. Markdown files are versioned editorial artifacts. CSV is the Gemini-to-local boundary; JSON request/result files under `output/batches/BATCH_ID/` are the local-to-Claude boundary.

## Provider and trust boundaries

Gemini ends at the reviewed CSV. Claude App receives one content request for the whole eligible batch, then a separate fresh conversation receives one fact-check request. There is no model SDK or network call in the runtime. The application never downloads images, decides that a license is valid, or publishes to a CMS.

Every Claude result is validated for schema, request identity, exact article membership, slugs, and image rights metadata before writes begin. Multi-article imports use one SQLite transaction and staged files. Immutable `batch_requests` retain every content, fact-check, and revision exchange; a newer request ID invalidates stale results.

## Reliability and human gates

Database migration to schema v3 is additive and backs up a v1/v2 database before changes. SQL uses parameter binding, UTC ISO-8601 timestamps, and append-only state transitions. Content import preserves article versions. `FAIL` is stored as valid fact-check data and routes only that article to revision.

Humans remain responsible for Sheet source review, ambiguous duplicate decisions, Claude App upload/download, opening image landing and rights pages, downloading and visually inspecting the image, final review, approval, CMS upload, and publication URL registration.
