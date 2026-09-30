# Phase 1 操作流程

## 1. 首次初始化

```bash
cd /Users/jianchengsun/agent/news_agent
uv sync --locked --dev
uv run newsroom init
```

正式数据库位于 `data/newsroom.db`。查看命令参数：

```bash
uv run newsroom --help
uv run newsroom candidate add --help
```

## 2. 设置 Google Sheet

创建私有 spreadsheet：

- 文件名：`Tech Newsroom Candidates`
- 第一个 worksheet：`Inbox`
- 使用运行 Gemini Spark 的同一 Google Account 创建，不要共享

将以下表头按顺序粘贴到 `Inbox` 第一行：

```text
discovered_at,event_date,candidate_title,core_fact,company,product,topic,subtopic,event_type,keywords,primary_source,primary_source_status,secondary_sources,secondary_source_status,rejected_secondary_sources,source_status,source_review_reason,discovered_from,priority,duplicate_hint,candidate_add_command,review_status,notes
```

选择范围，点击 `Data → Data validation → Add rule → Dropdown`。显示方式选 `Chip`，无效数据选 `Reject input`：

| 范围 | 选项 |
| --- | --- |
| `G2:G` | `AI`, `AI Agent`, `AI Coding`, `Semiconductors`, `Cloud`, `Data Centers`, `Developer Tools`, `Robotics`, `Autonomous Vehicles`, `Cybersecurity`, `Technology Business` |
| `I2:I` | `launch`, `update`, `research`, `funding`, `acquisition`, `security-patch`, `security-incident`, `shipment`, `policy`, `earnings`, `partnership`, `other` |
| `L2:L` | `UNVERIFIED_BY_HUMAN`, `VERIFIED`, `INVALID` |
| `N2:N` | `NOT_PROVIDED`, `UNVERIFIED_BY_HUMAN`, `VERIFIED`, `PARTIALLY_VERIFIED`, `INVALID` |
| `P2:P` | `PRIMARY_ONLY`, `SECONDARY_UNVERIFIED`, `CORROBORATED`, `SOURCE_CONFLICT`, `INSUFFICIENT` |
| `S2:S` | `1`, `2`, `3`, `4`, `5` |
| `T2:T` | `NEW`, `FOLLOW_UP`, `RELATED`, `POSSIBLE_DUPLICATE`, `DUPLICATE` |
| `V2:V` | `PENDING`, `READY_TO_IMPORT`, `IMPORTED`, `ON_HOLD`, `REJECTED` |

新建 worksheet `Ready to Import`，在 `A1` 输入：

```text
={Inbox!A1:W1; IFNA(FILTER(Inbox!A2:W, Inbox!V2:V="READY_TO_IMPORT"),)}
```

不要直接编辑 `Ready to Import`；所有修改都在 `Inbox` 完成。

## 3. 设置 Gemini Spark

1. 在 Gemini 连接 Google Workspace。
2. 切换到 Spark，新建定时 Task。
3. 复制完整的 [Gemini Spark Prompt](../prompts/gemini_spark_discovery.md)。
4. 确认 schedule、时区、spreadsheet 名称和 worksheet 名称。
5. 执行一次 `Run now`，确认 Gemini 只向 `Inbox` 追加行。

## 4. 审核每日候选

在 `Inbox` 筛选 `review_status = PENDING`，逐行操作：

1. 打开 `primary_source`，确认页面存在并支持 `core_fact`。
2. 核对 `event_date` 是事件日期。
3. 检查最近 30 天是否已有相同事件。
4. 有 secondary URL 时，确认它存在、支持同一事实，并且与 primary 独立。
5. 修正 title、分类、关键词、priority 和 notes。

按审核结果填写：

| 结果 | 状态设置 |
| --- | --- |
| Primary 和 secondary 均有效 | `primary_source_status=VERIFIED`、`secondary_source_status=VERIFIED`、`source_status=CORROBORATED`、`review_status=READY_TO_IMPORT` |
| Primary 有效但没有 secondary | `primary_source_status=VERIFIED`、`secondary_source_status=NOT_PROVIDED`、`source_status=PRIMARY_ONLY`、`review_status=READY_TO_IMPORT` |
| Secondary 错误但 primary 有效 | 清空 `secondary_sources`，把错误 URL 移到 `rejected_secondary_sources`，设置 `secondary_source_status=INVALID`、`source_status=PRIMARY_ONLY`、`review_status=READY_TO_IMPORT` |
| Primary 不支持核心事实 | `primary_source_status=INVALID`、`source_status=INSUFFICIENT`、`review_status=REJECTED` |
| 暂时无法判断 | `review_status=ON_HOLD`，在 `notes` 写明原因 |

## 5. 下载并导入候选

1. 打开 `Ready to Import` worksheet。
2. 选择 `File → Download → Comma-separated values (.csv)`。
3. 在项目目录运行：

```bash
uv run newsroom candidate import \
  ~/Downloads/tech-newsroom-candidates.csv
```

4. 保存命令输出的所有 `candidate_ids`。
5. 确认导入结果：

```bash
uv run newsroom candidate list
uv run newsroom candidate show CANDIDATE_ID
```

