# Phase 1.1 批处理操作流程

本流程的边界固定：Gemini 只负责定时发现、整理 Google Sheet 和生成 CSV；CSV 之后的研究、写作与图片搜索在第一个 Claude App 对话完成，事实核验在一个全新的 Claude 对话完成。CLI 不调用任何模型 API，不自动下载图片，也不自动发布。

## 1. 从 Gemini 获得 CSV

在 Google Sheet 的 `Inbox` 审核 Gemini 发现的新闻。确认 primary URL 支持 `core_fact`，移除错误 secondary URL，核对事件日期，并把通过项标为 `READY_TO_IMPORT`。从保留表头的 `Ready to Import` worksheet 下载 CSV。

CSV 至少需要以下列；额外审核列可以保留，CLI 会忽略它们：

```text
candidate_title,core_fact,event_date,company,product,topic,subtopic,event_type,keywords,primary_source,secondary_sources,discovered_from,priority
```

`keywords` 和多个 `secondary_sources` 使用 `|` 分隔。只有 primary 可靠时，把 `secondary_sources` 留空。
导出的尾部空行或仅含空白字符的整行会被 CLI 自动忽略；含有部分数据但缺少必填字段的行仍会拒绝整批导入并报告行号。

## 2. 初始化并准备整批文章

```bash
cd /Users/jianchengsun/agent/news_agent
uv sync --locked --dev
uv run newsroom init
uv run newsroom batch prepare ~/Downloads/tech-newsroom-candidates.csv
```

`init` 会幂等升级数据库；从旧 schema 升级前，备份写入 `data/backups/`。`batch prepare` 会验证整份 CSV、复用此前导入的候选、自动处理 `NEW`、创建文章记录，并返回：

- `batch_id`：后续命令使用的 `batch-...`；
- `content_request_path`：第一个 Claude 窗口要上传的 JSON；
- `counts` 和 `items`：批次状态。

同一文件重复执行会返回原批次。即使以前运行过 `candidate import`，也可以直接执行 `batch prepare`；系统按候选指纹复用数据。

查看进度：

```bash
uv run newsroom batch status BATCH_ID
```

状态输出始终包含 `content_request_path`；内容导入完成后还会包含当前版本的 `fact_check_request_path`，可直接用于定位要上传到 Claude 的文件。

## 3. 只处理有歧义的重复项

`NEW` 已自动进入写作。`WAITING_DUPLICATE` 才需要人工操作。

排除：

```bash
uv run newsroom batch resolve BATCH_ID CANDIDATE_ID \
  --decision exclude --actor editor --reason "与已选事件重复"
```

`FOLLOW_UP` 或 `RELATED` 可直接 include：

```bash
uv run newsroom batch resolve BATCH_ID CANDIDATE_ID \
  --decision include --actor editor --reason "属于有独立新闻价值的后续事件"
```

`POSSIBLE_DUPLICATE` 或 `DUPLICATE` 必须先记录人工覆盖，再 include：

```bash
uv run newsroom duplicate-check CANDIDATE_ID \
  --override NEW --actor editor --reason "来源与事件日期证明是独立事件"

uv run newsroom batch resolve BATCH_ID CANDIDATE_ID \
  --decision include --actor editor --reason "已完成重复项覆盖"
```

所有 `WAITING_DUPLICATE` 都完成 include 或 exclude 后，才会生成包含整批已选文章的 content request；此前 `content_request_path` 为空。以最新 `batch status` 返回的路径为准。旧批次若已导入部分内容却仍有未解决重复项，CLI 会在任何修改前拒绝继续 resolve；需人工恢复旧批次或在新批次导入内容前先完成重复项审核。

## 4. 第一个 Claude 窗口：整批研究、写作和图片搜索

新建一个 Claude App 对话，上传 `content_request_path` 指向的一个 JSON 文件，只发送一次：

```text
严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。
```

Claude 应返回一个可下载 JSON，包含整批 Research Brief、完整 Markdown 文章，以及每篇最多三个图片候选。图片必须有原始落地页和原始权利页面；不接受搜索缩略图、版权不明的媒体图片或仅以 fair use 为依据的图片。

下载后一次性导入：

```bash
uv run newsroom batch content-import BATCH_ID \
  ~/Downloads/BATCH_ID-content-result.json
```

CLI 会先验证整个 JSON。缺少文章、出现额外文章、slug 冲突或图片权利字段不完整时，整批不写入。成功后自动返回 `fact_check_request_path`。

## 5. 第二个 Claude 窗口：一次独立核验

必须新建 Claude 对话，上传 `fact_check_request_path`，仍只发送同一句固定指令。下载结果后运行：

```bash
uv run newsroom batch fact-check-import BATCH_ID \
  ~/Downloads/BATCH_ID-fact-check-result-v1.json
```

- `PASS` → `FACT_CHECKED`，进入图片人工确认；
- `FAIL` → `NEEDS_REVISION`，不会阻塞同批其他文章。

核验只做一次整批交接；每篇文章仍分别获得 PASS/FAIL 和 claim-level 报告。

## 6. 仅在 FAIL 时修订

```bash
uv run newsroom batch revision-request BATCH_ID
```

把返回的 revision request 上传到一个 Claude 对话，发送固定指令，下载后导入：

