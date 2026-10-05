# Phase 1 人工操作流程

本流程覆盖从 Gemini 定时发现、Google Sheet 审核、候选导入，到研究、写作、事实核验、配图、批准和人工发布的完整操作。Phase 1 不会自动搜索、调用模型或上传 CMS；每个高影响步骤都由编辑确认。

## 1. 初始化项目

```bash
cd /Users/jianchengsun/agent/news_agent
uv sync --locked --dev
uv run newsroom init
```

数据库位于 `data/newsroom.db`。需要查看参数时运行：

```bash
uv run newsroom --help
uv run newsroom COMMAND --help
```

文档中的 `CANDIDATE_ID` 和 `ARTICLE_ID` 都是占位符，必须替换为命令实际返回的 `cand-...` 和 `art-...`。

## 2. 设置 Google Sheet 和 Gemini Discovery

创建私有 spreadsheet `Tech Newsroom Candidates`，第一个 worksheet 命名为 `Inbox`。第一行使用以下表头：

```text
discovered_at,event_date,candidate_title,core_fact,company,product,topic,subtopic,event_type,keywords,primary_source,primary_source_status,secondary_sources,secondary_source_status,rejected_secondary_sources,source_status,source_review_reason,discovered_from,priority,duplicate_hint,candidate_add_command,review_status,notes
```

为审核字段添加下拉菜单：

| 列 | 允许值 |
| --- | --- |
| `topic` | `AI`, `AI Agent`, `AI Coding`, `Semiconductors`, `Cloud`, `Data Centers`, `Developer Tools`, `Robotics`, `Autonomous Vehicles`, `Cybersecurity`, `Technology Business` |
| `event_type` | `launch`, `update`, `research`, `funding`, `acquisition`, `security-patch`, `security-incident`, `shipment`, `policy`, `earnings`, `partnership`, `other` |
| `primary_source_status` | `UNVERIFIED_BY_HUMAN`, `VERIFIED`, `INVALID` |
| `secondary_source_status` | `NOT_PROVIDED`, `UNVERIFIED_BY_HUMAN`, `VERIFIED`, `PARTIALLY_VERIFIED`, `INVALID` |
| `source_status` | `PRIMARY_ONLY`, `SECONDARY_UNVERIFIED`, `CORROBORATED`, `SOURCE_CONFLICT`, `INSUFFICIENT` |
| `priority` | `1`, `2`, `3`, `4`, `5` |
| `duplicate_hint` | `NEW`, `FOLLOW_UP`, `RELATED`, `POSSIBLE_DUPLICATE`, `DUPLICATE` |
| `review_status` | `PENDING`, `READY_TO_IMPORT`, `IMPORTED`, `ON_HOLD`, `REJECTED` |

新建 worksheet `Ready to Import`，在 `A1` 输入：

```text
={Inbox!A1:W1; IFNA(FILTER(Inbox!A2:W, Inbox!V2:V="READY_TO_IMPORT"),)}
```

在 Gemini Spark 创建定时 Task，使用 `prompts/gemini_spark_discovery.md`。确认它只向 `Inbox` 追加新行，不覆盖表头或旧记录。

## 3. 审核候选来源

在 `Inbox` 筛选 `review_status=PENDING`，逐行检查：

1. 打开 primary URL，确认页面真实存在并支持 `core_fact`。
2. 核对 `event_date` 是事件发生日期，而非转载日期。
3. 检查最近 30 天是否已记录相同事件。
4. 打开每个 secondary URL，确认它支持相同核心事实且与 primary 独立。
5. 修正标题、分类、关键词、优先级和备注。

按结果设置状态：

| 审核结果 | 操作 |
| --- | --- |
| 两个来源均有效 | 设置 `CORROBORATED` 和 `READY_TO_IMPORT` |
| 只有 primary 有效 | 清空 `secondary_sources`，设置 `PRIMARY_ONLY` 和 `READY_TO_IMPORT` |
| Secondary 错误或伪造 | 移入 `rejected_secondary_sources`，清空 `secondary_sources`，设置 `INVALID`、`PRIMARY_ONLY`、`READY_TO_IMPORT` |
| Primary 不支持核心事实 | 设置 `INSUFFICIENT` 和 `REJECTED` |
| 来源互相冲突 | 设置 `SOURCE_CONFLICT` 和 `ON_HOLD`，在 notes 记录冲突 |

单一可靠 primary source 可以进入流程，但文章必须把厂商能力声明明确归因，并说明缺少独立核验。不要为了凑足两个来源保留错误 secondary URL。

## 4. 下载 CSV 并导入候选

在 `Ready to Import` 选择 `File → Download → Comma-separated values (.csv)`，然后运行：

```bash
uv run newsroom candidate import \
  ~/Downloads/tech-newsroom-candidates.csv
```

查看候选摘要：

```bash
uv run newsroom candidate list
```

从输出复制真实 ID，再查看完整记录：

```bash
uv run newsroom candidate show CANDIDATE_ID
```

