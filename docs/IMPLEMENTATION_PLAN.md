# Phase 0–1 Implementation Plan

**Goal:** Deliver a local, human-in-the-loop newsroom workflow that records sources, decisions, artifacts, approval, and publication packages without paid APIs.

**Architecture:** A single Python package exposes a Typer CLI with Pydantic v2 input models. SQLite is the canonical store; Markdown/JSON/CSV files are human-facing interchange and artifacts. Workflow rules are deterministic and every state change is appended to an audit table.

**Tech Stack:** Python 3.12+, SQLite, Pydantic v2, Typer, pytest, uv.

## Global Constraints

- Implement Phase 0 and Phase 1 only.
- Preserve the SELECTED and APPROVED human gates.
- Never publish externally or require an API key.
- Never mark an image ready without a recorded license or AI provenance.
- Every important transition and duplicate override requires an audit reason.

## Review Focus

- Invalid or skipped state transitions must fail without changing stored state.
- Duplicate checks must use event attributes and allow a reasoned human override.
- Candidate sources must be URLs and approval must require primary plus independent sources.
- Image records with unclear rights must block `IMAGE_READY` and approval.
- Publication packages must be reproducible and must not silently overwrite an existing package.

## Tasks

1. Document the workflow, policies, architecture, data model, assumptions, and roadmap. Verify internal links and scope statements manually.
2. Write failing tests for IDs, workflow transitions, duplicate classifications, source and image validation, approval, and package generation. Run `pytest` and confirm missing-module failures.
3. Implement SQLite initialization, repositories, validators, and workflow services. Run `pytest` until the domain tests pass.
4. Write failing CLI integration tests for initialization and a complete article path. Implement the unified CLI and rerun the full suite.
5. Add versioned prompt templates, configuration, example candidates, and the demo script. Run the full suite and `bash scripts/demo.sh`; inspect the database and generated package.

## Assumptions Recorded by This Plan

The MVP requires Python 3.12+. Typer provides the command tree and Pydantic v2 validates boundary inputs. CSV is the Phase 1 spreadsheet interchange format. Human inputs are Markdown files and URLs; the system stores metadata rather than third-party article bodies.
