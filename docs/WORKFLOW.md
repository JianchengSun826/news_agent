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

每次 include 都会生成新的不可变 content request；以最新 `batch status` 返回的路径为准。

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

查看某篇文章的候选：

```bash
uv run newsroom image candidates ARTICLE_ID
```

对选中的 `image_candidate_id`：

1. 打开 `landing_page_url` 和 `rights_url`；
2. 确认作者、许可、署名要求与 CLI 记录一致；
3. 人工下载原图并检查画面、格式和分辨率；
4. 显式确认权利并登记：

```bash
uv run newsroom image register ARTICLE_ID \
  --candidate IMAGE_CANDIDATE_ID \
  --file ~/Downloads/hero.jpg \
  --rights-confirmed
```

CLI 不下载图片，也不替编辑判断版权。候选属于另一篇文章、未提供文件或未写 `--rights-confirmed` 时会拒绝登记。

## 8. 审核、批准、发布包和发布登记

```bash
uv run newsroom review ARTICLE_ID \
  --actor editor --reason "已检查文章、核验报告、图片与权利页面"

uv run newsroom approve ARTICLE_ID \
  --actor editor --reason "当前版本及发布素材已确认"

uv run newsroom publish-package ARTICLE_ID
```

发布包位于 `output/ARTICLE_ID/`，包含当前版本的 `article.md`、Research Brief、fact-check、图片、来源、metadata、图片权利记录和 checklist。人工上传到 CMS 后，用真实 URL 登记：

```bash
uv run newsroom publish ARTICLE_ID \
  --actor editor --url "https://your-site.example/article-slug"

uv run newsroom published list
uv run newsroom stats
```

仍需人工完成的步骤只有：Sheet 来源审核、重复项歧义判断、两个 Claude 文件交接、图片权利页与文件检查、最终编辑批准、CMS 上传和真实 URL 登记。

## 9. 验证与离线 Demo

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 在 `demo-workspace/` 内生成合成 Claude JSON，完整走通批次准备、内容导入、独立核验、图片登记、批准、发布包和发布登记，不访问网络或模型服务。