`candidate list` 用于找到 ID、状态和优先级；`candidate show` 用于检查核心事实、来源、关键词和去重结果。不要原样输入 `CANDIDATE_ID` 或 `candidate_id`。

导入成功后，将 Sheet 对应行改为 `IMPORTED`。导入失败时保留 `READY_TO_IMPORT`，修正后重新下载 CSV。

需要绕过 Sheet 手动添加一条候选时：

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

只有在 secondary 已核验时才增加：

```bash
--secondary-source "https://media.example/report"
```

## 5. 去重并选择候选

```bash
uv run newsroom duplicate-check CANDIDATE_ID
```

- `NEW`：可以继续选择。
- `POSSIBLE_DUPLICATE`、`RELATED` 或 `FOLLOW_UP`：人工比较事件日期、公司、产品和核心事实。
- `DUPLICATE`：通常不继续；确有理由时才覆盖。

人工覆盖示例：

```bash
uv run newsroom duplicate-check CANDIDATE_ID \
  --override NEW \
  --actor editor \
  --reason "确认是独立事件"
```

选择或拒绝候选：

```bash
uv run newsroom candidate select CANDIDATE_ID \
  --actor editor \
  --reason "来源已审核，去重结果允许进入研究阶段"

uv run newsroom candidate reject CANDIDATE_ID \
  --actor editor \
  --reason "来源不足或新闻价值有限"
```

选择成功后再次运行 `candidate show`，保存生成的 `event_id`。

## 6. 创建并登记 Research Brief

创建文章记录，并保存返回的 `ARTICLE_ID`：

```bash
uv run newsroom brief create CANDIDATE_ID
uv run newsroom prompt create ARTICLE_ID --purpose research
```

把生成的 research Prompt 交给 Gemini。要求它只使用可访问来源，区分 `FACT`、`COMPANY_CLAIM` 和 `ANALYSIS`，列出无法核验的信息，并使用直接 URL，不使用 `[cite: 1]` 等内部引用。

将结果保存为：

```bash
touch research/ARTICLE_ID-result.md
open -e research/ARTICLE_ID-result.md
```

粘贴并保存后检查：

```bash
wc -l research/ARTICLE_ID-result.md
sed -n '1,160p' research/ARTICLE_ID-result.md
```

确认文件是正常多行 Markdown，包含来源、未知信息、冲突和避免事项。若 secondary 无法核验，Research Brief 必须明确写明文章只能依赖 primary，并要求归因。

登记通过的研究结果：

```bash
uv run newsroom research register ARTICLE_ID \
  --file research/ARTICLE_ID-result.md
```

成功状态应为 `RESEARCHED`，正式文件位于 `research/ARTICLE_ID/research-brief.md`。

## 7. 生成并登记文章草稿

```bash
uv run newsroom prompt create ARTICLE_ID --purpose writing
```

向 Gemini 提供：

- `output/prompts/ARTICLE_ID-writing.md`
- `research/ARTICLE_ID/research-brief.md`

要求输出约 600–1000 个中文字符，并包含一级标题、至少三个二级标题和来源列表。为了获得原始 Markdown，要求 Gemini 把全文放入一个 `markdown` 代码块，代码块外不要输出说明。复制时不要保留最外层代码围栏。

保存并检查：

```bash
touch articles/ARTICLE_ID-draft.md
open -e articles/ARTICLE_ID-draft.md
wc -l articles/ARTICLE_ID-draft.md
sed -n '1,160p' articles/ARTICLE_ID-draft.md
```

登记草稿：

```bash
uv run newsroom article register ARTICLE_ID \
  --file articles/ARTICLE_ID-draft.md \
  --title "中文文章标题" \
  --slug "lowercase-ascii-slug"
```

成功状态应为 `DRAFTED`。`slug` 只能使用小写 ASCII 字母、数字和连字符。

## 8. 事实核验

```bash
uv run newsroom prompt create ARTICLE_ID --purpose fact_check
```

向 Gemini 提供：

- `output/prompts/ARTICLE_ID-fact_check.md`
- `research/ARTICLE_ID/research-brief.md`
- `articles/ARTICLE_ID/article.md`

要求报告第一行为 `# Fact Check: PASS` 或 `# Fact Check: FAIL`，并用 Markdown 表格逐项检查日期、数字、适用用户、产品能力、安全机制、计费、来源和归因。

保存报告：

```bash
touch articles/ARTICLE_ID-fact-check.md
open -e articles/ARTICLE_ID-fact-check.md
sed -n '1,200p' articles/ARTICLE_ID-fact-check.md
```

如果结果是 `FAIL`，先按报告修正 `articles/ARTICLE_ID/article.md`，然后重新核验。只有没有阻断问题的 `PASS` 报告才能登记：

```bash
uv run newsroom fact-check register ARTICLE_ID \
  --file articles/ARTICLE_ID-fact-check.md
```

成功状态应为 `FACT_CHECKED`。

## 9. 生成、检查并登记图片

```bash
uv run newsroom prompt create ARTICLE_ID --purpose image
```

