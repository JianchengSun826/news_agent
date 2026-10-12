from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


SCHEMA_VERSION = 1
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
RIGHTS_BASES = Literal["PUBLIC_DOMAIN", "OPEN_LICENSE", "OFFICIAL_PRESS_LICENSE", "OWNED"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _normalized_text(value: Any) -> str:
    return " ".join(str(value).casefold().split())


def candidate_fingerprint(values: Mapping[str, Any]) -> str:
    canonical = {
        "event_date": str(values["event_date"]),
        "company": _normalized_text(values["company"]),
        "product": _normalized_text(values["product"]),
        "event_type": _normalized_text(values["event_type"]),
        "core_fact": _normalized_text(values["core_fact"]),
        "primary_source": str(values["primary_source"]).rstrip("/").casefold(),
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ImageCandidateResult(StrictModel):
    landing_page_url: HttpUrl
    direct_image_url: HttpUrl | None = None
    creator: str | None = None
    rights_basis: RIGHTS_BASES
    license_name: str | None = None
    rights_url: HttpUrl
    rights_statement: str = Field(min_length=1)
    attribution_required: bool
    attribution_text: str | None = None
    relevance: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rights_metadata(self) -> Self:
        if self.rights_basis == "OPEN_LICENSE":
            if not self.creator:
                raise ValueError("creator is required for OPEN_LICENSE")
            if not self.license_name:
                raise ValueError("license_name is required for OPEN_LICENSE")
        if self.attribution_required and not self.attribution_text:
            raise ValueError("attribution_text is required when attribution_required is true")
        return self


class ImageRecommendationResult(StrictModel):
    """A search lead, never evidence of permission or a selectable candidate."""

    landing_page_url: HttpUrl
    direct_image_url: HttpUrl | None = None
    creator: str | None = None
    rights_url: HttpUrl | None = None
    rights_information: str | None = None
    verification_status: Literal["PAGE_INACCESSIBLE", "INSUFFICIENT_EVIDENCE", "RESTRICTED_USE"]
    verification_issue: str = Field(min_length=1)
    relevance: str = Field(min_length=1)


class ContentRequestItem(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    candidate_id: str = Field(pattern=r"^cand-[a-z0-9]+$")
    event_id: str = Field(min_length=1)
    event_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    candidate_title: str = Field(min_length=1)
    core_fact: str = Field(min_length=1)
    company: str = Field(min_length=1)
    product: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    subtopic: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    primary_source: HttpUrl
    secondary_sources: list[HttpUrl] = Field(default_factory=list)


class ContentResultItem(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    research_brief_md: str = Field(min_length=20)
    title_cn: str = Field(min_length=1)
    slug: str = Field(pattern=SLUG_PATTERN)
    article_md: str = Field(min_length=20)
    image_candidates: list[ImageCandidateResult] = Field(default_factory=list, max_length=3)
    image_recommendations: list[ImageRecommendationResult] = Field(default_factory=list, max_length=3)


class FactCheckRequestItem(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    revision_number: int = Field(ge=1)
    title_cn: str = Field(min_length=1)
    research_brief_md: str = Field(min_length=20)
    article_md: str = Field(min_length=20)
    primary_source: HttpUrl
    secondary_sources: list[HttpUrl] = Field(default_factory=list)


class FactCheckResultItem(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    outcome: Literal["PASS", "FAIL"]
    report_md: str = Field(min_length=20)
    blocking_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.outcome == "PASS" and self.blocking_issues:
            raise ValueError("PASS cannot contain blocking_issues")
        if self.outcome == "FAIL" and not self.blocking_issues:
            raise ValueError("FAIL requires at least one blocking issue")
        return self


class RevisionRequestItem(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    revision_number: int = Field(ge=1)
    title_cn: str = Field(min_length=1)
    slug: str = Field(pattern=SLUG_PATTERN)
    research_brief_md: str = Field(min_length=20)
    article_md: str = Field(min_length=20)
    fact_check_report_md: str = Field(min_length=20)
    blocking_issues: list[str] = Field(min_length=1)


class RevisionResultItem(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    research_brief_md: str = Field(min_length=20)
    title_cn: str = Field(min_length=1)
    slug: str = Field(pattern=SLUG_PATTERN)
    article_md: str = Field(min_length=20)


class BatchDocument(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    batch_id: str = Field(pattern=r"^batch-[a-z0-9]+$")
    request_id: str = Field(pattern=r"^req-[a-z0-9]+$")
    output_file: str = Field(min_length=1)


class BatchRequestDocument(BatchDocument):
    instructions: str = Field(min_length=20)


def _reject_duplicate_article_ids(items: list[Any]) -> None:
    article_ids = [item.article_id for item in items]
    if len(article_ids) != len(set(article_ids)):
        raise ValueError("duplicate article_id in items")


class BatchContentRequest(BatchRequestDocument):
    items: list[ContentRequestItem] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self


class BatchContentResult(BatchDocument):
    items: list[ContentResultItem] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self


class BatchFactCheckRequest(BatchRequestDocument):
    items: list[FactCheckRequestItem] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self


class BatchFactCheckResult(BatchDocument):
    items: list[FactCheckResultItem] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self


class BatchRevisionRequest(BatchRequestDocument):
    items: list[RevisionRequestItem] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self


class BatchRevisionResult(BatchDocument):
    items: list[RevisionResultItem] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self


class BatchImageSelection(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    image_candidate_id: str = Field(pattern=r"^imgcand-[a-z0-9]+$")
    image_file: Path
    rights_confirmed: Literal[True]

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def literal_confirmation(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("rights_confirmed must be literal true")
        return value


class NoImageSelection(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    no_image: Literal[True]
    reason: str = Field(min_length=1)

    @field_validator("no_image", mode="before")
    @classmethod
    def literal_choice(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("no_image must be literal true")
        return value


class ImageCandidateSupplementItem(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    image_candidates: list[ImageCandidateResult] = Field(min_length=1, max_length=3)


class BatchImageCandidateSupplement(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    batch_id: str = Field(pattern=r"^batch-[a-z0-9]+$")
    items: list[ImageCandidateSupplementItem] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self


class BatchImagePlan(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    batch_id: str = Field(pattern=r"^batch-[a-z0-9]+$")
    # An incomplete list is a resumable draft, never an executable image plan.
    items: list[BatchImageSelection | NoImageSelection]

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self


class PublicationResultItem(StrictModel):
    article_id: str = Field(pattern=r"^art-[a-z0-9]+$")
    article_url: HttpUrl


class BatchPublicationResult(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    batch_id: str = Field(pattern=r"^batch-[a-z0-9]+$")
    items: list[PublicationResultItem]

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        _reject_duplicate_article_ids(self.items)
        return self
