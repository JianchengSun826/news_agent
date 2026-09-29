# Phase 1 Workflow

## State machine

主路径为：

`DISCOVERED → CANDIDATE → DUPLICATE_CHECKED → SELECTED → RESEARCHED → DRAFTED → FACT_CHECKED → IMAGE_READY → REVIEWED → APPROVED → PUBLISHED`

`REJECTED`、`DUPLICATE`、`ON_HOLD` 是终止或暂停状态。Phase 1 CLI 不自动恢复这些状态；编辑可保留记录并重新创建候选。每次状态变化都写入 `state_transitions`，包含 actor、reason 和 UTC 时间。

## Daily operating procedure

1. 人工在可信来源或 Gemini 中发现新闻，用 `candidate add` 或 CSV import 保存 metadata。
2. 对每条候选运行 `duplicate-check`。系统比较公司、产品、事件类型、日期、core fact 和关键词；编辑可以带原因覆盖分类。
3. 编辑执行 `candidate select`。这是 Gate 1，重复或 unresolved possible duplicate 不可选择。
4. `brief create` 建立独立 Research Brief；`prompt create --purpose research` 生成可复制 prompt。
5. 人工研究并用 `research register` 录入 Markdown。系统随后生成 writing prompt。
6. 人工写稿并用 `article register` 录入，再用 `fact-check register` 录入核验结果。
7. 生成 image prompt，人工制作或寻找图片；`image register` 必须记录许可证或 AI provenance。
8. 编辑执行 `review`，确认标题、日期、来源、数字、claims、去重和图片权利。
9. 编辑执行 `approve`。这是 Gate 2。
10. `publish-package` 生成本地文件；人工发布后用 `publish` 记录公开 URL。

所有命令都可用 `uv run newsroom COMMAND --help` 或 `uv run newsroom GROUP COMMAND --help` 查看参数。Artifact register 命令不会覆盖已存在文件；publication package 也拒绝静默覆盖。

## Human checkpoints

人工始终负责选题、研究判断、写作、事实核验、图片生成或选择、最终 review、approve 和外部发布。系统只连接步骤并验证最低证据，不声称判断内容真伪。

## Recovery

SQLite transaction 保证失败命令不会产生半完成的数据库状态。输入文件仍在原路径；修正参数后可重跑。已注册 artifact 为防止覆盖而不可重复写入，需保留审计后显式更换（Phase 1 通过创建新候选处理）。
