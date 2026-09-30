from __future__ import annotations

import json
import shutil
import csv
import tomllib
from pydantic import ValidationError
from pathlib import Path
from typing import Any

from .db import Database
from .dedup import classify_candidate
from .models import CandidateInput, ImageInput, make_event_id, new_id, require_url, utc_now


class WorkflowError(ValueError):
    pass


class Newsroom:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.db_path = self.root / "data" / "newsroom.db"
        self.db = Database(self.db_path)

    def init(self) -> None:
        for name in ("data", "research", "articles", "images", "output"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.db.initialize(utc_now())
        self._seed_sources()

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _seed_sources(self) -> None:
        config = self.project_root / "config" / "sources.toml"
        if not config.exists():
            return
        sources = tomllib.loads(config.read_text(encoding="utf-8")).get("sources", [])
        with self.db.connect() as connection:
            for source in sources:
                connection.execute(
                    """INSERT INTO sources(source_id,source_name,domain,source_type,reliability_level,usage_policy,notes,active)
                    VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(domain) DO NOTHING""",
                    (source["source_id"],source["source_name"],source["domain"],source["source_type"],source["reliability_level"],source["usage_policy"],source.get("notes", "")),
                )

    def _transition(self, connection, entity_type: str, entity_id: str, old: str | None, new: str, actor: str, reason: str) -> None:
        if not reason.strip():
            raise WorkflowError("transition reason is required")
        connection.execute(
            "INSERT INTO state_transitions(entity_type, entity_id, from_status, to_status, actor, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity_type, entity_id, old, new, actor, reason, utc_now()),
        )

    def add_candidate(self, **values: Any) -> dict[str, Any]:
        values = CandidateInput.model_validate(values).model_dump(mode="json")
        now, candidate_id = utc_now(), new_id("cand")
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO candidates(candidate_id,status,discovered_at,event_date,candidate_title,core_fact,company,product,topic,subtopic,event_type,keywords,primary_source,secondary_sources,discovered_from,priority,notes,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, "CANDIDATE", now, values["event_date"], values["candidate_title"], values["core_fact"], values["company"], values["product"], values["topic"], values["subtopic"], values["event_type"], json.dumps(values["keywords"], ensure_ascii=False), values["primary_source"], json.dumps(values["secondary_sources"], ensure_ascii=False), values["discovered_from"], values["priority"], values.get("notes", ""), now, now),
            )
            self._transition(connection, "candidate", candidate_id, None, "DISCOVERED", "system", "candidate discovered")
            self._transition(connection, "candidate", candidate_id, "DISCOVERED", "CANDIDATE", "system", "candidate recorded")
        return self.get_candidate(candidate_id)

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,))
        if not row:
            raise WorkflowError(f"candidate not found: {candidate_id}")
        row["keywords"] = json.loads(row["keywords"])
        row["secondary_sources"] = json.loads(row["secondary_sources"])
        return row

    def list_candidates(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT candidate_id,status,candidate_title,event_date,company,product,duplicate_status,priority FROM candidates ORDER BY priority DESC, discovered_at")

    def import_candidates(self, path: Path) -> list[str]:
        required = {"candidate_title","core_fact","event_date","company","product","topic","subtopic","event_type","keywords","primary_source","secondary_sources","discovered_from","priority"}
        created: list[str] = []
        with Path(path).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"candidate CSV missing columns: {', '.join(sorted(missing))}")
            for row in reader:
                candidate = self.add_candidate(
                    candidate_title=row["candidate_title"], core_fact=row["core_fact"], event_date=row["event_date"],
                    company=row["company"], product=row["product"], topic=row["topic"], subtopic=row["subtopic"],
                    event_type=row["event_type"], keywords=[item.strip() for item in row["keywords"].split("|") if item.strip()],
                    primary_source=row["primary_source"], secondary_sources=[item.strip() for item in row["secondary_sources"].split("|") if item.strip()],
                    discovered_from=row["discovered_from"], priority=int(row["priority"]),
                )
                created.append(candidate["candidate_id"])
        return created

    def export_candidates(self, path: Path) -> int:
        fields = ["candidate_id","status","candidate_title","core_fact","event_date","company","product","topic","subtopic","event_type","keywords","primary_source","secondary_sources","discovered_from","priority","duplicate_status","matching_event_id","event_id","notes"]
        rows = self.db.all("SELECT " + ",".join(fields) + " FROM candidates ORDER BY created_at")
        target = Path(path)
        if target.exists():
            raise FileExistsError(f"export already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                row["keywords"] = "|".join(json.loads(row["keywords"]))
                row["secondary_sources"] = "|".join(json.loads(row["secondary_sources"]))
                writer.writerow(row)
        return len(rows)

    def check_duplicate(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        if candidate["status"] not in {"CANDIDATE", "DUPLICATE_CHECKED"}:
            raise WorkflowError("duplicate check requires CANDIDATE or DUPLICATE_CHECKED")
        events = self.db.all("SELECT event_id,event_date,company,product,event_type,core_fact,keywords FROM articles WHERE event_id IS NOT NULL UNION ALL SELECT event_id,event_date,company,product,event_type,core_fact,keywords FROM candidates WHERE candidate_id<>? AND event_id IS NOT NULL", (candidate_id,))
        result = classify_candidate(candidate, events)
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("UPDATE candidates SET status='DUPLICATE_CHECKED',duplicate_status=?,matching_event_id=?,updated_at=? WHERE candidate_id=?", (result.classification, result.matching_event_id, now, candidate_id))
            if candidate["status"] != "DUPLICATE_CHECKED":
                self._transition(connection, "candidate", candidate_id, candidate["status"], "DUPLICATE_CHECKED", "system", result.explanation)
        return {"classification": result.classification, "matching_event_id": result.matching_event_id, "score": result.score, "explanation": result.explanation}

    def override_duplicate(self, candidate_id: str, classification: str, reason: str, actor: str) -> None:
        allowed = {"NEW", "FOLLOW_UP", "RELATED", "POSSIBLE_DUPLICATE", "DUPLICATE"}
        if classification not in allowed or not reason.strip():
            raise WorkflowError("valid duplicate classification and reason are required")
        candidate = self.get_candidate(candidate_id)
        if candidate["status"] != "DUPLICATE_CHECKED":
            raise WorkflowError("duplicate override requires DUPLICATE_CHECKED")
        with self.db.connect() as connection:
            connection.execute("UPDATE candidates SET duplicate_status=?,updated_at=? WHERE candidate_id=?", (classification, utc_now(), candidate_id))
            self._transition(connection, "candidate", candidate_id, "DUPLICATE_CHECKED", "DUPLICATE_CHECKED", actor, f"duplicate override to {classification}: {reason}")

    def select_candidate(self, candidate_id: str, actor: str, reason: str) -> None:
        candidate = self.get_candidate(candidate_id)
        if candidate["status"] != "DUPLICATE_CHECKED":
            raise WorkflowError("selection requires DUPLICATE_CHECKED")
        if candidate["duplicate_status"] in {"DUPLICATE", "POSSIBLE_DUPLICATE"}:
            raise WorkflowError("resolve duplicate status before selection")
        event_id = make_event_id(candidate["company"], candidate["product"] or candidate["topic"], candidate["event_type"], candidate["event_date"])
        with self.db.connect() as connection:
            connection.execute("UPDATE candidates SET status='SELECTED',selected=1,event_id=?,updated_at=? WHERE candidate_id=?", (event_id, utc_now(), candidate_id))
            self._transition(connection, "candidate", candidate_id, candidate["status"], "SELECTED", actor, reason)

    def reject_candidate(self, candidate_id: str, actor: str, reason: str) -> None:
        candidate = self.get_candidate(candidate_id)
        if candidate["status"] not in {"CANDIDATE", "DUPLICATE_CHECKED"}:
            raise WorkflowError("rejection requires CANDIDATE or DUPLICATE_CHECKED")
        with self.db.connect() as connection:
            connection.execute("UPDATE candidates SET status='REJECTED',rejection_reason=?,updated_at=? WHERE candidate_id=?", (reason, utc_now(), candidate_id))
            self._transition(connection, "candidate", candidate_id, candidate["status"], "REJECTED", actor, reason)

    def create_brief(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        if candidate["status"] != "SELECTED":
            raise WorkflowError("brief creation requires SELECTED")
        existing = self.db.one("SELECT * FROM articles WHERE candidate_id=?", (candidate_id,))
        if existing:
            return existing
        article_id, now = new_id("art"), utc_now()
        path = self.root / "research" / f"{article_id}.md"
        path.write_text(self._brief_template(candidate), encoding="utf-8")
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO articles(article_id,candidate_id,event_id,status,event_date,core_fact,topic,subtopic,company,product,event_type,keywords,primary_source,secondary_sources,research_brief_path,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article_id,candidate_id,candidate["event_id"],"SELECTED",candidate["event_date"],candidate["core_fact"],candidate["topic"],candidate["subtopic"],candidate["company"],candidate["product"],candidate["event_type"],json.dumps(candidate["keywords"],ensure_ascii=False),candidate["primary_source"],json.dumps(candidate["secondary_sources"],ensure_ascii=False),str(path.relative_to(self.root)),now,now),
            )
            self._transition(connection, "article", article_id, None, "SELECTED", "system", "research brief created after human selection")
        return self.get_article(article_id)

    def create_prompt(self, article_id: str, purpose: str) -> Path:
        mapping = {
            "research": ("research_brief.md", {"SELECTED"}),
            "writing": ("article_writer.md", {"RESEARCHED"}),
            "fact_check": ("fact_checker.md", {"DRAFTED"}),
            "image": ("image_concept.md", {"FACT_CHECKED"}),
            "editor_review": ("editor_review.md", {"IMAGE_READY"}),
        }
        if purpose not in mapping:
            raise WorkflowError(f"unknown prompt purpose: {purpose}")
        article = self.get_article(article_id)
        template_name, allowed = mapping[purpose]
        if article["status"] not in allowed:
            raise WorkflowError(f"{purpose} prompt requires one of {sorted(allowed)}; current status is {article['status']}")
        template = (self.project_root / "prompts" / template_name).read_text(encoding="utf-8")
        prompt_dir = self.root / "output" / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        path = prompt_dir / f"{article_id}-{purpose}.md"
        context = {
            "article_id": article_id, "event_id": article["event_id"], "status": article["status"],
            "event_date": article["event_date"], "core_fact": article["core_fact"], "company": article["company"],
            "product": article["product"], "primary_source": article["primary_source"], "secondary_sources": article["secondary_sources"],
            "research_brief_path": article["research_brief_path"], "draft_path": article["draft_path"], "fact_check_path": article["fact_check_path"],
        }
        path.write_text(template + "\n\n# Workflow Context\n\n```json\n" + json.dumps(context, ensure_ascii=False, indent=2) + "\n```\n", encoding="utf-8")
        return path

    def _brief_template(self, candidate: dict[str, Any]) -> str:
        headings = ["Event","Core Fact","Why It Matters","Confirmed Facts","Company Claims","Independent Reporting","Important Numbers","Technical Background","Timeline","Primary Sources","Secondary Sources","Conflicting Information","Unknown / Unconfirmed","Potential Errors To Avoid","Suggested Angle"]
        content = {"Event": candidate["candidate_title"], "Core Fact": candidate["core_fact"], "Primary Sources": f"- {candidate['primary_source']}", "Secondary Sources": "\n".join(f"- {url}" for url in candidate["secondary_sources"])}
        return "\n".join(f"# {heading}\n\n{content.get(heading, '')}\n" for heading in headings)

    def get_article(self, article_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM articles WHERE article_id=?", (article_id,))
        if not row:
            raise WorkflowError(f"article not found: {article_id}")
        row["keywords"] = json.loads(row["keywords"])
        row["secondary_sources"] = json.loads(row["secondary_sources"])
        return row

    def _copy_artifact(self, article_id: str, source: Path, directory: str, filename: str) -> str:
        source = Path(source)
        if not source.is_file():
            raise WorkflowError(f"input file not found: {source}")
        target_dir = self.root / directory / article_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        if target.exists():
            raise FileExistsError(f"artifact already exists: {target}")
        shutil.copyfile(source, target)
        return str(target.relative_to(self.root))

    def _advance_article(self, article_id: str, required: str, new: str, actor: str, reason: str, updates: dict[str, Any] | None = None) -> None:
        article = self.get_article(article_id)
        if article["status"] != required:
            raise WorkflowError(f"{new} requires {required}; current status is {article['status']}")
        updates = updates or {}
        assignments = ["status=?", "updated_at=?"] + [f"{key}=?" for key in updates]
        params = [new, utc_now(), *updates.values(), article_id]
        with self.db.connect() as connection:
            connection.execute(f"UPDATE articles SET {','.join(assignments)} WHERE article_id=?", params)
            self._transition(connection, "article", article_id, required, new, actor, reason)

    def register_research(self, article_id: str, source: Path) -> None:
        path = self._copy_artifact(article_id, source, "research", "research-brief.md")
        self._advance_article(article_id, "SELECTED", "RESEARCHED", "editor", "manual research registered", {"research_brief_path": path})

    def register_article(self, article_id: str, source: Path, title_cn: str, slug: str) -> None:
        path = self._copy_artifact(article_id, source, "articles", "article.md")
        self._advance_article(article_id, "RESEARCHED", "DRAFTED", "editor", "manual draft registered", {"draft_path": path, "title_cn": title_cn, "slug": slug})

    def register_fact_check(self, article_id: str, source: Path) -> None:
        path = self._copy_artifact(article_id, source, "articles", "fact-check.md")
        self._advance_article(article_id, "DRAFTED", "FACT_CHECKED", "editor", "manual fact check registered", {"fact_check_path": path})

    def register_image(self, article_id: str, *, image_type: str, image_file: Path | None = None, source_url: str | None = None, creator: str | None = None, license: str | None = None, license_url: str | None = None, attribution_text: str | None = None, ai_provider: str | None = None, ai_model: str | None = None, prompt_path: str | None = None, notes: str = "") -> str:
        article = self.get_article(article_id)
        if article["status"] != "FACT_CHECKED":
            raise WorkflowError("image registration requires FACT_CHECKED")
        try:
            image = ImageInput(image_type=image_type, source_url=source_url, creator=creator, license=license, license_url=license_url, attribution_text=attribution_text, ai_provider=ai_provider, ai_model=ai_model, prompt_path=prompt_path, notes=notes).model_dump(mode="json")
        except ValidationError as error:
            raise WorkflowError("image requires a clear license or AI provenance") from error
        if image_file is None:
            raise WorkflowError("image file is required")
        image_file = Path(image_file)
        image_path = self._copy_artifact(article_id, image_file, "images", f"hero{image_file.suffix.lower()}")
        ai_valid = image_type == "AI_GENERATED"
        image_id = new_id("img")
        with self.db.connect() as connection:
            connection.execute("INSERT INTO images(image_id,article_id,image_type,source_url,creator,license,license_url,attribution_text,ai_provider,ai_model,prompt_path,image_path,generated_at,downloaded_at,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (image_id,article_id,image["image_type"],image["source_url"],image["creator"],image["license"],image["license_url"],image["attribution_text"],image["ai_provider"],image["ai_model"],image["prompt_path"],image_path,utc_now() if ai_valid else None,None if ai_valid else utc_now(),image["notes"]))
            connection.execute("UPDATE articles SET image_id=?,status='IMAGE_READY',updated_at=? WHERE article_id=?", (image_id,utc_now(),article_id))
            self._transition(connection, "article", article_id, "FACT_CHECKED", "IMAGE_READY", "editor", "image rights and provenance recorded")
        return image_id

    def complete_review(self, article_id: str, actor: str, reason: str) -> None:
        self._advance_article(article_id, "IMAGE_READY", "REVIEWED", actor, reason)

    def approve(self, article_id: str, actor: str, reason: str) -> None:
        article = self.get_article(article_id)
        if article["status"] != "REVIEWED":
            raise WorkflowError(f"approval requires IMAGE_READY and REVIEWED; current status is {article['status']}")
        self._advance_article(article_id, "REVIEWED", "APPROVED", actor, reason)

    def create_publication_package(self, article_id: str) -> Path:
        article = self.get_article(article_id)
        if article["status"] != "APPROVED":
            raise WorkflowError("publication package requires APPROVED")
        image = self.db.one("SELECT * FROM images WHERE image_id=?", (article["image_id"],))
        if not image or not image["image_path"]:
            raise WorkflowError("publication package requires a registered image file")
        image_source = self.root / image["image_path"]
        if not image_source.is_file():
            raise WorkflowError(f"registered image file not found: {image_source}")
        target = self.root / "output" / article_id
        target.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(self.root / article["draft_path"], target / "article.md")
        shutil.copyfile(self.root / article["research_brief_path"], target / "research-brief.md")
        shutil.copyfile(self.root / article["fact_check_path"], target / "fact-check.md")
        shutil.copyfile(image_source, target / image_source.name)
        (target / "metadata.json").write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "image-license.json").write_text(json.dumps(image, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "sources.md").write_text("# Sources\n\n## Primary\n\n- " + article["primary_source"] + "\n\n## Secondary\n\n" + "\n".join(f"- {url}" for url in article["secondary_sources"]) + "\n", encoding="utf-8")
        (target / "image-brief.md").write_text("# Image Brief\n\nSee the registered prompt and image-license.json provenance.\n", encoding="utf-8")
        checks = ["title reviewed","event date checked","primary source present","independent source checked","numbers verified","company claims attributed","duplicate check passed","image rights recorded","final human review","approved for publication"]
        (target / "publication-checklist.md").write_text("# Publication Checklist\n\n" + "\n".join(f"- [x] {item}" for item in checks) + "\n", encoding="utf-8")
        return target

    def publish(self, article_id: str, actor: str, article_url: str | None = None) -> None:
        if article_url:
            require_url(article_url, "article_url")
        self._advance_article(article_id, "APPROVED", "PUBLISHED", actor, "human confirmed external publication", {"article_url": article_url, "published_at": utc_now(), "publish_date": utc_now()[:10]})

    def audit(self, entity_id: str) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM state_transitions WHERE entity_id=? ORDER BY transition_id", (entity_id,))

    def stats(self) -> dict[str, Any]:
        return {"candidates": self.db.all("SELECT status,COUNT(*) AS count FROM candidates GROUP BY status"), "articles": self.db.all("SELECT status,COUNT(*) AS count FROM articles GROUP BY status")}
