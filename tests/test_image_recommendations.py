from __future__ import annotations

import json
import pytest
from pydantic import ValidationError
from newsroom.service import WorkflowError
from newsroom.batch_release import BatchReleaseWorkflow
from newsroom.workspace import BatchWorkspace
from test_batch import newsroom, prepared_batch, write_content_result


def recommendation():
    return {
        'landing_page_url': 'https://press.example.com/product-photo',
        'direct_image_url': None,
        'creator': None,
        'rights_url': 'https://press.example.com/media-policy',
        'rights_information': 'A media policy link exists but could not be opened.',
        'verification_status': 'PAGE_INACCESSIBLE',
        'verification_issue': 'Could not read the applicable permission and credit requirements.',
        'relevance': 'Archive photo of the previous model; label it accurately.',
    }


def result_with_recommendation(batch, path):
    write_content_result(batch, path)
    payload = json.loads(path.read_bytes())
    payload['items'][0]['image_candidates'] = []
    payload['items'][0]['image_recommendations'] = [recommendation()]
    path.write_text(json.dumps(payload))
    return payload


def test_recommendations_are_reviewable_but_not_selectable(prepared_batch, tmp_path):
    batch = prepared_batch
    path = tmp_path / 'result.json'
    payload = result_with_recommendation(batch, path)
    batch.workflow.import_content(batch.batch_id, path)
    workspace = BatchWorkspace(batch.room)
    root = workspace.article_root(batch.batch_id, batch.article_ids[0])
    assert batch.room.list_image_candidates(batch.article_ids[0]) == []
    assert json.loads((root / 'image-recommendations.json').read_bytes()) == [recommendation()]
    review = (root / 'image-recommendations.md').read_text()
    assert 'https://press.example.com/product-photo' in review
    assert 'Could not read' in review
    assert '待人工判断' in review
    template = json.loads((root / 'manual-image-candidates.json').read_bytes())
    assert template['batch_id'] == batch.batch_id
    candidate = template['items'][0]['image_candidates'][0]
    assert candidate['rights_basis'] is None
    assert candidate['rights_statement'] is None
    articles_before = [batch.room.get_article(a) for a in batch.article_ids]
    release = BatchReleaseWorkflow(batch.room)
    with pytest.raises(WorkflowError, match='invalid image candidate supplement'):
        release.import_image_candidates(batch.batch_id, root / 'manual-image-candidates.json', 'editor', 'reviewed')
    candidate.update(rights_basis='OFFICIAL_PRESS_LICENSE', rights_statement='Policy allows editorial reuse by the press.', attribution_required=False)
    (tmp_path / 'manual.json').write_text(json.dumps(template))
    assert release.import_image_candidates(batch.batch_id, tmp_path / 'manual.json', 'editor', 'Read applicable media policy')['added'] == 1
    assert [batch.room.get_article(a) for a in batch.article_ids] == articles_before
    assert json.loads(path.read_bytes()) == payload
    archive = batch.room.db.one("SELECT result_path FROM batch_requests WHERE batch_id=? AND request_type='CONTENT'", (batch.batch_id,))
    assert (batch.room.root / archive['result_path']).read_bytes() == path.read_bytes()
    (root / 'manual-image-candidates.json').write_text(json.dumps(template))
    manual_bytes = (root / 'manual-image-candidates.json').read_bytes()
    (root / 'image-recommendations.json').unlink()
    workspace.refresh(batch.batch_id)
    assert json.loads((root / 'image-recommendations.json').read_bytes()) == [recommendation()]
    assert (root / 'manual-image-candidates.json').read_bytes() == manual_bytes


@pytest.mark.parametrize('change', [
    {'verification_status': 'VERIFIED'},
    {'verification_issue': ' '},
    {'rights_basis': 'PUBLIC_DOMAIN'},
    {'landing_page_url': 'not-a-url'},
])
def test_invalid_recommendations_reject_entire_result(prepared_batch, tmp_path, change):
    path = tmp_path / 'result.json'
    payload = result_with_recommendation(prepared_batch, path)
    payload['items'][0]['image_recommendations'][0].update(change)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        prepared_batch.workflow.import_content(prepared_batch.batch_id, path)
    assert all(prepared_batch.room.get_article(a)['status'] == 'SELECTED' for a in prepared_batch.article_ids)
    assert prepared_batch.room.db.all('SELECT * FROM image_candidates') == []
