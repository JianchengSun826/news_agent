# Tech Newsroom Agent

一个本地运行、人工把关的中文科技新闻生产工作流。Phase 1 使用 Python 3.12、Pydantic、Typer 和 SQLite 保存候选、来源、文章、图片版权和审计记录；Gemini、Claude 或 ChatGPT 仅通过可复制的 prompt 与 Markdown 文件人工接入，不需要 API key，也不会自动发布。

## Quick start

```bash
uv sync --dev
uv run newsroom --root . init
uv run newsroom --help
uv run pytest
bash scripts/demo.sh
```

Demo 使用 `demo-workspace/` 中的独立数据库，创建 5 个候选，完成 3 次人工选题，并把其中 1 篇推进到 `PUBLISHED`。生成的发布包位于 `demo-workspace/output/<article_id>/`。

## Workflow

候选经过事件级去重后，编辑人工执行 SELECTED gate。系统生成 research prompt，人工将研究结果保存为 Markdown；随后依次录入稿件、事实核验和图片来源。人工 review 与 APPROVED gate 通过后，系统生成本地发布包。发布仍由人工在外部完成，再用 CLI 记录 URL 和时间。

详细命令和状态规则见 [工作流](docs/WORKFLOW.md)，数据字段见 [数据模型](docs/DATA_MODEL.md)，Phase 边界见 [路线图](docs/ROADMAP.md)。

## Design principles

- SQLite 是 Phase 1 的唯一 canonical database。
- 所有重要状态变化写入 append-only audit trail。
- Prompt 保存在 `prompts/`，代码不绑定模型供应商。
- 只保存来源 metadata、URL 和编辑笔记，不复制第三方全文。
- 图片必须有明确许可证或 AI 生成 provenance。
- Phase 1 不包含网络抓取、定时任务、Web UI 或自动发布。
