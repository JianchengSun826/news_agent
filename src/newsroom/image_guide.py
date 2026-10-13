"""Render image evidence for editors who handle image files externally."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .batch_models import BatchContentResult
from .service import WorkflowError

if TYPE_CHECKING:
    from .service import Newsroom


def render_image_guide(room: Newsroom, article: dict[str, Any]) -> str:
    """Use SQLite candidates and the hash-verified original content result."""
    candidates = room.list_image_candidates(article['article_id'])
    recommendations: list[dict[str, Any]] = []
    requests = room.db.all(
        "SELECT result_path,result_sha256 FROM batch_requests WHERE batch_id=? "
        "AND request_type='CONTENT' AND status='COMPLETED' ORDER BY created_at,request_id",
        (article['owning_batch_id'],),
    )
    for request in requests:
        relative = Path(request['result_path'] or '')
        path = room.root / relative
        if relative.is_absolute() or '..' in relative.parts or not path.resolve().is_relative_to(room.root):
            raise WorkflowError('unsafe image recommendation source path')
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise WorkflowError('image recommendation source cannot contain symlinks')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != request['result_sha256']:
            raise WorkflowError('image recommendation source hash mismatch')
        result = BatchContentResult.model_validate_json(raw)
        if result.batch_id != article['owning_batch_id']:
            raise WorkflowError('image recommendation source batch mismatch')
        for item in result.items:
            if item.article_id == article['article_id']:
                recommendations = [r.model_dump(mode='json') for r in item.image_recommendations]
    verified = {(c['landing_page_url'], c['direct_image_url']) for c in candidates}
    recommendations = [r for r in recommendations
                       if (r['landing_page_url'], r['direct_image_url']) not in verified]
    lines = ['# 图片指南', '', f"文章：{article['title_cn'] or article['article_id']}", '',
             '导出后自行选择、下载原图并上传 Google Drive。以下提供证据和建议，不代表已经选用或下载图片。', '',
             '## 已核实可用（须遵守所列条件）', '',
             '依据已记录授权核实；下载时核对具体图片、使用范围、第三方限制和署名。', '']
    if not candidates:
        lines += ['没有已核实可用的图片。', '']
    for i, image in enumerate(candidates, 1):
        lines += [f'### 图片 {i}', '', f"- 原始页面：{image['landing_page_url']}",
                  f"- 原图链接：{image['direct_image_url'] or '请从原始页面下载'}",
                  f"- 作者／权利方：{image['creator'] or '未记录'}",
                  f"- 权利依据：{image['rights_basis']} · {image['license_name'] or '见权利声明'}",
                  f"- 授权页面：{image['rights_url']}", f"- 使用条件：{image['rights_statement']}",
                  f"- 是否必须署名：{'是' if image['attribution_required'] else '否'}",
                  f"- 署名文字：{image['attribution_text'] or '无要求'}",
                  f"- 推荐原因／画面说明：{image['relevance']}", '']
    lines += ['## 推荐使用，授权待确认', '', '以下仅为配图建议；须解决列出的授权问题后再使用。', '']
    if not recommendations:
        lines += ['没有额外推荐。', '']
    labels = {'PAGE_INACCESSIBLE': '页面无法访问', 'INSUFFICIENT_EVIDENCE': '授权证据不足', 'RESTRICTED_USE': '存在使用限制'}
    for i, image in enumerate(recommendations, 1):
        lines += [f'### 推荐 {i}', '', f"- 原始页面：{image['landing_page_url']}",
                  f"- 原图链接：{image['direct_image_url'] or '请从原始页面查找'}",
                  f"- 作者／权利方：{image['creator'] or '待确认'}",
                  f"- 权利线索页面：{image['rights_url'] or '未找到'}",
                  f"- 已知权利信息：{image['rights_information'] or '未知'}",
                  f"- 核验状态：{labels[image['verification_status']]}",
                  f"- 使用前须解决：{image['verification_issue']}",
                  f"- 推荐原因／画面说明：{image['relevance']}", '']
    return '\n'.join(lines) + '\n'
