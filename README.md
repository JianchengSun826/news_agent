# Tech Newsroom Agent

一个在 macOS 上通过 Finder 操作、人工把关的中文科技新闻生产工作流。当前 Phase 1.1 使用 Python 3.12、Pydantic、Typer 和 SQLite 保存状态；Gemini 只负责发现和整理审核后的 CSV，导入后由 Claude App 完成整批研究、写作、图片搜索和独立核验。无需 API key，没有网络或付费 API 集成。

## Quick start

```bash
uv sync --dev
uv run newsroom --root . init
uv run newsroom --root . batch prepare ~/Downloads/tech-newsroom-candidates.csv
```

在 Finder 打开 `workspace/README.md`，找到批次并双击 `workspace/batches/BATCH_ID/OPEN_BATCH.command`。批次 README 显示当前任务、文章版本和相对链接；`actions/` 的八个启动器完成 Claude 交接、图片登记、整批审核、批准和发布包。启动时 Terminal 可能短暂出现，文件选择和确认由 macOS 对话框完成。Finder 操作仅支持 macOS；候选审核与重复项歧义仍在导入前后由编辑处理。

`init` 只创建数据库与本地协调文件，不创建旧的 `research/`、`articles/`、`images/` 或 `output/`；准备批次时按需生成工作区。已有旧素材必须先阅读[迁移与恢复说明](docs/WORKFLOW.md#9-旧工作区迁移与恢复)，在副本中演练后使用 `workspace/Migrate Newsroom.command` 预检、确认、创建验证备份并迁移。旧目录完整保留在 journal 绑定的恢复树，程序不会自动删除。

## Workflow

第一个 Claude 对话接收一个整批 content request，返回研究简报、Markdown 稿件和每篇最多三个图片候选。将结果放入批次 `claude/inbox/`，双击导入操作。第二个全新 Claude 对话一次核验整批，逐篇返回 PASS/FAIL；FAIL 修订后再在新窗口核验。所有有效文章 PASS 后，每篇人工确认并下载一张图片，整批一次登记。整批审核、批准和发布包均先验证全部文章，失败不推进部分文章。

发布包位于 `workspace/batches/BATCH_ID/publish/ARTICLE_ID/`，整批索引为 `publish/batch-manifest.json`。人工上传 CMS 后，用 `batch publish-register` 一次登记全部真实 URL。候选选择后，改变内容和发布流程状态的命令统一属于 `batch`；`published list` 与 `stats` 等顶层只读命令仍受支持。旧单篇内容、图片、审核、批准和发布命令已移除。

`REUSED_COMPLETE` 不参与本批次编辑或发布。有 `owning_batch_id` 的复用文章链接到所属批次；历史记录可能没有拥有批次，README 无法提供批次素材链接，应按数据库记录的规范/旧路径查找，或先完成迁移与归属核对。

完整操作文件名和备用 CLI 见[工作流](docs/WORKFLOW.md)与[中文速查](docs/WORKFLOW_ZH.md)，字段见[数据模型](docs/DATA_MODEL.md)，Phase 边界见[路线图](docs/ROADMAP.md)。

## Offline demo and tests

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 每次重建独立的 `demo-workspace/`，用两个候选、合成 Claude JSON 和本地 SVG 走通全部批次闸门，两篇文章均到达 `PUBLISHED`。脚本验证批次 README、整批 manifest 与无旧运行目录；它不打开原生对话框、不访问网络、不操作真实数据库、不执行迁移。合成新闻、图片权利和示例 URL 仅用于验证。

## Design principles

- SQLite 是 Phase 1 的唯一 canonical database。
- 所有重要状态变化写入 append-only audit trail。
- Prompt 保存在 `prompts/`，代码不绑定模型供应商。
- 只保存来源 metadata、URL 和编辑笔记，不复制第三方全文。
- Claude 图片搜索建议需要原始落地页、权利页、作者、许可与署名；编辑确认后每篇登记一张原图。
- README、batch-state.json 和图片候选视图可从 SQLite 重建；活动素材集中在所属批次中。
- 当前阶段不包含网络抓取、定时任务、Web UI、自动图片下载或自动发布。
