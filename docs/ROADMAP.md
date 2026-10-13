# Roadmap

## Phase 0 — Workflow Design (implemented)

Business rules, source and image policies, data model, prompt library, and assumptions are maintained with the executable workflow.

## Phase 1 — Human-in-the-loop MVP (implemented)

The local Typer CLI and SQLite database support candidates, deterministic deduplication, manual selection, versioned research/articles, fact-checking, image provenance, review, approval, publication packages, audit history, and publication registration without paid APIs.

## Phase 1.1 — Human-in-the-loop batches (current)

The supported desktop path is macOS Finder-first. Gemini performs discovery only through the reviewed CSV. Batch preparation reuses candidates, auto-selects deterministic `NEW` items, and creates one strict, self-contained JSON request after ambiguous candidate decisions are complete. Editors manually open Claude and upload the current request linked in the batch README; the 01 launcher and mandatory repeated chat instruction are removed. One Claude App conversation performs all post-import research, writing, and image search; a separate fresh conversation performs one independent batch fact-check pass with individual reports. `FAIL` articles enter a versioned revision loop and another independent check. All active articles must pass before one human confirmation for whole-batch review and approval, staged content export, and automatic view refresh (Finder action 05 replaces the former 05–08). Images are handled manually after export: no Finder action 04 or per-article image/no-image decision is required. Each final article includes image-guide.md separating verified usable candidates (with license conditions) from recommendations awaiting clearance, including reasons and source links. Editors download and upload images to Google Drive themselves; existing registered images retain their validation. Candidate supplements preserve content/check versions, Finder reports outcomes and task paths, and reversible batch index archival changes visibility only. Local work is complete at PACKAGED when news content and image guides are exported. Action 05 creates a clean delivery folder under workspace/completed/BATCH_ID with per-slug article/image-guide files, plus any legacy registered image/license files, then removes the completed batch process tree after validating final bytes and switching SQLite references. Schema v5 stores CLEANING/FINAL_ONLY retention state and a durable deletion inventory; interruption resumes without repeating approval. Candidates, event metadata, fingerprints, rights and audit records remain for deterministic deduplication. Completed batches link to final deliverables, and refresh does not recreate process files. Unknown files added after cleanup starts and delivery edits block destructive retries. Existing completed batches opt in by explicit export; active batches, external originals, database backups and migration recovery data are retained. CMS upload and complete URL registration are optional human follow-ups.

SQLite remains canonical. Active files live in `workspace/batches/BATCH_ID/`, while finalized articles/images live in `workspace/completed/BATCH_ID/`; generated README/status views and three Finder actions (02, 03, 05) are rebuildable. New initialization creates no legacy runtime roots. Whole-batch validation and transactions prevent partial image registration, approval, and publication; package staging preserves prior packages on failure. Finder launchers use macOS dialogs and may briefly display Terminal. Windows/Linux Finder support, a native application, automatic inbox processing, and network/model integration are outside this phase.

The migration implementation includes read-only preflight, deterministic v3 ownership projection, mandatory verified backup, staging, transactional owner/path changes, durable access barriers, journals, rollback proof, and retained complete old trees. Reused and excluded relationships cannot claim an article; ambiguous or changed relationships block. Real migration must first be rehearsed against copied data and explicitly confirmed; completing the implementation is not real-workspace acceptance. Archives and recovery trees are preserved for human cleanup, never automatically deleted. See [WORKFLOW.md](WORKFLOW.md#9-旧工作区迁移与恢复).

Operate this workflow on real batches and record duplicate accuracy, Claude schema failures, fact-check failures, revision frequency, image-rights rejection reasons, and human editing time. State-changing post-selection content and release workflow commands are batch-only; supported read-only top-level commands such as `published list` and `stats` remain available. Legacy single-article workflow commands are removed, and service helpers remain internal recovery/building blocks.

A Scheme A `Newsroom.app` prototype requires separate design approval and an isolated worktree using copied synthetic data only, after Scheme C Finder and migration acceptance. It is not part of the current implementation.

## Phase 2 — Semi-automated newsroom (do not start)

Possible work includes stronger entity clustering, URL metadata verification, eval datasets, and an optional Google Sheets adapter. Start only after Phase 1.1 has enough real operating evidence and an ADR defines scope, failure recovery, and the source of truth. Do not add model APIs, cloud databases, schedulers, queues, or automatic image downloads as part of current work.

## Phase 3 — Full agent (future)

Future provider adapters, scheduling, observability, cost tracking, automated discovery, and publishing require separate design and approval. Deterministic code and high-impact human gates remain the default.

## Exit criteria

Complete roughly 30 real articles across multiple batches; measure duplicate classifications, content import errors, PASS/FAIL outcomes, revisions, image-rights decisions, and final human edits. Use those results to decide the smallest justified Phase 2 change.

图片核验补充（Phase 1.1）：内容返回可选 `image_recommendations`（每篇最多 3 条），独立于已核实 `image_candidates`。推荐包含来源 URL、可空直接链接/作者/权利线索、verification_status（PAGE_INACCESSIBLE / INSUFFICIENT_EVIDENCE / RESTRICTED_USE）、非空 verification_issue 和 relevance。原始结果归档为推荐事实来源；不新增 SQLite 表或改变 schema。工作区生成 image-recommendations.json/md 审核视图及一次性 manual-image-candidates.json 可编辑草稿。人工补全适用授权后通过现有候选补充命令加入，记录 actor/reason；默认不再执行 04；最终 image-guide.md 分类提供依据与原因，编辑导出后自行下载和上传。适用全站媒体政策可作为 rights_url，不要求独立逐图许可页。详见 IMAGE_POLICY.md。
