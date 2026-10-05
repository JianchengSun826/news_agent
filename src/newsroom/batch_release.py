"""Human-confirmed batch release actions with deterministic, local validation."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from pydantic import ValidationError

from .batch_models import BatchImagePlan
from .models import new_id, utc_now
from .service import Newsroom, WorkflowError
from .workspace import BatchWorkspace, _atomic_write


@dataclass(frozen=True)
class _PreparedImage:
    article_id: str
    candidate: dict[str, Any]
    source: Path
    destination: Path
    owner_batch_id: str


class BatchReleaseWorkflow:
    def __init__(self, room: Newsroom):
        self.room = room
        self.workspace = BatchWorkspace(room)

    def save_image_draft(self, batch_id: str, plan: BatchImagePlan) -> Path:
        """Atomically save even incomplete selections without advancing workflow."""
        articles = self._active_articles(batch_id)
        try:
            plan = BatchImagePlan.model_validate(plan.model_dump(mode="json"))
        except ValidationError as error:
            raise WorkflowError(f"invalid image plan: {error}") from error
        self._validate_membership(batch_id, plan, articles, complete=False)
        path = self.workspace.image_draft_path(batch_id)
        _atomic_write(path, plan.model_dump_json(indent=2) + "\n")
        return path

    def load_image_draft(self, batch_id: str) -> BatchImagePlan:
        articles = self._active_articles(batch_id)
        plan = self._read_image_plan(self.workspace.image_draft_path(batch_id))
        self._validate_membership(batch_id, plan, articles, complete=False)
        return plan

    def apply_image_plan(self, batch_id: str, plan_path: Path) -> dict[str, Any]:
        """Validate and promote all local files before one SQLite transaction."""
        articles = self._active_articles(batch_id)
        if not articles or any(article["fact_check_outcome"] != "PASS" for article in articles):
            raise WorkflowError("all included articles must pass fact-check before image registration")
        if any(article["status"] != "FACT_CHECKED" or article["image_id"] for article in articles):
            raise WorkflowError("image registration requires FACT_CHECKED articles without registered images")
        plan = self._read_image_plan(Path(plan_path))
        self._validate_membership(batch_id, plan, articles, complete=True)
        prepared = self._prepare_images(plan, articles)
        promoted: list[Path] = []
        images: list[dict[str, str]] = []
        staging: Path | None = None
        try:
            staging = Path(tempfile.mkdtemp(prefix=".staging-images-", dir=self.workspace.batch_root(batch_id)))
            staged: list[tuple[_PreparedImage, Path]] = []
            for image in prepared:
                staged_path = staging / image.article_id / image.destination.name
                staged_path.parent.mkdir()
                shutil.copy2(image.source, staged_path)
                staged.append((image, staged_path))
            for image, staged_path in staged:
                image.destination.parent.mkdir(parents=True, exist_ok=True)
                # Same-filesystem linking promotes atomically and cannot overwrite
                # an existing destination, including one created during staging.
                os.link(staged_path, image.destination)
                promoted.append(image.destination)
            with self.room.db.connect() as connection:
                now = utc_now()
                for image in prepared:
                    image_id = new_id("img")
                    candidate = image.candidate
                    relative_path = image.destination.relative_to(self.room.root).as_posix()
                    connection.execute(
                        "INSERT INTO images(image_id,article_id,image_type,source_url,creator,license,"
                        "license_url,attribution_text,image_path,image_candidate_id,rights_basis,"
                        "rights_statement,downloaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (image_id, image.article_id, "LICENSED_SOURCE", candidate["landing_page_url"],
                         candidate["creator"], candidate["license_name"] or candidate["rights_basis"],
                         candidate["rights_url"], candidate["attribution_text"], relative_path,
                         candidate["image_candidate_id"], candidate["rights_basis"],
                         candidate["rights_statement"], now),
                    )
                    connection.execute(
                        "UPDATE image_candidates SET review_status='SELECTED',updated_at=? WHERE image_candidate_id=?",
                        (now, candidate["image_candidate_id"]),
                    )
                    connection.execute(
                        "UPDATE articles SET image_id=?,status='IMAGE_READY',updated_at=? WHERE article_id=?",
                        (image_id, now, image.article_id),
                    )
                    self.room._transition(
                        connection, "article", image.article_id, "FACT_CHECKED", "IMAGE_READY", "editor",
                        "batch downloaded image and original rights page confirmed",
                    )
                    images.append({"article_id": image.article_id, "image_id": image_id, "image_path": relative_path})
        except BaseException:
            # Interruptions must clean promoted files just as SQLite rolls back.
            for path in reversed(promoted):
                path.unlink(missing_ok=True)
            raise
        finally:
            if staging is not None:
                shutil.rmtree(staging)
        for affected_batch_id in sorted({batch_id, *(image.owner_batch_id for image in prepared)}):
            self.workspace.refresh(affected_batch_id)
        return {"batch_id": batch_id, "registered": len(images), "items": images}

    def _active_articles(self, batch_id: str) -> list[dict[str, Any]]:
        self.workspace._batch(batch_id)
        articles = self.room.db.all(
            "SELECT a.*,bi.item_status FROM batch_items bi LEFT JOIN articles a ON a.article_id=bi.article_id "
            "WHERE bi.batch_id=? AND bi.item_status NOT IN ('EXCLUDED','REUSED_COMPLETE') ORDER BY bi.candidate_id",
            (batch_id,),
        )
        return articles

    @staticmethod
    def _read_image_plan(path: Path) -> BatchImagePlan:
        try:
            return BatchImagePlan.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            raise WorkflowError(f"invalid image plan: {error}") from error

    @staticmethod
    def _validate_membership(batch_id: str, plan: BatchImagePlan, articles: list[dict[str, Any]], *, complete: bool) -> None:
        if plan.batch_id != batch_id:
            raise WorkflowError("image plan batch_id does not match batch")
        expected = {article["article_id"] for article in articles}
        actual = {item.article_id for item in plan.items}
        if actual - expected:
            raise WorkflowError(f"unexpected article IDs: {', '.join(sorted(actual - expected))}")
        if complete and expected - actual:
            raise WorkflowError(f"missing article IDs: {', '.join(sorted(expected - actual))}")

    def _prepare_images(self, plan: BatchImagePlan, articles: list[dict[str, Any]]) -> list[_PreparedImage]:
        by_id = {article["article_id"]: article for article in articles}
        prepared = []
        for item in plan.items:
            candidate = self.room.db.one("SELECT * FROM image_candidates WHERE image_candidate_id=?", (item.image_candidate_id,))
            if not candidate:
                raise WorkflowError(f"image candidate not found: {item.image_candidate_id}")
            if candidate["article_id"] != item.article_id:
                raise WorkflowError("image candidate belongs to a different article")
            if candidate["review_status"] == "SELECTED":
                raise WorkflowError("image candidate is already selected")
            source = item.image_file.expanduser().resolve()
            if not source.exists():
                raise WorkflowError(f"image file not found: {item.image_file}")
            if not source.is_file():
                raise WorkflowError(f"image file must be a local regular file: {item.image_file}")
            suffix = item.image_file.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]+", suffix, flags=re.ASCII):
                raise WorkflowError("image file requires a safe lowercase source suffix")
            owner = by_id[item.article_id]["owning_batch_id"]
            if not owner:
                raise WorkflowError(f"article has no canonical batch workspace: {item.article_id}")
            destination = self.workspace.article_root(owner, item.article_id) / f"hero{suffix}"
            if not destination.resolve().is_relative_to(self.room.root):
                raise WorkflowError("image destination must remain within the newsroom workspace")
            if destination.exists() or destination.is_symlink():
                raise WorkflowError(f"image destination already exists: {destination}")
            prepared.append(_PreparedImage(item.article_id, candidate, source, destination, owner))
        return prepared
