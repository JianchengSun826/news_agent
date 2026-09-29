from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Iterable


@dataclass(frozen=True)
class DuplicateResult:
    classification: str
    matching_event_id: str | None
    score: float
    explanation: str


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _keywords(value) -> set[str]:
    if isinstance(value, str):
        import json
        value = json.loads(value)
    return {_norm(item) for item in value if _norm(item)}


def classify_candidate(candidate: dict, existing_events: Iterable[dict]) -> DuplicateResult:
    best = DuplicateResult("NEW", None, 0.0, "No matching event attributes found")
    for event in existing_events:
        same_company = _norm(candidate["company"]) == _norm(event["company"])
        same_product = _norm(candidate["product"]) == _norm(event["product"])
        same_type = _norm(candidate["event_type"]) == _norm(event["event_type"])
        days = _days(candidate["event_date"], event["event_date"])
        text_score = SequenceMatcher(None, _norm(candidate["core_fact"]), _norm(event["core_fact"])).ratio()
        left, right = _keywords(candidate.get("keywords", [])), _keywords(event.get("keywords", []))
        keyword_score = len(left & right) / len(left | right) if left | right else 0.0
        score = (0.25 * same_company + 0.20 * same_product + 0.15 * same_type + 0.25 * text_score + 0.15 * keyword_score)
        if same_company and same_product and same_type and days <= 2 and text_score >= 0.80:
            classification = "DUPLICATE"
        elif same_company and same_product and days <= 14 and same_type:
            classification = "POSSIBLE_DUPLICATE"
        elif same_company and same_product and days > 2:
            classification = "FOLLOW_UP"
        elif same_company and (same_product or keyword_score >= 0.4):
            classification = "RELATED"
        else:
            classification = "NEW"
        if score > best.score:
            best = DuplicateResult(classification, event.get("event_id"), round(score, 3), f"company={same_company}, product={same_product}, type={same_type}, days={days}, text={text_score:.2f}, keywords={keyword_score:.2f}")
    return best
