# Phase 1 Assumptions

- 项目要求 Python 3.12+；`uv` 负责选择或安装兼容解释器。
- CLI 使用 Typer，输入模型使用 Pydantic v2；两者是 Phase 1 的唯一 runtime dependencies。
- SQLite 是 canonical database；CSV 是 Google Sheets-friendly interchange，Phase 1 不做 Sheets API sync。
- 一个 selected candidate 对应一篇 article；重大 follow-up 以新 event_id 新建候选。
- CLI 的人工 actor/reason 字段是 Phase 1 的身份与审批证据，不包含账号认证。
- 自动 checklist 只能确认结构性证据；内容准确性、独立性和图片实际视觉内容由 review/approve 的人负责。
- 发布包生成不等于公开发布；`publish` 仅在人工完成外部发布后记录结果。
- Demo 使用虚构示例 URL 和内容，只验证 workflow，不作为真实新闻或来源。
