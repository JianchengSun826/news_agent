from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from newsroom.batch_models import (
    BatchContentResult,
    BatchFactCheckResult,
    ImageCandidateResult,
    candidate_fingerprint,
)


def candidate_values(**overrides):
    values = {
        "event_date": "2026-10-05",
        "company": "Example",
        "product": "Tool",
        "event_type": "launch",
        "core_fact": "Example launched Tool on 2026-10-05.",
        "primary_source": "https://example.com/news/tool",
    }
    values.update(overrides)
    return values


def valid_image_candidate(**overrides):
    values = {
        "landing_page_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
        "direct_image_url": "https://upload.wikimedia.org/example.jpg",
        "creator": "Example Creator",
        "rights_basis": "OPEN_LICENSE",
        "license_name": "CC BY 4.0",
        "rights_url": "https://creativecommons.org/licenses/by/4.0/",
        "rights_statement": "Licensed under CC BY 4.0.",
        "attribution_required": True,
        "attribution_text": "Example Creator, CC BY 4.0",
        "relevance": "Shows the announced product.",
    }
    values.update(overrides)
    return values


def valid_content_result():
    return {
        "schema_version": 1,
        "batch_id": "batch-123456789abc",
        "request_id": "req-123456789abc",
        "output_file": "batch-123456789abc-content-result.json",
        "items": [
            {
                "article_id": "art-123456789abc",
                "research_brief_md": "# Research Brief\n\nA sufficiently detailed verified research brief.",
                "title_cn": "示例公司发布新工具",
                "slug": "example-company-new-tool",
                "article_md": "# 示例公司发布新工具\n\n这是一篇包含充分事实和来源说明的科技新闻文章。",
                "image_candidates": [valid_image_candidate()],
            }
        ],
    }


def test_candidate_fingerprint_normalizes_case_whitespace_and_trailing_slash():
    first = candidate_values(company=" OpenAI ", primary_source="https://openai.com/news/")
    second = candidate_values(company="openai", primary_source="https://openai.com/news")

    assert candidate_fingerprint(first) == candidate_fingerprint(second)


def test_candidate_fingerprint_changes_when_event_changes():
    first = candidate_values(event_date="2026-10-05")
    second = candidate_values(event_date="2026-10-06")

    assert candidate_fingerprint(first) != candidate_fingerprint(second)


def test_content_result_forbids_unknown_fields():
    payload = valid_content_result()
    payload["items"][0]["surprise"] = True

    with pytest.raises(ValidationError, match="surprise"):
        BatchContentResult.model_validate(payload)


def test_content_result_rejects_duplicate_article_ids():
    payload = valid_content_result()
    payload["items"].append(deepcopy(payload["items"][0]))

    with pytest.raises(ValidationError, match="duplicate article_id"):
        BatchContentResult.model_validate(payload)


def test_content_result_rejects_more_than_three_image_candidates():
    payload = valid_content_result()
    payload["items"][0]["image_candidates"] = [valid_image_candidate() for _ in range(4)]

    with pytest.raises(ValidationError, match="image_candidates"):
        BatchContentResult.model_validate(payload)


def test_image_candidate_requires_original_rights_page():
    payload = valid_image_candidate()
    payload["rights_url"] = None

    with pytest.raises(ValidationError, match="rights_url"):
        ImageCandidateResult.model_validate(payload)


def test_open_license_requires_creator_and_license_name():
    payload = valid_image_candidate(creator=None)

    with pytest.raises(ValidationError, match="creator"):
        ImageCandidateResult.model_validate(payload)


def test_attribution_text_is_required_when_attribution_is_required():
    payload = valid_image_candidate(attribution_text=None)

    with pytest.raises(ValidationError, match="attribution_text"):
        ImageCandidateResult.model_validate(payload)


def test_fact_check_result_rejects_duplicate_article_ids():
    item = {
        "article_id": "art-123456789abc",
        "outcome": "PASS",
        "report_md": "# Fact Check: PASS\n\nAll material claims were checked against sources.",
        "blocking_issues": [],
    }
    payload = {
        "schema_version": 1,
        "batch_id": "batch-123456789abc",
        "request_id": "req-123456789abc",
        "output_file": "batch-123456789abc-fact-check-result.json",
        "items": [item, deepcopy(item)],
    }

    with pytest.raises(ValidationError, match="duplicate article_id"):
        BatchFactCheckResult.model_validate(payload)