6. 导入成功后，把对应 `Inbox` 行的 `review_status` 改为 `IMPORTED`。导入失败时保留 `READY_TO_IMPORT`，修正后重新下载。

## 6. 手动添加单条候选

只有 primary source：

```bash
uv run newsroom candidate add \
  --candidate-title "示例标题" \
  --core-fact "Company released Product on 2026-09-29." \
  --event-date "2026-09-29" \
  --company "Company" \
  --product "Product" \
  --topic "AI" \
  --subtopic "Developer Tools" \
  --event-type "launch" \
  --primary-source "https://company.example/announcement" \
  --keyword "company" \
  --priority 4
```

有独立来源时增加一个或多个：

```bash
--secondary-source "https://media.example/report"
```

没有可靠 secondary 时完全省略该参数。

## 7. 处理导入后的候选

每个 `CANDIDATE_ID` 先运行去重：

```bash
uv run newsroom duplicate-check CANDIDATE_ID
```

需要人工覆盖时：

```bash
uv run newsroom duplicate-check CANDIDATE_ID \
  --override NEW \
  --actor editor \
  --reason "确认是独立事件"
```

采用候选：

```bash
uv run newsroom candidate select CANDIDATE_ID \
  --actor editor \
  --reason "来源和新闻价值已确认"
```

不采用候选：

```bash
uv run newsroom candidate reject CANDIDATE_ID \
  --actor editor \
  --reason "来源不足或价值有限"
```

## 8. 研究、写作与事实核验

创建 Research Brief，并记录输出的 `ARTICLE_ID`：

```bash
uv run newsroom brief create CANDIDATE_ID
uv run newsroom prompt create ARTICLE_ID --purpose research
```

完成研究后登记 Markdown：

```bash
uv run newsroom research register ARTICLE_ID --file research/manual-result.md
```

生成写作 prompt，完成文章后登记：

```bash
uv run newsroom prompt create ARTICLE_ID --purpose writing
uv run newsroom article register ARTICLE_ID \
  --file articles/my-article.md \
  --title "中文标题" \
  --slug "article-slug"
```

完成事实核验后登记：

```bash
uv run newsroom prompt create ARTICLE_ID --purpose fact_check
uv run newsroom fact-check register ARTICLE_ID \
  --file articles/my-fact-check.md
```

## 9. 图片、审核与发布

生成图片 prompt：

```bash
uv run newsroom prompt create ARTICLE_ID --purpose image
```

先准备好本地图片，例如：

```text
~/Downloads/hero.png
```

登记 AI 图片：

```bash
uv run newsroom image register ARTICLE_ID \
  --file ~/Downloads/hero.png \
  --type AI_GENERATED \
  --ai-provider Gemini \
  --ai-model manual-subscription \
  --prompt-path "output/prompts/ARTICLE_ID-image.md"
```

登记外部授权图片：

```bash
uv run newsroom image register ARTICLE_ID \
  --file ~/Downloads/hero.jpg \
  --type WIKIMEDIA \
  --source-url "https://commons.wikimedia.org/example" \
  --creator "Creator Name" \
  --license "CC BY 4.0" \
  --license-url "https://creativecommons.org/licenses/by/4.0/" \
  --attribution "Creator Name, CC BY 4.0"
```

登记时，CLI 会把图片复制为 `images/ARTICLE_ID/hero.EXT`，并把相对路径、来源、许可证或 AI provenance 保存到 SQLite。目标文件已存在时不会覆盖。

完成最终审核和批准：

```bash
uv run newsroom review ARTICLE_ID \
  --actor editor \
  --reason "已检查事实、来源、标题和图片权利"

uv run newsroom approve ARTICLE_ID \
  --actor editor \
  --reason "批准生成发布包"
```

生成发布包：

```bash
uv run newsroom publish-package ARTICLE_ID
```

最终新闻以本地发布包呈现：

```text
output/ARTICLE_ID/
├── article.md
├── hero.png
├── metadata.json
├── sources.md
├── research-brief.md
├── fact-check.md
├── image-brief.md
├── image-license.json
└── publication-checklist.md
```

- `article.md` 是提供给读者的 Markdown 新闻正文。
- `hero.png` 表示已登记的实际图片；扩展名与输入图片一致。
- `metadata.json` 保存标题、slug、分类和发布 metadata。
- `sources.md`、`research-brief.md` 和 `fact-check.md` 供编辑核验。
- `image-license.json` 保存图片授权或 AI 生成记录。
- `publication-checklist.md` 是发布前检查清单。

发布包包含实际图片，但不会自动生成网页或上传 CMS。发布时，把发布包中的 `article.md` 和 `hero.EXT` 上传到 WordPress、Ghost、微信公众号或其他平台。

人工发布后登记 URL：

```bash
uv run newsroom publish ARTICLE_ID \
  --actor editor \
  --url "https://your-site.example/article-slug"
```

查看结果：

```bash
uv run newsroom published list
uv run newsroom stats
```

## 10. 测试与 Demo

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 使用 `demo-workspace/`，不会修改正式数据库。
