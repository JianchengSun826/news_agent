# Tech Newsroom Agent

一套面向中文科技新闻的人机协作生产工具：由编辑使用 Claude 研究与写作，程序负责历史去重、任务交接、流程检查和最终文件整理。

## 为什么使用这个项目？

如果只是偶尔写一篇新闻，直接让 Claude 搜索要点并撰稿通常更省事。这个项目更适合持续运营、每次处理多篇新闻，并希望统一核验标准和交付方式的个人编辑或小型团队。

项目的主要价值是减少重复选题、漏核验、混批和旧稿误用，让多篇新闻能够按同一套规则完成。文章质量仍取决于来源、模型表现和人工判断。

| 编辑要解决的问题 | 每次临时使用 Claude | 本项目提供的支持 |
| --- | --- | --- |
| 这个事件是否已经写过？ | 自行翻查旧稿和聊天记录 | 保存跨批次历史事件，比较重复和相似选题，歧义交给编辑判断 |
| 如何保持要求一致？ | 自己维护提示词和每次交接内容 | 统一任务文件包含选题、来源、写作要求和输出格式，支持整批交接 |
| 返回结果是否齐全、属于当前任务？ | 人工核对文章、文件名和版本 | 检查文章集合、必填字段、批次与请求身份，拒绝缺项和过期结果 |
| 是否完成了核验和修订？ | 编辑自行安排并记住进度 | 在新对话中独立核验，逐篇记录 PASS/FAIL，失败稿件修订后重新核验 |
| 图片能否使用？ | 自行保存授权依据和署名 | 区分已核实候选与待确认线索；人工确认图片或无图，有图时登记权利记录 |
| 最终稿件保存在哪里？ | 自己整理正文、图片和历史文件 | 每篇一个最终成果文件夹，验证后清理过程材料，SQL 去重与业务记录继续保留 |

**编辑负责选题、来源判断、模型交互、图片权利、最终审核和发布；程序负责能确定检查的身份、结构、版本与状态。** 完成后获得正文和已登记图片即可交付，上传 CMS 和登记发布 URL 是可选后续操作。

需要了解的边界：独立核验仍可能出错，PASS 不代表事实绝对正确；项目尚无自动搜索、自动发布或 Google Docs 转换。完成清理后不保留原始研究、完整核验报告和历史稿件。效率与质量收益需要实际使用数据支持，尚未测得节省时间或提升准确率的百分比。

详细对比、8 篇批次示例、适用场景及效果衡量方法见[项目价值说明](docs/PROJECT_VALUE_ZH.md)。

## Quick start

当前 Phase 1.1 采用 macOS Finder、Python 3.12、Pydantic、Typer 和 SQLite。Gemini 负责准备经审核的 CSV，导入后由人工在 Claude App 中完成研究、写作与核验。程序无需 API key、不调用付费模型 API；Claude 账户费用按实际服务计费。

```bash
uv sync --locked
uv run newsroom --root . init
uv run newsroom --root . batch prepare ~/Downloads/tech-newsroom-candidates.csv
```

在 Finder 打开 `workspace/README.md`，找到批次并双击 `workspace/batches/BATCH_ID/OPEN_BATCH.command`。批次 README 显示当前任务、文章版本和相对链接；`actions/` 的四个启动器（02–05）完成结果导入、材料查看、图片登记和确认导出（合并原 05–08）。启动时 Terminal 可能短暂出现，文件选择和确认由 macOS 对话框完成。Finder 操作仅支持 macOS；候选审核与重复项歧义仍在导入前后由编辑处理。

`init` 初始化或升级数据库 schema，并幂等写入配置的来源种子；它不移动文章素材，也不创建旧的 `research/`、`articles/`、`images/` 或 `output/`。普通 `newsroom init` 自行持有仓库独占锁，检查迁移屏障与 journal，并在备份或写入前完成 schema、WAL、sidecar 和复验闸门；不要在外层再套 operation lock。当前可执行程序最高支持 schema v5，并拒绝未来版本、负版本、无有效版本行的版本表，以及含用户表却没有 `schema_version` 的未知数据库；只有不存在的数据库或没有用户表的空 SQLite 数据库可作为新库初始化。rehearsal copy 前先停止所有 newsroom/Finder 操作及 SQLite 写入者，在独占 operation lock 内检查 journal/WAL 状态，并用 SQLite backup API 捕获连贯数据库。production init 的 `copy2` 安全快照在 WAL 成功 checkpoint、连接关闭、sidecar 清除和复验后创建；任何 busy、不完整或残留 sidecar 都会在快照与 schema 写入前停止。schema v1-v4 才应生成一份新的升级安全快照；已是 v5 时不应生成升级快照或改变 schema；其他版本必须停止。该安全副本不能替代迁移系统的 verified backup。已有旧素材须在包含现有 `workspace/` 且保留符号链接身份的连贯副本中演练后再迁移。迁移若归档了版本库中旧 `output/.gitkeep`，Git 显示它被删除是预期的代码清理；提交该删除，成功 receipt 后不要在运行目录手工重建它。