把 image Prompt 和正式文章交给图片模型。图片要求：

- 16:9 科技新闻编辑插画。
- 不包含文字、字母、数字、Logo、商标、水印或人物肖像。
- 不复制现有产品宣传图或模仿在世艺术家。
- 画面应直接支持文章主题。

下载后检查真实格式和尺寸：

```bash
file ~/Downloads/hero.png
sips -g pixelWidth -g pixelHeight ~/Downloads/hero.png
open ~/Downloads/hero.png
```

必须人工查看图片，不能只看文件名。若扩展名与真实格式不一致，先转换；若比例不准确，先裁切。示例：

```bash
sips --cropToHeightWidth 1530 2720 \
  -s format png \
  ~/Downloads/hero.png \
  --out ~/Downloads/hero-final.png
```

确认图片没有可读文字、畸形 UI、品牌标识或明显生成错误后再登记：

```bash
uv run newsroom image register ARTICLE_ID \
  --file ~/Downloads/hero-final.png \
  --type AI_GENERATED \
  --ai-provider Gemini \
  --ai-model "Gemini image generation" \
  --prompt-path "output/prompts/ARTICLE_ID-image.md"
```

成功状态应为 `IMAGE_READY`。登记后若又编辑图片，必须保留原图、保存新版本，并同步更新真实的生成/编辑来源；不要让扩展名、文件内容和 provenance 不一致。

使用外部授权图片时登记完整授权信息：

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

## 10. 最终审核、批准和发布包

最终审核前确认：

1. 正文只使用已核验来源。
2. `secondary_sources` 不包含无法访问、错误或伪造的 URL。
3. Fact Check 为 `PASS`。
4. 图片已经人工查看，格式、比例和来源记录正确。
5. 标题、slug、日期和公司声明归因正确。

登记人工审核：

```bash
uv run newsroom review ARTICLE_ID \
  --actor editor \
  --reason "已检查文章、来源、事实核验和配图"
```

最终批准：

```bash
uv run newsroom approve ARTICLE_ID \
  --actor editor \
  --reason "文章内容、来源归因和发布图片均已确认"
```

生成发布包：

```bash
uv run newsroom publish-package ARTICLE_ID
```

包位于 `output/ARTICLE_ID/`，包括：

```text
article.md
hero.EXT
metadata.json
sources.md
research-brief.md
fact-check.md
image-brief.md
image-license.json
publication-checklist.md
```

检查包：

```bash
find output/ARTICLE_ID -maxdepth 1 -type f -print
cat output/ARTICLE_ID/sources.md
python -m json.tool output/ARTICLE_ID/metadata.json
python -m json.tool output/ARTICLE_ID/image-license.json
cat output/ARTICLE_ID/publication-checklist.md
```

如果 `sources.md` 或 `metadata.json` 仍包含未验证来源，停止发布并修正数据。发布包目录已存在时命令不会覆盖；先保留旧包作为备份，再重新生成，不能直接丢失旧结果。

当前 Phase 1 CLI 没有来源编辑命令。若在导入后才发现 secondary 无效，必须暂停审核和批准，记录需要移除的 URL，并由维护者修正数据库后重新生成发布包；不要直接发布包含错误来源的包。这个限制应作为下一次工作流改造的输入。

## 11. 人工发布和登记

打开发布包：

```bash
open output/ARTICLE_ID
```

把 `article.md` 和 `hero.EXT` 人工上传到 WordPress、Ghost、微信公众号或其他平台。生成发布包不会自动上线，文章此时仍是 `APPROVED`。

只有获得真实公开 URL 后，才登记为已发布：

```bash
uv run newsroom publish ARTICLE_ID \
  --actor editor \
  --url "https://your-site.example/article-slug"
```

最后检查：

```bash
uv run newsroom published list
uv run newsroom stats
```

没有发布平台时保持 `APPROVED`，不要为了让列表出现记录而虚假执行 `publish`。

## 12. 测试和隔离 Demo

修改代码或工作流文档后运行：

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 使用 `demo-workspace/`，不会修改正式数据库。真实文章流程产生的数据库、研究稿、文章、图片和发布包属于本地工作材料，不应提交到 Git。

## 13. 常见问题

- `candidate show CANDIDATE_ID` 找不到记录：占位符没有替换为真实的 `cand-...`。
- `research/...-result.md` 不存在：Gemini 输出尚未保存到本地，CLI 不会自动创建模型结果。
- Gemini 看似没有输出 Markdown：它通常把 Markdown 渲染成富文本；要求放入 `markdown` 代码块并使用 Copy code。
- 只有 primary source：可以继续，但必须设置为 `PRIMARY_ONLY` 并明确归因。
- Fact Check 为 `FAIL`：先修稿并重新核验，不要登记失败报告或进入图片阶段。
- 图片文件名是 `.png` 但 `file` 显示 JPEG：先转换为真实 PNG 或把扩展名改为 `.jpg`。
- `published list` 为空：发布包尚未人工上传并通过 `newsroom publish` 登记，这是正常状态。
