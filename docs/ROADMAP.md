# Roadmap

## Phase 0 — Workflow Design (implemented)

Business rules, source and image policies, data model, prompt library, assumptions, and an offline acceptance demo are maintained with the executable workflow.

## Phase 1 — Human-in-the-loop MVP (implemented)

The local Typer CLI and SQLite database support candidates, deterministic deduplication, manual selection, versioned research/articles, fact-checking, image provenance, review, approval, publication packages, audit history, and publication registration without paid APIs.

## Phase 1.1 — Human-in-the-loop batches (current)

Gemini performs discovery only through the reviewed CSV. The CLI reuses candidates, auto-selects deterministic `NEW` items, and creates one strict JSON request for a whole batch. One Claude App conversation performs research, writing, and image search; a separate conversation performs one independent batch fact-check. `FAIL` articles enter a versioned revision loop. Image download, rights confirmation, approval, and publishing remain human gates.

Operate this workflow on real batches and record duplicate accuracy, Claude schema failures, fact-check failures, revision frequency, image-rights rejection reasons, and human editing time. The supported post-selection CLI interface is batch-only; underlying single-article service helpers remain internal recovery and building blocks.

## Phase 2 — Semi-automated newsroom (do not start)

Possible work includes stronger entity clustering, URL metadata verification, eval datasets, and an optional Google Sheets adapter. Start only after Phase 1.1 has enough real operating evidence and an ADR defines scope, failure recovery, and the source of truth. Do not add model APIs, cloud databases, schedulers, queues, or automatic image downloads as part of current work.

## Phase 3 — Full agent (future)

Future provider adapters, scheduling, observability, cost tracking, automated discovery, and publishing require separate design and approval. Deterministic code and high-impact human gates remain the default.

## Exit criteria

Complete roughly 30 real articles across multiple batches; measure duplicate classifications, content import errors, PASS/FAIL outcomes, revisions, image-rights decisions, and final human edits. Use those results to decide the smallest justified Phase 2 change.
