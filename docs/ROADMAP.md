# Roadmap

## Phase 0 — Workflow Design (implemented)

维护业务流程、编辑/来源/图片政策、架构、数据模型、假设、prompt library 和 acceptance demo。文档、schema、tests 与真实 workflow 必须同步。

## Phase 1 — Human-in-the-loop MVP (current)

本地 CLI + SQLite 支持候选登记、事件级 deterministic 去重、人工选题、Research Brief、人工模型交接、稿件/事实核验/图片登记、审批、发布包、审计和 published 查询。使用 2–4 周并积累约 30 篇真实文章后，再评估下一阶段。

## Phase 2 — Semi-automated newsroom (do not start yet)

候选 JSON/Markdown import、增强 fuzzy/entity clustering、URL metadata 验证、完整 prompt package、可选 Google Sheets adapter 和 eval dataset。SQLite 是否继续作为唯一 source of truth 需先写 ADR；不得引入双向同步冲突。

## Phase 3 — Full agent (future only)

可替换 provider adapters、配置化 scheduler、失败恢复、observability、cost/token tracking、自动 discovery 与发布 adapter。确定性工作优先使用普通代码，高影响行为继续保留人工 gate。

## Exit criteria for Phase 1

真实完成约 30 篇文章；记录 duplicate accuracy、人工修改和常见失败；确认每个拟自动化步骤已经由人工流程证明有效；再用 ADR 决定 Phase 2 的最小范围。
