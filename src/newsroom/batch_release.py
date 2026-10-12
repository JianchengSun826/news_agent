"""Human-confirmed batch release actions with deterministic, local validation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from pydantic import ValidationError

from .batch_models import BatchImagePlan, BatchPublicationResult, NoImageSelection, BatchImageCandidateSupplement
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

    def review(self, batch_id: str, actor: str, reason: str) -> dict[str, Any]:
        return self._advance_batch_articles(batch_id, "IMAGE_READY", "REVIEWED", actor, reason, "reviewed")

    def approve(self, batch_id: str, actor: str, reason: str) -> dict[str, Any]:
        return self._advance_batch_articles(batch_id, "REVIEWED", "APPROVED", actor, reason, "approved")

    def export(self, batch_id: str, actor: str, reason: str) -> Path:
        from .coordination import operation_lock
        from .migration import assert_migration_access
        with operation_lock(self.room.root, exclusive=True):
            assert_migration_access(self.room.root)
            return self._export(batch_id, actor, reason)

    def _export(self, batch_id: str, actor: str, reason: str) -> Path:
        """Confirm review and approval together, then export local deliverables.

        Each completed gate remains durable if packaging fails; retry resumes
        from that gate without duplicating review or approval audit records.
        """
        self._require_actor_reason(actor, reason)
        from .retention import BatchRetention
        retention = BatchRetention(self.room)
        if 'batch_retention' not in self.room.db.table_names():
            raise WorkflowError('run newsroom init to upgrade the database before final cleanup')
        if retention.record(batch_id):
            target = retention.finalize(batch_id, actor, reason)
            self.workspace.refresh(batch_id)
            return target
        retention._inventory(self.workspace.batch_root(batch_id))
        retention._check_references(batch_id)
        from .delivery import CompletedDelivery
        delivery = CompletedDelivery(self.room)
        delivery.validate_target(batch_id)
        articles = self._active_articles(batch_id)
        statuses = {article["status"] for article in articles}
        if len(statuses) != 1 or not statuses <= {"IMAGE_READY", "REVIEWED", "APPROVED"}:
            raise WorkflowError("export requires every active article to be IMAGE_READY, REVIEWED or APPROVED at the same stage")
        root = self.workspace.batch_root(batch_id)
        publish = root / "publish"
        if not root.resolve().is_relative_to(self.room.root) or publish.is_symlink():
            raise WorkflowError("publish directory must remain within the newsroom and cannot be a symlink")
        if publish.exists() and not publish.is_dir():
            raise WorkflowError("publish path must be a directory")
        # Validate all current inputs before recording either human gate.
        for article in articles:
            if article["fact_check_outcome"] != "PASS":
                raise WorkflowError("export requires current PASS fact-check for every active article")
            canonical = self.room.get_article(article["article_id"])
            self.room._publication_materials({**canonical, "status": "APPROVED"})
        stage = next(iter(statuses))
        if stage == "IMAGE_READY":
            self.review(batch_id, actor, reason)
        if stage in {"IMAGE_READY", "REVIEWED"}:
            self.approve(batch_id, actor, reason)
        publish = self.build_packages(batch_id)
        target = delivery.build(batch_id, [self.room.get_article(article['article_id']) for article in articles], publish)
        retention.finalize(batch_id, actor, reason)
        self._refresh_workspaces(batch_id, articles)
        return target

    def _advance_batch_articles(self, batch_id: str, required: str, new: str, actor: str, reason: str, count_key: str) -> dict[str, Any]:
        articles = self._active_articles(batch_id)
        self._require_article_status(articles, required)
        self._require_actor_reason(actor, reason)
        for article in articles:
            if article['fact_check_outcome'] != 'PASS':
                raise WorkflowError('batch review and approval require current PASS fact-check')
            self.room.require_image_decision(article)
        with self.room.db.connect() as connection:
            now = utc_now()
            for article in articles:
                connection.execute("UPDATE articles SET status=?,updated_at=? WHERE article_id=?", (new, now, article["article_id"]))
                self.room._transition(connection, "article", article["article_id"], required, new, actor, reason)
            connection.execute("UPDATE batches SET status=?,updated_at=? WHERE batch_id=?", (new, now, batch_id))
        self._refresh_workspaces(batch_id, articles)
        return {"batch_id": batch_id, count_key: len(articles)}

    def build_packages(self, batch_id: str) -> Path:
        """Stage and validate the whole batch; compensate filesystem on DB failure."""
        from .retention import BatchRetention
        BatchRetention(self.room).require_process(batch_id)
        prior_batch = self.workspace._batch(batch_id)
        articles = self._active_articles(batch_id)
        self._require_article_status(articles, "APPROVED")
        # Read deserialized canonical metadata, including list-valued sources.
        articles = [self.room.get_article(article["article_id"]) for article in articles]
        materials = [(article, *self.room._publication_materials(article)) for article in articles]
        root = self.workspace.batch_root(batch_id)
        publish = root / "publish"
        if not root.resolve().is_relative_to(self.room.root) or publish.is_symlink():
            raise WorkflowError("publish directory must remain within the newsroom and cannot be a symlink")
        if publish.exists() and not publish.is_dir():
            raise WorkflowError("publish path must be a directory")
        staging: Path | None = None
        previous: Path | None = None
        previous_holder: Path | None = None
        try:
            staging = Path(tempfile.mkdtemp(prefix=".staging-publish-", dir=root))
            manifest_items = []
            for article, image, sources in materials:
                package = staging / article["article_id"]
                package.mkdir()
                self.room._write_publication_package(article, image, sources, package)
                self._validate_staged_package(package, article, image, sources)
                manifest_items.append({"article_id": article["article_id"], "revision_number": article["revision_number"], "package_path": article["article_id"]})
            manifest = {"schema_version": 1, "batch_id": batch_id, "items": manifest_items}
            manifest_path = staging / "batch-manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
                raise WorkflowError("staged batch manifest is incomplete")
            if publish.exists():
                previous_holder = Path(tempfile.mkdtemp(prefix=".previous-publish-", dir=root))
                previous = previous_holder / "publish"
                # os.replace cannot replace a nonempty directory; first move the
                # previous complete tree aside on the same filesystem.
                os.replace(publish, previous)
            os.replace(staging, publish)
            with self.room.db.connect() as connection:
                connection.execute("UPDATE batches SET status='PACKAGED',updated_at=? WHERE batch_id=?", (utc_now(), batch_id))
            # Retain the prior tree through the canonical update and view refresh.
            self._refresh_workspaces(batch_id, articles)
        except BaseException as original_error:
            compensation_errors: list[BaseException] = []
            # A rename or SQLite context exit can complete and then raise;
            # inspect the filesystem and restore the captured canonical values.
            promoted = staging is not None and not staging.exists()
            try:
                if promoted and publish.exists():
                    shutil.rmtree(publish)
                if previous is not None and previous.exists():
                    os.replace(previous, publish)
            except BaseException as error:
                compensation_errors.append(error)
            if promoted:
                try:
                    with self.room.db.connect() as connection:
                        connection.execute("BEGIN")
                        current = connection.execute(
                            "SELECT status,updated_at FROM batches WHERE batch_id=?", (batch_id,)
                        ).fetchone()
                        prior_values = (prior_batch["status"], prior_batch["updated_at"])
                        # A pre-commit rejection already rolled back. Avoid an
                        # unnecessary UPDATE that could retrigger the same guard.
                        if current is None or tuple(current) != prior_values:
                            connection.execute(
                                "UPDATE batches SET status=?,updated_at=? WHERE batch_id=?",
                                (*prior_values, batch_id),
                            )
                except BaseException as error:
                    compensation_errors.append(error)
            try:
                if staging is not None and staging.exists():
                    shutil.rmtree(staging)
                # Never delete the remaining backup if restoration failed.
                if previous_holder is not None and previous_holder.exists() and (previous is None or not previous.exists()):
                    shutil.rmtree(previous_holder)
            except BaseException as error:
                compensation_errors.append(error)
            if compensation_errors:
                raise BaseExceptionGroup(
                    "publication packaging failed; compensation incomplete",
                    [original_error, *compensation_errors],
                ) from None
            raise
        else:
            # Commit and refresh define success. Cleanup cannot reverse that
            # outcome; any leftover backup is noncanonical recovery data.
            try:
                if previous_holder is not None and previous_holder.exists():
                    shutil.rmtree(previous_holder)
            except BaseException:
                pass
        return publish

    @staticmethod
    def _validate_staged_package(package: Path, article: dict[str, Any], image: dict[str, Any] | None, sources: dict[str, Path]) -> None:
        for output, source in sources.items():
            filename = "hero" + source.suffix.lower() if output == "hero" else output
            path = package / filename
            if not path.is_file() or path.read_bytes() != source.read_bytes():
                raise WorkflowError(f"staged publication file is incomplete: {filename}")
        if image is None and (list(package.glob('hero*')) or (package / 'image-license.json').exists() or (package / 'image-brief.md').exists()):
            raise WorkflowError('no-image package contains image material')
        for filename, metadata in [("metadata.json", article), *([("image-license.json", image)] if image is not None else [])]:
            path = package / filename
            if not path.is_file() or json.loads(path.read_text(encoding="utf-8")) != metadata:
                raise WorkflowError(f"staged publication metadata is incomplete: {filename}")
        for filename in ("sources.md", "publication-checklist.md"):
            path = package / filename
            if not path.is_file() or not path.read_text(encoding="utf-8").strip():
                raise WorkflowError(f"staged publication file is incomplete: {filename}")

    def register_publications(self, batch_id: str, result_path: Path, actor: str) -> dict[str, Any]:
        articles = self._active_articles(batch_id)
        self._require_article_status(articles, "APPROVED")
        reason = "human confirmed external publication"
        self._require_actor_reason(actor, reason)
        try:
            result = BatchPublicationResult.model_validate_json(Path(result_path).read_bytes())
        except (OSError, ValidationError) as error:
            raise WorkflowError(f"invalid publication result: {error}") from error
        self._validate_membership(batch_id, result, articles, complete=True)
        with self.room.db.connect() as connection:
            now = utc_now()
            for item in result.items:
                connection.execute(
                    "UPDATE articles SET article_url=?,status='PUBLISHED',published_at=?,publish_date=?,updated_at=? WHERE article_id=?",
                    (str(item.article_url), now, now[:10], now, item.article_id),
                )
                self.room._transition(connection, "article", item.article_id, "APPROVED", "PUBLISHED", actor, reason)
            connection.execute("UPDATE batches SET status='PUBLISHED',updated_at=? WHERE batch_id=?", (now, batch_id))
        self._refresh_workspaces(batch_id, articles)
        return {"batch_id": batch_id, "published": len(result.items)}

    @staticmethod
    def _require_article_status(articles: list[dict[str, Any]], required: str) -> None:
        if not articles or any(article["status"] != required for article in articles):
            raise WorkflowError(f"batch action requires every active article to be {required}")

    @staticmethod
    def _require_actor_reason(actor: str, reason: str) -> None:
        if not actor.strip():
            raise WorkflowError("transition actor is required")
        if not reason.strip():
            raise WorkflowError("transition reason is required")

    def _refresh_workspaces(self, batch_id: str, articles: list[dict[str, Any]]) -> None:
        affected = {batch_id, *(article["owning_batch_id"] for article in articles if article["owning_batch_id"])}
        for affected_batch_id in sorted(affected):
            self.workspace.refresh(affected_batch_id)

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
                by_id = {article['article_id']: article for article in articles}
                for item in plan.items:
                    if isinstance(item, NoImageSelection):
                        connection.execute("UPDATE articles SET status='IMAGE_READY',updated_at=? WHERE article_id=?", (now, item.article_id))
                        self.room._transition(connection, 'image_choice', item.article_id,
                            f"v{by_id[item.article_id]['revision_number']}", 'NO_IMAGE', 'editor', item.reason)
                        self.room._transition(connection, 'article', item.article_id, 'FACT_CHECKED', 'IMAGE_READY', 'editor', 'batch explicit no-image choice: ' + item.reason)
        except BaseException:
            # Interruptions must clean promoted files just as SQLite rolls back.
            for path in reversed(promoted):
                path.unlink(missing_ok=True)
            raise
        finally:
            if staging is not None:
                shutil.rmtree(staging)
        self._refresh_workspaces(batch_id, articles)
        return {"batch_id": batch_id, "registered": len(images), "no_image": sum(isinstance(item, NoImageSelection) for item in plan.items), "items": images}

    def import_image_candidates(self, batch_id: str, path: Path, actor: str, reason: str) -> dict[str, Any]:
        """Append validated candidates without altering imported content or checks."""
        self._require_actor_reason(actor, reason)
        articles = self._active_articles(batch_id)
        try:
            payload = BatchImageCandidateSupplement.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            raise WorkflowError(f'invalid image candidate supplement: {error}') from error
        self._validate_membership(batch_id, payload, articles, complete=False)
        by_id = {a['article_id']: a for a in articles}
        for item in payload.items:
            article = by_id[item.article_id]
            if article['status'] not in {'DRAFTED', 'FACT_CHECKED', 'NEEDS_REVISION'} or article['image_id']:
                raise WorkflowError('candidate supplements require articles before image decisions')
            if not article['owning_batch_id']:
                raise WorkflowError('candidate supplements require a canonical owning batch')
        count = 0
        with self.room.db.connect() as connection:
            now = utc_now()
            for item in payload.items:
                for candidate in item.image_candidates:
                    values = candidate.model_dump(mode='json')
                    columns = ['landing_page_url', 'direct_image_url', 'creator', 'rights_basis', 'license_name',
                        'rights_url', 'rights_statement', 'attribution_required', 'attribution_text', 'relevance']
                    values['attribution_required'] = int(values['attribution_required'])
                    existing = connection.execute('SELECT * FROM image_candidates WHERE article_id=?', (item.article_id,)).fetchall()
                    if any(all(row[key] == values[key] for key in columns) for row in existing):
                        continue
                    connection.execute('INSERT INTO image_candidates(image_candidate_id,batch_id,article_id,'
                        + ','.join(columns) + ',created_at,updated_at) VALUES (' + ','.join('?' for _ in range(15)) + ')',
                        (new_id('imgcand'), by_id[item.article_id]['owning_batch_id'], item.article_id, *(values[key] for key in columns), now, now))
                    count += 1
                self.room._transition(connection, 'image_candidates', item.article_id, None, 'SUPPLEMENTED', actor, reason)
        self._refresh_workspaces(batch_id, articles)
        return {'batch_id': batch_id, 'added': count}

    def set_archived(self, batch_id: str, archived: bool, actor: str, reason: str) -> dict[str, Any]:
        """Hide a batch from the active index while retaining all canonical paths."""
        self.workspace._batch(batch_id)
        self._require_actor_reason(actor, reason)
        old = 'ARCHIVED' if self.room.batch_archived(batch_id) else 'VISIBLE'
        new = 'ARCHIVED' if archived else 'VISIBLE'
        if old != new:
            with self.room.db.connect() as connection:
                self.room._transition(connection, 'batch_visibility', batch_id, old, new, actor, reason)
        self.workspace.install_root_actions()
        return {'batch_id': batch_id, 'archived': archived, 'materials_retained': True}

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
    def _validate_membership(batch_id: str, plan: BatchImagePlan | BatchPublicationResult | BatchImageCandidateSupplement, articles: list[dict[str, Any]], *, complete: bool) -> None:
        if plan.batch_id != batch_id:
            raise WorkflowError("result batch_id does not match batch")
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
            if isinstance(item, NoImageSelection):
                continue
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
