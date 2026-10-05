from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .batch_models import (
    BatchContentRequest,
    BatchContentResult,
    BatchFactCheckRequest,
    BatchFactCheckResult,
    BatchRevisionRequest,
    BatchRevisionResult,
    ContentRequestItem,
    FactCheckRequestItem,
    RevisionRequestItem,
    candidate_fingerprint,
)
from .models import CandidateInput, new_id, utc_now
from .service import Newsroom, WorkflowError


REQUIRED_CSV_FIELDS = {
    "candidate_title",
    "core_fact",
    "event_date",
    "company",
    "product",
    "topic",
    "subtopic",
    "event_type",
    "keywords",
    "primary_source",
    "secondary_sources",
    "discovered_from",
    "priority",
}


class BatchWorkflow:
    def __init__(self, room: Newsroom):
        self.room = room

    def prepare(self, csv_path: Path) -> dict[str, Any]:
        source = Path(csv_path)
        raw = source.read_bytes()
        source_sha256 = hashlib.sha256(raw).hexdigest()
        existing = self.room.db.one(
            "SELECT batch_id FROM batches WHERE source_sha256=?", (source_sha256,)
        )
        if existing:
            return self._summary(existing["batch_id"])

        rows = self._validated_rows(source)
        fingerprints = [candidate_fingerprint(row) for row in rows]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("candidate CSV contains duplicate candidate fingerprints")

        batch_id = new_id("batch")
        now = utc_now()
        with self.room.db.connect() as connection:
            connection.execute(
                "INSERT INTO batches(batch_id,source_filename,source_sha256,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (batch_id, source.name, source_sha256, "PREPARING", now, now),
            )

        for values, fingerprint in zip(rows, fingerprints, strict=True):
            candidate = self.room.find_candidate_by_fingerprint(fingerprint)
            if candidate is None:
                candidate = self.room.add_candidate(**values)
            article = self.room.db.one(
                "SELECT * FROM articles WHERE candidate_id=?", (candidate["candidate_id"],)
            )
            item_status, duplicate_classification = self._process_candidate(candidate, article)
            if article is None:
                article = self.room.db.one(
                    "SELECT * FROM articles WHERE candidate_id=?", (candidate["candidate_id"],)
                )
            item_now = utc_now()
            with self.room.db.connect() as connection:
                connection.execute(
                    "INSERT INTO batch_items(batch_id,candidate_id,article_id,item_status,"
                    "duplicate_classification,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        candidate["candidate_id"],
                        article["article_id"] if article else None,
                        item_status,
                        duplicate_classification,
                        item_now,
                        item_now,
                    ),
                )

        request_path = self._write_content_request(batch_id)
        self._refresh_batch_status(batch_id)
        summary = self._summary(batch_id)
        summary["content_request_path"] = str(request_path) if request_path else None
        return summary

    def status(self, batch_id: str) -> dict[str, Any]:
        return self._summary(batch_id)

    def import_content(self, batch_id: str, result_path: Path) -> dict[str, Any]:
        source = Path(result_path)
        raw = source.read_bytes()
        result_sha256 = hashlib.sha256(raw).hexdigest()
        payload = BatchContentResult.model_validate_json(raw)
        batch = self.room.db.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise WorkflowError(f"batch not found: {batch_id}")
        request_id = batch["content_request_id"]
        if not request_id:
            raise WorkflowError("batch has no content request")
        request_row = self.room.db.one(
            "SELECT * FROM batch_requests WHERE request_id=? AND request_type='CONTENT'",
            (request_id,),
        )
        if not request_row:
            raise WorkflowError(f"content request not found: {request_id}")
        if request_row["status"] == "COMPLETED":
            if request_row["result_sha256"] != result_sha256:
                raise WorkflowError("content request already completed with a different result")
            return self._content_import_summary(batch_id, request_id)
        if request_row["status"] != "PENDING":
            raise WorkflowError(f"content request is not open: {request_row['status']}")

        self._validate_result_identity(
            payload.batch_id,
            payload.request_id,
            payload.output_file,
            batch_id,
            request_row,
        )
        request_document = json.loads(
            (self.room.root / request_row["request_path"]).read_text(encoding="utf-8")
        )
        expected_ids = {item["article_id"] for item in request_document["items"]}
        actual_ids = {item.article_id for item in payload.items}
        self._validate_membership(expected_ids, actual_ids)
        self._validate_slugs(payload)

        batch_dir = self.room.root / "output" / "batches" / batch_id
        staging = batch_dir / f".staging-{request_id}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        for item in payload.items:
            item_dir = staging / item.article_id
            item_dir.mkdir()
            (item_dir / "research-brief-v1.md").write_text(
                item.research_brief_md.rstrip() + "\n", encoding="utf-8"
            )
            (item_dir / "article-v1.md").write_text(
                item.article_md.rstrip() + "\n", encoding="utf-8"
            )

        stored_result = batch_dir / f"{request_id}-content-result.json"
        created_paths: list[Path] = []
        fact_check_path: Path | None = None
        try:
            if stored_result.exists():
                raise WorkflowError(f"stored content result already exists: {stored_result}")
            shutil.copyfile(source, stored_result)
            created_paths.append(stored_result)
            with self.room.db.connect() as connection:
                for item in payload.items:
                    article = connection.execute(
                        "SELECT * FROM articles WHERE article_id=?", (item.article_id,)
                    ).fetchone()
                    if not article or article["status"] != "SELECTED":
                        current = article["status"] if article else "missing"
                        raise WorkflowError(
                            f"content import requires SELECTED article {item.article_id}; current status is {current}"
                        )
                    research_path = (
                        self.room.root
                        / "research"
                        / item.article_id
                        / "research-brief-v1.md"
                    )
                    article_path = (
                        self.room.root / "articles" / item.article_id / "article-v1.md"
                    )
                    research_path.parent.mkdir(parents=True, exist_ok=True)
                    article_path.parent.mkdir(parents=True, exist_ok=True)
                    if research_path.exists() or article_path.exists():
                        raise WorkflowError(f"version 1 artifacts already exist for {item.article_id}")
                    os.replace(staging / item.article_id / research_path.name, research_path)
                    created_paths.append(research_path)
                    os.replace(staging / item.article_id / article_path.name, article_path)
                    created_paths.append(article_path)
                    now = utc_now()
                    connection.execute(
                        "UPDATE articles SET status='RESEARCHED',research_brief_path=?,updated_at=? "
                        "WHERE article_id=?",
                        (str(research_path.relative_to(self.room.root)), now, item.article_id),
                    )
                    self.room._transition(
                        connection,
                        "article",
                        item.article_id,
                        "SELECTED",
                        "RESEARCHED",
                        "claude-app",
                        "batch research brief imported",
                    )
                    connection.execute(
                        "UPDATE articles SET status='DRAFTED',draft_path=?,title_cn=?,slug=?,updated_at=? "
                        "WHERE article_id=?",
                        (
                            str(article_path.relative_to(self.room.root)),
                            item.title_cn,
                            item.slug,
                            now,
                            item.article_id,
                        ),
                    )
                    self.room._transition(
                        connection,
                        "article",
                        item.article_id,
                        "RESEARCHED",
                        "DRAFTED",
                        "claude-app",
                        "batch article draft imported",
                    )
                    for image in item.image_candidates:
                        values = image.model_dump(mode="json")
                        image_candidate_id = new_id("imgcand")
                        connection.execute(
                            "INSERT INTO image_candidates(image_candidate_id,batch_id,article_id,"
                            "landing_page_url,direct_image_url,creator,rights_basis,license_name,"
                            "rights_url,rights_statement,attribution_required,attribution_text,"
                            "relevance,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                image_candidate_id,
                                batch_id,
                                item.article_id,
                                values["landing_page_url"],
                                values["direct_image_url"],
                                values["creator"],
                                values["rights_basis"],
                                values["license_name"],
                                values["rights_url"],
                                values["rights_statement"],
                                int(values["attribution_required"]),
                                values["attribution_text"],
                                values["relevance"],
                                now,
                                now,
                            ),
                        )
                    connection.execute(
                        "UPDATE batch_items SET item_status='CONTENT_IMPORTED',error_message=NULL,"
                        "updated_at=? WHERE batch_id=? AND article_id=?",
                        (now, batch_id, item.article_id),
                    )
                completed_at = utc_now()
                connection.execute(
                    "UPDATE batch_requests SET result_path=?,result_sha256=?,status='COMPLETED',"
                    "completed_at=? WHERE request_id=?",
                    (
                        str(stored_result.relative_to(self.room.root)),
                        result_sha256,
                        completed_at,
                        request_id,
                    ),
                )
                fact_check_path = self._write_fact_check_request(
                    connection, batch_id, sorted(expected_ids), revision_number=1
                )
                created_paths.append(fact_check_path)
                connection.execute(
                    "UPDATE batches SET status='WAITING_FACT_CHECK',updated_at=? WHERE batch_id=?",
                    (completed_at, batch_id),
                )
        except Exception:
            for path in reversed(created_paths):
                path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        return {
            "batch_id": batch_id,
            "request_id": request_id,
            "imported": len(payload.items),
            "fact_check_request_path": str(fact_check_path),
        }

    def import_fact_checks(self, batch_id: str, result_path: Path) -> dict[str, Any]:
        source = Path(result_path)
        raw = source.read_bytes()
        result_sha256 = hashlib.sha256(raw).hexdigest()
        payload = BatchFactCheckResult.model_validate_json(raw)
        batch = self.room.db.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise WorkflowError(f"batch not found: {batch_id}")
        request_id = batch["fact_check_request_id"]
        if not request_id:
            raise WorkflowError("batch has no fact-check request")
        request_row = self.room.db.one(
            "SELECT * FROM batch_requests WHERE request_id=? AND request_type='FACT_CHECK'",
            (request_id,),
        )
        if not request_row:
            raise WorkflowError(f"fact-check request not found: {request_id}")
        self._validate_result_identity(
            payload.batch_id,
            payload.request_id,
            payload.output_file,
            batch_id,
            request_row,
        )
        if request_row["status"] == "COMPLETED":
            if request_row["result_sha256"] != result_sha256:
                raise WorkflowError("fact-check request already completed with a different result")
            return self._fact_check_import_summary(batch_id, request_id)
        if request_row["status"] != "PENDING":
            raise WorkflowError(f"fact-check request is not open: {request_row['status']}")
        request_document = json.loads(
            (self.room.root / request_row["request_path"]).read_text(encoding="utf-8")
        )
        expected_ids = {item["article_id"] for item in request_document["items"]}
        actual_ids = {item.article_id for item in payload.items}
        self._validate_membership(expected_ids, actual_ids)

        batch_dir = self.room.root / "output" / "batches" / batch_id
        staging = batch_dir / f".staging-{request_id}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        request_revisions = {
            item["article_id"]: item["revision_number"] for item in request_document["items"]
        }
        for item in payload.items:
            (staging / f"{item.article_id}-fact-check.md").write_text(
                item.report_md.rstrip() + "\n", encoding="utf-8"
            )
        stored_result = batch_dir / f"{request_id}-fact-check-result.json"
        created_paths: list[Path] = []
        try:
            if stored_result.exists():
                raise WorkflowError(f"stored fact-check result already exists: {stored_result}")
            shutil.copyfile(source, stored_result)
            created_paths.append(stored_result)
            with self.room.db.connect() as connection:
                for item in payload.items:
                    article = connection.execute(
                        "SELECT * FROM articles WHERE article_id=?", (item.article_id,)
                    ).fetchone()
                    if not article or article["status"] != "DRAFTED":
                        current = article["status"] if article else "missing"
                        raise WorkflowError(
                            f"fact-check import requires DRAFTED article {item.article_id}; current status is {current}"
                        )
                    revision_number = request_revisions[item.article_id]
                    if article["revision_number"] != revision_number:
                        raise WorkflowError(
                            f"revision mismatch for {item.article_id}: expected {revision_number}"
                        )
                    report_path = (
                        self.room.root
                        / "articles"
                        / item.article_id
                        / f"fact-check-v{revision_number}.md"
                    )
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    if report_path.exists():
                        raise WorkflowError(
                            f"fact-check version {revision_number} already exists for {item.article_id}"
                        )
                    os.replace(staging / f"{item.article_id}-fact-check.md", report_path)
                    created_paths.append(report_path)
                    new_status = "FACT_CHECKED" if item.outcome == "PASS" else "NEEDS_REVISION"
                    now = utc_now()
                    connection.execute(
                        "UPDATE articles SET status=?,fact_check_path=?,fact_check_outcome=?,updated_at=? "
                        "WHERE article_id=?",
                        (
                            new_status,
                            str(report_path.relative_to(self.room.root)),
                            item.outcome,
                            now,
                            item.article_id,
                        ),
                    )
                    self.room._transition(
                        connection,
                        "article",
                        item.article_id,
                        "DRAFTED",
                        new_status,
                        "claude-app",
                        f"independent batch fact-check result: {item.outcome}",
                    )
                    connection.execute(
                        "UPDATE batch_items SET item_status=?,error_message=?,updated_at=? "
                        "WHERE batch_id=? AND article_id=?",
                        (
                            new_status,
                            "\n".join(item.blocking_issues) if item.blocking_issues else None,
                            now,
                            batch_id,
                            item.article_id,
                        ),
                    )
                completed_at = utc_now()
                connection.execute(
                    "UPDATE batch_requests SET result_path=?,result_sha256=?,status='COMPLETED',"
                    "completed_at=? WHERE request_id=?",
                    (
                        str(stored_result.relative_to(self.room.root)),
                        result_sha256,
                        completed_at,
                        request_id,
                    ),
                )
                failed = sum(item.outcome == "FAIL" for item in payload.items)
                batch_status = "NEEDS_REVISION" if failed else "READY_FOR_IMAGE"
                connection.execute(
                    "UPDATE batches SET status=?,updated_at=? WHERE batch_id=?",
                    (batch_status, completed_at, batch_id),
                )
        except Exception:
            for path in reversed(created_paths):
                path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return self._fact_check_import_summary(batch_id, request_id)

    def create_revision_request(self, batch_id: str) -> Path:
        batch = self.room.db.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise WorkflowError(f"batch not found: {batch_id}")
        failed_items = self.room.db.all(
            "SELECT bi.article_id,a.* FROM batch_items bi JOIN articles a ON a.article_id=bi.article_id "
            "WHERE bi.batch_id=? AND bi.item_status='NEEDS_REVISION' ORDER BY bi.article_id",
            (batch_id,),
        )
        if not failed_items:
            raise WorkflowError("batch has no articles needing revision")
        fact_request = self.room.db.one(
            "SELECT * FROM batch_requests WHERE request_id=? AND request_type='FACT_CHECK'",
            (batch["fact_check_request_id"],),
        )
        if not fact_request or not fact_request["result_path"]:
            raise WorkflowError("completed fact-check result is required for revision")
        fact_result = BatchFactCheckResult.model_validate_json(
            (self.room.root / fact_request["result_path"]).read_bytes()
        )
        findings = {item.article_id: item for item in fact_result.items}
        request_id = new_id("req")
        target_revision = max(row["revision_number"] + 1 for row in failed_items)
        output_file = f"{batch_id}-revision-result-v{target_revision}.json"
        instructions = (
            self.room.project_root / "prompts" / "claude_batch_revision.md"
        ).read_text(encoding="utf-8").strip()
        items = []
        for row in failed_items:
            finding = findings.get(row["article_id"])
            if not finding or not finding.blocking_issues:
                raise WorkflowError(f"blocking issues not found for {row['article_id']}")
            items.append(
                RevisionRequestItem(
                    article_id=row["article_id"],
                    revision_number=row["revision_number"] + 1,
                    title_cn=row["title_cn"],
                    slug=row["slug"],
                    research_brief_md=(self.room.root / row["research_brief_path"]).read_text(
                        encoding="utf-8"
                    ),
                    article_md=(self.room.root / row["draft_path"]).read_text(encoding="utf-8"),
                    fact_check_report_md=(self.room.root / row["fact_check_path"]).read_text(
                        encoding="utf-8"
                    ),
                    blocking_issues=finding.blocking_issues,
                )
            )
        request = BatchRevisionRequest(
            batch_id=batch_id,
            request_id=request_id,
            output_file=output_file,
            instructions=instructions,
            items=items,
        )
        batch_dir = self.room.root / "output" / "batches" / batch_id
        path = batch_dir / f"{request_id}-revision-request-v{target_revision}.json"
        path.write_text(
            json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        now = utc_now()
        try:
            with self.room.db.connect() as connection:
                pending = connection.execute(
                    "SELECT request_id FROM batch_requests WHERE batch_id=? "
                    "AND request_type='REVISION' AND status='PENDING'",
                    (batch_id,),
                ).fetchone()
                if pending:
                    raise WorkflowError(f"revision request already pending: {pending['request_id']}")
                connection.execute(
                    "INSERT INTO batch_requests(request_id,batch_id,request_type,revision_number,"
                    "request_path,expected_result_filename,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        request_id,
                        batch_id,
                        "REVISION",
                        target_revision,
                        str(path.relative_to(self.room.root)),
                        output_file,
                        "PENDING",
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE batches SET status='WAITING_REVISION',updated_at=? WHERE batch_id=?",
                    (now, batch_id),
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def import_revisions(self, batch_id: str, result_path: Path) -> dict[str, Any]:
        source = Path(result_path)
        raw = source.read_bytes()
        result_sha256 = hashlib.sha256(raw).hexdigest()
        payload = BatchRevisionResult.model_validate_json(raw)
        request_row = self.room.db.one(
            "SELECT * FROM batch_requests WHERE request_id=? AND batch_id=? "
            "AND request_type='REVISION'",
            (payload.request_id, batch_id),
        )
        if not request_row:
            raise WorkflowError("revision request_id does not match this batch")
        self._validate_result_identity(
            payload.batch_id,
            payload.request_id,
            payload.output_file,
            batch_id,
            request_row,
        )
        if request_row["status"] == "COMPLETED":
            if request_row["result_sha256"] != result_sha256:
                raise WorkflowError("revision request already completed with a different result")
            return self._revision_import_summary(batch_id, request_row)
        if request_row["status"] != "PENDING":
            raise WorkflowError(f"revision request is not open: {request_row['status']}")
        request_document = json.loads(
            (self.room.root / request_row["request_path"]).read_text(encoding="utf-8")
        )
        expected_ids = {item["article_id"] for item in request_document["items"]}
        actual_ids = {item.article_id for item in payload.items}
        self._validate_membership(expected_ids, actual_ids)
        self._validate_slugs(payload)
        target_revisions = {
            item["article_id"]: item["revision_number"] for item in request_document["items"]
        }

        batch_dir = self.room.root / "output" / "batches" / batch_id
        staging = batch_dir / f".staging-{payload.request_id}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        for item in payload.items:
            item_dir = staging / item.article_id
            item_dir.mkdir()
            (item_dir / "research.md").write_text(
                item.research_brief_md.rstrip() + "\n", encoding="utf-8"
            )
            (item_dir / "article.md").write_text(
                item.article_md.rstrip() + "\n", encoding="utf-8"
            )
        stored_result = batch_dir / f"{payload.request_id}-revision-result.json"
        created_paths: list[Path] = []
        fact_check_path: Path | None = None
        try:
            if stored_result.exists():
                raise WorkflowError(f"stored revision result already exists: {stored_result}")
            shutil.copyfile(source, stored_result)
            created_paths.append(stored_result)
            with self.room.db.connect() as connection:
                for item in payload.items:
                    article = connection.execute(
                        "SELECT * FROM articles WHERE article_id=?", (item.article_id,)
                    ).fetchone()
                    if not article or article["status"] != "NEEDS_REVISION":
                        current = article["status"] if article else "missing"
                        raise WorkflowError(
                            f"revision import requires NEEDS_REVISION article {item.article_id}; current status is {current}"
                        )
                    target_revision = target_revisions[item.article_id]
                    if target_revision != article["revision_number"] + 1:
                        raise WorkflowError(f"revision number mismatch for {item.article_id}")
                    research_path = (
                        self.room.root
                        / "research"
                        / item.article_id
                        / f"research-brief-v{target_revision}.md"
                    )
                    article_path = (
                        self.room.root
                        / "articles"
                        / item.article_id
                        / f"article-v{target_revision}.md"
                    )
                    research_path.parent.mkdir(parents=True, exist_ok=True)
                    article_path.parent.mkdir(parents=True, exist_ok=True)
                    if research_path.exists() or article_path.exists():
                        raise WorkflowError(
                            f"revision {target_revision} artifacts already exist for {item.article_id}"
                        )
                    os.replace(staging / item.article_id / "research.md", research_path)
                    created_paths.append(research_path)
                    os.replace(staging / item.article_id / "article.md", article_path)
                    created_paths.append(article_path)
                    now = utc_now()
                    connection.execute(
                        "UPDATE articles SET status='DRAFTED',research_brief_path=?,draft_path=?,"
                        "title_cn=?,slug=?,fact_check_outcome=NULL,revision_number=?,updated_at=? "
                        "WHERE article_id=?",
                        (
                            str(research_path.relative_to(self.room.root)),
                            str(article_path.relative_to(self.room.root)),
                            item.title_cn,
                            item.slug,
                            target_revision,
                            now,
                            item.article_id,
                        ),
                    )
                    self.room._transition(
                        connection,
                        "article",
                        item.article_id,
                        "NEEDS_REVISION",
                        "DRAFTED",
                        "claude-app",
                        f"batch revision {target_revision} imported",
                    )
                    connection.execute(
                        "UPDATE batch_items SET item_status='CONTENT_IMPORTED',content_version=?,"
                        "error_message=NULL,updated_at=? WHERE batch_id=? AND article_id=?",
                        (target_revision, now, batch_id, item.article_id),
                    )
                completed_at = utc_now()
                connection.execute(
                    "UPDATE batch_requests SET result_path=?,result_sha256=?,status='COMPLETED',"
                    "completed_at=? WHERE request_id=?",
                    (
                        str(stored_result.relative_to(self.room.root)),
                        result_sha256,
                        completed_at,
                        payload.request_id,
                    ),
                )
                fact_check_path = self._write_fact_check_request(
                    connection,
                    batch_id,
                    sorted(expected_ids),
                    revision_number=request_row["revision_number"],
                )
                created_paths.append(fact_check_path)
                connection.execute(
                    "UPDATE batches SET status='WAITING_FACT_CHECK',updated_at=? WHERE batch_id=?",
                    (completed_at, batch_id),
                )
        except Exception:
            for path in reversed(created_paths):
                path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {
            "batch_id": batch_id,
            "request_id": payload.request_id,
            "revised": len(payload.items),
            "fact_check_request_path": str(fact_check_path),
        }

    def resolve(
        self,
        batch_id: str,
        candidate_id: str,
        decision: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        if decision not in {"include", "exclude"}:
            raise WorkflowError("decision must be include or exclude")
        if not reason.strip():
            raise WorkflowError("resolution reason is required")
        item = self.room.db.one(
            "SELECT * FROM batch_items WHERE batch_id=? AND candidate_id=?",
            (batch_id, candidate_id),
        )
        if not item:
            raise WorkflowError(f"batch item not found: {batch_id}/{candidate_id}")
        if item["item_status"] != "WAITING_DUPLICATE":
            raise WorkflowError("duplicate resolution requires WAITING_DUPLICATE")

        classification = item["duplicate_classification"]
        if decision == "exclude":
            self.room.reject_candidate(candidate_id, actor, reason)
            article_id = None
            item_status = "EXCLUDED"
        else:
            if classification in {"POSSIBLE_DUPLICATE", "DUPLICATE"}:
                raise WorkflowError(
                    "duplicate override is required before including POSSIBLE_DUPLICATE or DUPLICATE"
                )
            self.room.select_candidate(candidate_id, actor, reason)
            article = self.room.create_brief(candidate_id)
            article_id = article["article_id"]
            item_status = "READY_FOR_CONTENT"

        with self.room.db.connect() as connection:
            connection.execute(
                "UPDATE batch_items SET article_id=?,item_status=?,error_message=NULL,updated_at=? "
                "WHERE batch_id=? AND candidate_id=?",
                (article_id, item_status, utc_now(), batch_id, candidate_id),
            )
        if decision == "include":
            self._write_content_request(batch_id)
        self._refresh_batch_status(batch_id)
        return self._summary(batch_id)

    def _validated_rows(self, source: Path) -> list[dict[str, Any]]:
        with source.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = REQUIRED_CSV_FIELDS - set(reader.fieldnames or [])
            if missing:
                raise ValueError(
                    f"candidate CSV missing columns: {', '.join(sorted(missing))}"
                )
            rows: list[dict[str, Any]] = []
            for line_number, row in enumerate(reader, start=2):
                try:
                    parsed = CandidateInput.model_validate(
                        {
                            "candidate_title": row["candidate_title"],
                            "core_fact": row["core_fact"],
                            "event_date": row["event_date"],
                            "company": row["company"],
                            "product": row["product"],
                            "topic": row["topic"],
                            "subtopic": row["subtopic"],
                            "event_type": row["event_type"],
                            "keywords": self._split_pipe(row["keywords"]),
                            "primary_source": row["primary_source"],
                            "secondary_sources": self._split_pipe(row["secondary_sources"]),
                            "discovered_from": row["discovered_from"],
                            "priority": row["priority"],
                        }
                    )
                except ValueError as error:
                    raise ValueError(f"invalid candidate CSV row {line_number}: {error}") from error
                rows.append(parsed.model_dump(mode="json"))
        if not rows:
            raise ValueError("candidate CSV contains no rows")
        return rows

    @staticmethod
    def _split_pipe(value: str | None) -> list[str]:
        return [item.strip() for item in (value or "").split("|") if item.strip()]

    def _process_candidate(
        self, candidate: dict[str, Any], article: dict[str, Any] | None
    ) -> tuple[str, str | None]:
        if article:
            if article["status"] == "SELECTED":
                return "READY_FOR_CONTENT", candidate["duplicate_status"]
            return "REUSED_COMPLETE", candidate["duplicate_status"]
        if candidate["status"] == "REJECTED":
            return "EXCLUDED", candidate["duplicate_status"]
        if candidate["status"] == "SELECTED":
            self.room.create_brief(candidate["candidate_id"])
            return "READY_FOR_CONTENT", candidate["duplicate_status"]
        if candidate["status"] == "CANDIDATE":
            duplicate = self.room.check_duplicate(candidate["candidate_id"])
            candidate = self.room.get_candidate(candidate["candidate_id"])
        elif candidate["status"] == "DUPLICATE_CHECKED":
            duplicate = {"classification": candidate["duplicate_status"]}
        else:
            return "REUSED_COMPLETE", candidate["duplicate_status"]
        classification = duplicate["classification"]
        if classification != "NEW":
            return "WAITING_DUPLICATE", classification
        self.room.select_candidate(
            candidate["candidate_id"], "system", "batch auto-selected deterministic NEW candidate"
        )
        self.room.create_brief(candidate["candidate_id"])
        return "READY_FOR_CONTENT", classification

    def _write_content_request(self, batch_id: str) -> Path | None:
        items = self.room.db.all(
            "SELECT bi.article_id,c.* FROM batch_items bi "
            "JOIN candidates c ON c.candidate_id=bi.candidate_id "
            "WHERE bi.batch_id=? AND bi.item_status='READY_FOR_CONTENT' "
            "ORDER BY bi.created_at,bi.candidate_id",
            (batch_id,),
        )
        if not items:
            return None
        request_id = new_id("req")
        output_file = f"{batch_id}-content-result.json"
        instructions = (
            self.room.project_root / "prompts" / "claude_batch_content.md"
        ).read_text(encoding="utf-8").strip()
        request = BatchContentRequest(
            batch_id=batch_id,
            request_id=request_id,
            output_file=output_file,
            instructions=instructions,
            items=[
                ContentRequestItem(
                    article_id=row["article_id"],
                    candidate_id=row["candidate_id"],
                    event_id=row["event_id"],
                    event_date=row["event_date"],
                    candidate_title=row["candidate_title"],
                    core_fact=row["core_fact"],
                    company=row["company"],
                    product=row["product"],
                    topic=row["topic"],
                    subtopic=row["subtopic"],
                    event_type=row["event_type"],
                    keywords=json.loads(row["keywords"]),
                    primary_source=row["primary_source"],
                    secondary_sources=json.loads(row["secondary_sources"]),
                )
                for row in items
            ],
        )
        batch_dir = self.room.root / "output" / "batches" / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        path = batch_dir / f"{request_id}-content-request.json"
        path.write_text(
            json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        now = utc_now()
        with self.room.db.connect() as connection:
            connection.execute(
                "UPDATE batch_requests SET status='SUPERSEDED' "
                "WHERE batch_id=? AND request_type='CONTENT' AND status='PENDING'",
                (batch_id,),
            )
            connection.execute(
                "INSERT INTO batch_requests(request_id,batch_id,request_type,revision_number,"
                "request_path,expected_result_filename,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    batch_id,
                    "CONTENT",
                    1,
                    str(path.relative_to(self.room.root)),
                    output_file,
                    "PENDING",
                    now,
                ),
            )
            connection.execute(
                "UPDATE batches SET content_request_id=?,updated_at=? WHERE batch_id=?",
                (request_id, now, batch_id),
            )
        return path

    def _write_fact_check_request(
        self,
        connection,
        batch_id: str,
        article_ids: list[str],
        revision_number: int,
    ) -> Path:
        request_id = new_id("req")
        output_file = f"{batch_id}-fact-check-result-v{revision_number}.json"
        instructions = (
            self.room.project_root / "prompts" / "claude_batch_fact_check.md"
        ).read_text(encoding="utf-8").strip()
        items: list[FactCheckRequestItem] = []
        for article_id in article_ids:
            row = connection.execute(
                "SELECT * FROM articles WHERE article_id=?", (article_id,)
            ).fetchone()
            if not row:
                raise WorkflowError(f"article not found: {article_id}")
            research_path = self.room.root / row["research_brief_path"]
            article_path = self.room.root / row["draft_path"]
            items.append(
                FactCheckRequestItem(
                    article_id=article_id,
                    revision_number=row["revision_number"],
                    title_cn=row["title_cn"],
                    research_brief_md=research_path.read_text(encoding="utf-8"),
                    article_md=article_path.read_text(encoding="utf-8"),
                    primary_source=row["primary_source"],
                    secondary_sources=json.loads(row["secondary_sources"]),
                )
            )
        request = BatchFactCheckRequest(
            batch_id=batch_id,
            request_id=request_id,
            output_file=output_file,
            instructions=instructions,
            items=items,
        )
        batch_dir = self.room.root / "output" / "batches" / batch_id
        path = batch_dir / f"{request_id}-fact-check-request-v{revision_number}.json"
        path.write_text(
            json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        now = utc_now()
        connection.execute(
            "INSERT INTO batch_requests(request_id,batch_id,request_type,revision_number,"
            "request_path,expected_result_filename,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                request_id,
                batch_id,
                "FACT_CHECK",
                revision_number,
                str(path.relative_to(self.room.root)),
                output_file,
                "PENDING",
                now,
            ),
        )
        connection.execute(
            "UPDATE batches SET fact_check_request_id=?,updated_at=? WHERE batch_id=?",
            (request_id, now, batch_id),
        )
        return path

    @staticmethod
    def _validate_result_identity(
        actual_batch_id: str,
        actual_request_id: str,
        actual_output_file: str,
        batch_id: str,
        request_row: dict[str, Any],
    ) -> None:
        if actual_batch_id != batch_id:
            raise WorkflowError("result batch_id does not match the requested batch")
        if actual_request_id != request_row["request_id"]:
            raise WorkflowError("result request_id does not match the open request")
        if actual_output_file != request_row["expected_result_filename"]:
            raise WorkflowError("result output_file does not match the requested filename")

    @staticmethod
    def _validate_membership(expected_ids: set[str], actual_ids: set[str]) -> None:
        missing = sorted(expected_ids - actual_ids)
        unexpected = sorted(actual_ids - expected_ids)
        if missing:
            raise WorkflowError(f"missing article IDs: {', '.join(missing)}")
        if unexpected:
            raise WorkflowError(f"unexpected article IDs: {', '.join(unexpected)}")

    def _validate_slugs(self, payload: BatchContentResult) -> None:
        slugs = [item.slug for item in payload.items]
        if len(slugs) != len(set(slugs)):
            raise WorkflowError("duplicate slug in content result")
        article_ids = [item.article_id for item in payload.items]
        placeholders = ",".join("?" for _ in article_ids)
        existing = self.room.db.one(
            f"SELECT slug FROM articles WHERE slug IN ({','.join('?' for _ in slugs)}) "
            f"AND article_id NOT IN ({placeholders}) LIMIT 1",
            (*slugs, *article_ids),
        )
        if existing:
            raise WorkflowError(f"duplicate slug already exists: {existing['slug']}")

    def _content_import_summary(self, batch_id: str, request_id: str) -> dict[str, Any]:
        batch = self.room.db.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        fact_request = self.room.db.one(
            "SELECT request_path FROM batch_requests WHERE request_id=?",
            (batch["fact_check_request_id"],),
        )
        count = self.room.db.one(
            "SELECT COUNT(*) AS count FROM batch_items WHERE batch_id=? "
            "AND item_status IN ('CONTENT_IMPORTED','FACT_CHECKED','NEEDS_REVISION')",
            (batch_id,),
        )
        return {
            "batch_id": batch_id,
            "request_id": request_id,
            "imported": count["count"],
            "fact_check_request_path": (
                str(self.room.root / fact_request["request_path"]) if fact_request else None
            ),
        }

    def _fact_check_import_summary(self, batch_id: str, request_id: str) -> dict[str, Any]:
        rows = self.room.db.all(
            "SELECT item_status,COUNT(*) AS count FROM batch_items WHERE batch_id=? "
            "GROUP BY item_status",
            (batch_id,),
        )
        counts = {row["item_status"]: row["count"] for row in rows}
        return {
            "batch_id": batch_id,
            "request_id": request_id,
            "passed": counts.get("FACT_CHECKED", 0),
            "needs_revision": counts.get("NEEDS_REVISION", 0),
        }

    def _revision_import_summary(
        self, batch_id: str, request_row: dict[str, Any]
    ) -> dict[str, Any]:
        batch = self.room.db.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        fact_request = self.room.db.one(
            "SELECT request_path FROM batch_requests WHERE request_id=?",
            (batch["fact_check_request_id"],),
        )
        request_document = json.loads(
            (self.room.root / request_row["request_path"]).read_text(encoding="utf-8")
        )
        return {
            "batch_id": batch_id,
            "request_id": request_row["request_id"],
            "revised": len(request_document["items"]),
            "fact_check_request_path": (
                str(self.room.root / fact_request["request_path"]) if fact_request else None
            ),
        }

    def _refresh_batch_status(self, batch_id: str) -> None:
        counts = Counter(
            row["item_status"]
            for row in self.room.db.all(
                "SELECT item_status FROM batch_items WHERE batch_id=?", (batch_id,)
            )
        )
        if counts.get("WAITING_DUPLICATE"):
            status = "WAITING_DUPLICATE"
        elif counts.get("READY_FOR_CONTENT"):
            status = "READY_FOR_CONTENT"
        else:
            status = "COMPLETE"
        with self.room.db.connect() as connection:
            connection.execute(
                "UPDATE batches SET status=?,updated_at=? WHERE batch_id=?",
                (status, utc_now(), batch_id),
            )

    def _summary(self, batch_id: str) -> dict[str, Any]:
        batch = self.room.db.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise WorkflowError(f"batch not found: {batch_id}")
        items = self.room.db.all(
            "SELECT bi.*,c.candidate_title,c.event_date,c.company,c.product,a.status AS article_status "
            "FROM batch_items bi JOIN candidates c ON c.candidate_id=bi.candidate_id "
            "LEFT JOIN articles a ON a.article_id=bi.article_id "
            "WHERE bi.batch_id=? ORDER BY bi.created_at,bi.candidate_id",
            (batch_id,),
        )
        counts = dict(Counter(item["item_status"] for item in items))
        actions = {
            "waiting_duplicate_candidate_ids": [
                item["candidate_id"]
                for item in items
                if item["item_status"] == "WAITING_DUPLICATE"
            ],
            "needs_revision_article_ids": [
                item["article_id"]
                for item in items
                if item["item_status"] == "NEEDS_REVISION"
            ],
            "image_pending_article_ids": [
                item["article_id"]
                for item in items
                if item["item_status"] == "FACT_CHECKED"
            ],
        }
        request = None
        if batch["content_request_id"]:
            request = self.room.db.one(
                "SELECT request_path FROM batch_requests WHERE request_id=?",
                (batch["content_request_id"],),
            )
        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "source_filename": batch["source_filename"],
            "source_sha256": batch["source_sha256"],
            "counts": counts,
            "action_required": actions,
            "content_request_path": (
                str(self.room.root / request["request_path"]) if request else None
            ),
            "items": items,
        }
