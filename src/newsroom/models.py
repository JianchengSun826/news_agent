from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Self
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class CandidateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    candidate_title: str = Field(min_length=1)
    core_fact: str = Field(min_length=1)
    event_date: date
    company: str = Field(min_length=1)
    product: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    subtopic: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    primary_source: HttpUrl
    secondary_sources: list[HttpUrl] = Field(min_length=1)
    discovered_from: str = Field(min_length=1)
    priority: int = Field(ge=1, le=5)
    notes: str = ""


class ImageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    image_type: str
    source_url: HttpUrl | None = None
    creator: str | None = None
    license: str | None = None
    license_url: HttpUrl | None = None
    attribution_text: str | None = None
    ai_provider: str | None = None
    ai_model: str | None = None
    prompt_path: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def validate_rights(self) -> Self:
        ai_valid = self.image_type == "AI_GENERATED" and self.ai_provider and self.ai_model and self.prompt_path
        licensed_valid = self.image_type != "AI_GENERATED" and self.source_url and self.creator and self.license and self.license_url
        if not (ai_valid or licensed_valid):
            raise ValueError("image requires a clear license or AI provenance")
        return self


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")


def make_event_id(company: str, product_or_topic: str, event_type: str, event_date: str) -> str:
    date_part = datetime.strptime(event_date, "%Y-%m-%d").strftime("%Y%m%d")
    parts = [slugify(company), slugify(product_or_topic), slugify(event_type), date_part]
    if not all(parts[:-1]):
        raise ValueError("event_id requires ASCII company, product/topic, and event_type")
    return "-".join(parts)


def require_url(value: str, field: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field} must be an http(s) URL")
    return value
