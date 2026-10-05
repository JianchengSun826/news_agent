from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .batch_models import BatchContentRequest, ContentRequestItem, candidate_fingerprint
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
            "content_request_path": (
                str(self.room.root / request["request_path"]) if request else None
            ),
            "items": items,
        }