## Workflow

在批次 README 的“当前 Claude 任务”查看上传文件与预期返回名；手动打开 Claude 并上传，无需 01 启动器或重复长指令。如需文字输入，发送“执行附件任务”。第一个 Claude 对话接收一个整批 content request，返回研究简报、Markdown 稿件和每篇最多三个图片候选。将结果放入批次 `claude/inbox/`，双击导入操作。第二个全新 Claude 对话一次核验整批，逐篇返回 PASS/FAIL；FAIL 修订后再在新窗口核验。所有有效文章 PASS 后，每篇可明确选择无图，或人工确认并下载一张图片，整批一次登记。操作 05 一次确认审核和批准，生成发布包并刷新；每个闸门均整批验证。打包失败可从已完成的审批阶段重试。

最终正文和已登记图片集中在 `workspace/completed/BATCH_ID/文章slug/`，05 完成后直接打开这里，并在验证成功后删除整个批次过程目录：CSV 副本、Claude 文件、研究/核查、历史版本、推荐图片及完整中间发布包等。SQLite 去重、权利和审计记录完整保留；未完成批次、外部原文件及备份不清理。删除中断可重试 `batch export`，不重复审批。完成目录中的人工修改会受到覆盖保护。获得正文和已登记图片即完成本地内容任务；明确无图也可完成。上传 CMS 和 `batch publish-register` 登记真实 URL 为可选后续操作。候选选择后，改变内容和发布流程状态的命令统一属于 `batch`；`published list` 与 `stats` 等顶层只读命令仍受支持。旧单篇内容、图片、审核、批准和发布命令已移除。

`REUSED_COMPLETE` 不参与本批次编辑或发布。有 `owning_batch_id` 的复用文章链接到所属批次；历史记录可能没有拥有批次，README 无法提供批次素材链接，应按数据库记录的规范/旧路径查找，或先完成迁移与归属核对。

产品定位与能力见[产品介绍](docs/PRODUCT_OVERVIEW_ZH.md)。与临时使用 Claude 写新闻的区别见[项目价值说明](docs/PROJECT_VALUE_ZH.md)。完整操作文件名和备用 CLI 见[工作流](docs/WORKFLOW.md)与[中文速查](docs/WORKFLOW_ZH.md)，字段见[数据模型](docs/DATA_MODEL.md)，Phase 边界见[路线图](docs/ROADMAP.md)。

## 运行检查

```bash
uv run newsroom --help
uv run newsroom --root . stats
```

当前仓库只保留运行所需文件；测试、示例、演示和旧开发记录已移除。

## Design principles

- SQLite 是 Phase 1 的唯一 canonical database。
- 所有重要状态变化写入 append-only audit trail。
- Prompt 保存在 `prompts/`，代码不绑定模型供应商。
- 只保存来源 metadata、URL 和编辑笔记，不复制第三方全文。
- Claude 图片搜索建议需要原始落地页、权利页、作者、许可与署名；编辑可明确选择无图，或确认权利后登记一张原图。
- README、batch-state.json 和图片候选视图可从 SQLite 重建；活动素材集中在所属批次中。
- 当前阶段不包含网络抓取、定时任务、Web UI、自动图片下载或自动发布。

## Optional images and batch housekeeping

04 支持逐篇选择图片或明确无图，完成后统一进入素材就绪状态；无图也可审核、批准、打包与登记发布。候选可用 `batch image-candidates-import` 补充，稿件与核验保持不变。整理旧批次请用 `batch archive` / `batch unarchive` 保留素材，勿手动删除批次目录。具体格式和恢复指引见 [工作流](docs/WORKFLOW.md)。
