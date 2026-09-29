# Architecture

## Phase 1 components

`newsroom.cli` 使用 Typer 将类型化命令转换为领域操作；`newsroom.models` 使用 Pydantic v2 校验候选和图片输入，并负责 ID、URL 与时间格式；`newsroom.service.Newsroom` 执行状态 gate、artifact 复制和发布包生成；`newsroom.dedup` 是无网络的事件级 heuristic；`newsroom.db` 管理 SQLite schema 和参数化查询。

SQLite 位于 `data/newsroom.db`，是唯一事实来源。Markdown、JSON 和 CSV 是人工界面与导出格式。外部 LLM 不在 runtime 内：`prompts/` 使用 provider-neutral frontmatter，人工复制到订阅产品并将结果注册回来。

## Boundaries

Phase 1 没有 provider SDK、网络抓取、scheduler、Web frontend、embedding、向量数据库或自动发布。未来 provider 必须通过 Search/Research/Writing/FactCheck/Image adapter 接入，并保留 ManualProvider 路径。任何重大选择写入 `docs/ADR/`。

## Reliability and security

状态更新和 audit insert 在同一 SQLite transaction 中完成。所有 SQL 使用参数绑定。CLI 拒绝无效 URL、跳步、未解决重复项、无图片权利记录和缺少人工 review 的审批。系统只保存 URL、metadata 和人工笔记，不绕过 paywall，不长期保存第三方全文。