```bash
uv run newsroom batch revision-import BATCH_ID \
  ~/Downloads/BATCH_ID-revision-result-v2.json
```

CLI 保留 v1，写入 `research-brief-v2.md` 和 `article-v2.md`，并自动生成新的 fact-check request。使用新的 Claude 对话核验，再运行 `fact-check-import`。旧 request 的结果不能用于新一轮。

## 7. 人工确认并登记图片

在批次目录的 `articles/ARTICLE_ID/image-candidates.json` 查看图片候选。对每篇有效文章选中的 `image_candidate_id`：

1. 打开 `landing_page_url` 和 `rights_url`；
2. 确认作者、许可、署名要求与记录一致；
3. 人工下载原图并检查画面、格式和分辨率；
4. 把全部选择写入一个 JSON，其中 `image_file` 为人工下载文件的本地路径，`rights_confirmed` 必须显式为 `true`：

```json
{
  "schema_version": 1,
  "batch_id": "BATCH_ID",
  "items": [
    {
      "article_id": "ARTICLE_ID",
      "image_candidate_id": "IMAGE_CANDIDATE_ID",
      "image_file": "/absolute/path/to/hero.jpg",
      "rights_confirmed": true
    }
  ]
}
```

```bash
uv run newsroom batch image-plan BATCH_ID ~/Downloads/BATCH_ID-image-plan.json
```

CLI 不下载图片，也不替编辑判断版权。缺少有效文章、候选属于另一篇文章、缺少本地文件或未显式确认权利时，整批拒绝登记。

## 8. 审核、批准、发布包和发布登记

整批发布闸门以 SQLite 中的有效条目为准：`EXCLUDED` 与 `REUSED_COMPLETE` 不阻塞操作、不改变文章状态，也不接收当前批次的发布包或要求发布 URL。整批审核要求全部有效文章为 `IMAGE_READY`；整批批准要求全部为 `REVIEWED`。先验证整批，再在一个事务内记录相同审核人及理由的文章状态转换。

整批发布包要求全部有效文章为 `APPROVED`。系统先暂存并验证各篇当前版本的文章、研究简报、事实核验、图片、metadata、许可、来源及 checklist，以及 `batch-manifest.json`，再替换 `workspace/batches/BATCH_ID/publish/`。失败或中断会保留之前的发布目录并回滚数据库；成功后批次为 `PACKAGED`，文章仍为 `APPROVED`。数据库提交和工作区刷新成功后，旧包备份清理若失败，命令仍返回成功；残留的 `.previous-publish-*` 只是不作为当前发布包的恢复数据。人工上传后，以包含 `schema_version: 1`、正确 `batch_id` 和 `items`（每项为 `article_id`、HTTP(S) `article_url`）的 JSON 登记整批发布结果；文章集合必须恰好对应全部有效文章，缺失、额外或重复项均整批拒绝。成功登记在一个事务内更新文章 URL、时间、审计及 `PUBLISHED` 状态。

```bash
uv run newsroom batch review BATCH_ID \
  --actor editor --reason "已检查文章、核验报告、图片与权利页面"

uv run newsroom batch approve BATCH_ID \
  --actor editor --reason "当前版本及发布素材已确认"

uv run newsroom batch publish-packages BATCH_ID
```

发布包位于 `workspace/batches/BATCH_ID/publish/ARTICLE_ID/`，包含当前版本的 `article.md`、Research Brief、fact-check、图片、来源、metadata、图片权利记录和 checklist。人工上传到 CMS 后，用真实 URL 登记：

```bash
uv run newsroom batch publish-register BATCH_ID \
  ~/Downloads/BATCH_ID-publication-result.json --actor editor

uv run newsroom published list
uv run newsroom stats
uv run newsroom batch refresh-workspace BATCH_ID
```

选择后的受支持 CLI 操作统一使用 `batch`；单篇文章服务仅保留为内部恢复和构建辅助，不再注册为用户命令。`refresh-workspace` 从 SQLite 重建工作区视图，不改变流程状态。

仍需人工完成的步骤只有：Sheet 来源审核、重复项歧义判断、两个 Claude 文件交接、图片权利页与文件检查、最终编辑批准、CMS 上传和真实 URL 登记。

迁移旧工作区前，可运行 `uv run newsroom workspace migrate --preflight`。此命令只读取数据库与文件，并把确定性的 JSON 清单保存到 `data/workspace-migration-preflight.json`；它不会更新流程状态、移动素材或创建迁移暂存目录。缺失的数据库引用、路径逃逸、符号链接、归属歧义或目标冲突会阻塞迁移并返回非零状态。可重复使用 `--source-csv PATH` 指定原始 CSV，只有字节级 SHA-256 与已记录批次一致时才会列为可复制输入；缺失原始 CSV 不会阻塞。

当前命令仅支持预检，尚不执行迁移。内部备份服务仅接受仍然有效且没有阻塞问题的清单，在 `data/backups/` 创建 SQLite 备份、素材归档、报告和 manifest，并重新打开数据库、检查归档成员安全性、提取并核对所有文件哈希后才返回。失败的备份会删除或标记 `INCOMPLETE`，不得用于迁移。

## 9. 验证与离线 Demo

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 在 `demo-workspace/` 内生成合成 Claude JSON，完整走通批次准备、内容导入、独立核验、图片登记、批准、发布包和发布登记，不访问网络或模型服务。
