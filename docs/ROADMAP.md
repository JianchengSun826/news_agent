# Roadmap

## Phase 0 — Workflow Design (implemented)

Business rules, source and image policies, data model, prompt library, assumptions, and an offline acceptance demo are maintained with the executable workflow.

## Phase 1 — Human-in-the-loop MVP (implemented)

The local Typer CLI and SQLite database support candidates, deterministic deduplication, manual selection, versioned research/articles, fact-checking, image provenance, review, approval, publication packages, audit history, and publication registration without paid APIs.

## Phase 1.1 — Human-in-the-loop batches (current)

The supported desktop path is macOS Finder-first. Gemini performs discovery only through the reviewed CSV. Batch preparation reuses candidates, auto-selects deterministic `NEW` items, and creates one strict JSON request after ambiguous candidate decisions are complete. One Claude App conversation performs all post-import research, writing, and image search; a separate fresh conversation performs one independent batch fact-check pass with individual reports. `FAIL` articles enter a versioned revision loop and another independent check. All active articles must pass before registering one human-selected image per article, whole-batch review, approval, and staged publication packages. CMS upload and complete URL registration remain human gates.

SQLite remains canonical. Active files live in `workspace/batches/BATCH_ID/`; generated README/status views and eight Finder actions are rebuildable. New initialization creates no legacy runtime roots. Whole-batch validation and transactions prevent partial image registration, approval, and publication; package staging preserves prior packages on failure. Finder launchers use macOS dialogs and may briefly display Terminal. Windows/Linux Finder support, a native application, automatic inbox processing, and network/model integration are outside this phase.

The migration implementation includes read-only preflight, mandatory verified backup, staging, transactional path changes, durable access barriers, journals, rollback proof, and retained complete old trees. Real migration must first be rehearsed against copied data and explicitly confirmed; completing the implementation or synthetic demo is not real-workspace acceptance. Archives and recovery trees are preserved for human cleanup, never automatically deleted. See [WORKFLOW.md](WORKFLOW.md#9-旧工作区迁移与恢复).

Operate this workflow on real batches and record duplicate accuracy, Claude schema failures, fact-check failures, revision frequency, image-rights rejection reasons, and human editing time. State-changing post-selection content and release workflow commands are batch-only; supported read-only top-level commands such as `published list` and `stats` remain available. Legacy single-article workflow commands are removed, and service helpers remain internal recovery/building blocks. The offline acceptance demo uses synthetic requests/results, one complete image plan, and all batch release operations through two `PUBLISHED` articles without creating legacy artifact roots.

A Scheme A `Newsroom.app` prototype requires separate design approval and an isolated worktree using copied synthetic data only, after Scheme C Finder and migration acceptance. It is not part of the current implementation.

## Phase 2 — Semi-automated newsroom (do not start)

Possible work includes stronger entity clustering, URL metadata verification, eval datasets, and an optional Google Sheets adapter. Start only after Phase 1.1 has enough real operating evidence and an ADR defines scope, failure recovery, and the source of truth. Do not add model APIs, cloud databases, schedulers, queues, or automatic image downloads as part of current work.

## Phase 3 — Full agent (future)

Future provider adapters, scheduling, observability, cost tracking, automated discovery, and publishing require separate design and approval. Deterministic code and high-impact human gates remain the default.

## Exit criteria

Complete roughly 30 real articles across multiple batches; measure duplicate classifications, content import errors, PASS/FAIL outcomes, revisions, image-rights decisions, and final human edits. Use those results to decide the smallest justified Phase 2 change.
