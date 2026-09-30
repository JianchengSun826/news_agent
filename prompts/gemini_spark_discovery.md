---
version: 1
provider: gemini-spark
purpose: scheduled-discovery
---

# Gemini Spark scheduled discovery prompt

复制下面整个代码块到 Gemini Spark Task。提前使用运行 Spark 的同一 Google Account 创建私有 Sheet，名称使用 `Tech Newsroom Candidates`，worksheet 使用 `Inbox`，并连接 Google Workspace。

```text
创建一个名为 “Tech Newsroom Discovery” 的定时任务。

执行时间：每周二、周四、周六上午 7:00。
时区：America/New_York。

目标：
发现最近 72 小时内值得中文科技媒体报道的重要科技新闻，进行初步来源核验，然后把候选追加到指定 Google Sheet。

Google Workspace target:
Use the connected Google Sheets app directly.
Do not open Google Sheets through Chrome or browser automation.

Spreadsheet exact title:
Tech Newsroom Candidates

Worksheet:
Inbox

The spreadsheet must be private and owned by the same Google Account running this task. Locate it by exact title through Google Workspace. Do not use a pasted spreadsheet URL. Do not create another spreadsheet if it cannot be found.

Sheet title and header:
Sheet title must be Tech Newsroom Candidates and worksheet must be Inbox. The first row must contain these columns in exactly this order:
discovered_at,event_date,candidate_title,core_fact,company,product,topic,subtopic,event_type,keywords,primary_source,primary_source_status,secondary_sources,secondary_source_status,rejected_secondary_sources,source_status,source_review_reason,discovered_from,priority,duplicate_hint,candidate_add_command,review_status,notes

If the header is missing or differs, report the mismatch and do not append rows. Never rename columns automatically.

工具与权限规则：
- Use Gemini's built-in web search and source-reading capabilities.
- Do not use local Chrome auto browse.
- Do not ask the user to take control of Chrome.
- Use the connected Google Sheets app to read and append rows.
- Do not use browser automation to edit the spreadsheet.
- Preserve all existing dropdowns, data-validation rules, formatting, filters, frozen rows, formulas, and protected ranges.
- Only write allowed dropdown values. Do not create, remove, or modify validation rules.
- If Google Sheets cannot be accessed through the connected app, report the error instead of switching to Chrome.
- Do not request approval merely because a candidate has only a valid primary source; record it as PRIMARY_ONLY.

每次最多添加 8 条候选。没有足够重要且来源可靠的新事件时可以少于 8 条，不要为了数量降低标准。

关注领域：
- AI / large language models
- AI agents and AI coding
- semiconductors, GPU, CPU
- cloud computing and data centers
- developer tools
- robotics and autonomous vehicles
- cybersecurity
- Apple, Google, Microsoft, Meta, Amazon/AWS, OpenAI, Anthropic, NVIDIA, AMD, Intel, Qualcomm
- major technology funding, acquisitions, launches, research releases, security incidents, regulation, and industry changes

来源规则：
1. 每条候选必须有一个 primary source。优先使用官方博客、官方新闻稿、开发者文档、GitHub release、论文、官方研究页面、监管或 SEC 文件。
2. 必须实际打开 primary URL，确认页面存在并直接支持 core_fact。无法确认时不要写入候选。
3. 尝试寻找 independent secondary source，例如 Reuters、AP、Bloomberg、Financial Times、WSJ、Ars Technica、The Verge、TechCrunch 或其他可靠专业媒体。
4. 只保留实际打开且内容支持同一 core_fact 的 secondary URL。找不到可靠 secondary 时允许保留候选，按 primary-only 规则写入。不得猜测、补全或虚构 URL。
5. Reddit、Hacker News、X、论坛和用户评论只能用于发现线索，不能作为 primary 或独立核验来源。
6. 不得虚构 URL、引用、日期、数字或产品名称，不要复制新闻正文，只提取 metadata 和一句简短事实。

事件去重：
写入前检查 Inbox 最近 30 天的数据，综合比较 company、product、event_type、event_date、core_fact、primary_source 和 keywords，不能只比较标题。
- 同一现实事件已存在：不要追加。
- 重要后续进展：duplicate_hint = FOLLOW_UP。
- 有关联但不是同一事件：duplicate_hint = RELATED。
- 无法确定：duplicate_hint = POSSIBLE_DUPLICATE，并在 notes 说明匹配行和原因。
- 其他新事件：duplicate_hint = NEW。

字段要求：
- discovered_at：当前执行时间，ISO-8601。
- event_date：事件发生或正式宣布日期，YYYY-MM-DD；不要默认使用媒体转载日期。
- candidate_title：简洁的中文工作标题，不使用标题党或营销语言。
- core_fact：一句无营销语言的事实，只说明谁在什么时候做了什么。
- company：主要公司或机构。
- product：主要产品、项目或技术。
- topic：只选 AI、AI Agent、AI Coding、Semiconductors、Cloud、Data Centers、Developer Tools、Robotics、Autonomous Vehicles、Cybersecurity、Technology Business 之一。
- subtopic：更具体的分类。
- event_type：只选 launch、update、research、funding、acquisition、security-patch、security-incident、shipment、policy、earnings、partnership、other 之一。
- keywords：3–6 个关键词，使用 | 分隔，例如 openai|codex|code-review。
- primary_source：一个已打开且支持 core_fact 的完整 URL。
- primary_source_status：写 UNVERIFIED_BY_HUMAN，表示 Gemini 已打开页面但仍需人工复核。
- secondary_sources：零个、一个或多个独立来源 URL；多个使用 | 分隔，没有可靠来源时留空。
- secondary_source_status：有 secondary_sources 时写 UNVERIFIED_BY_HUMAN；没有时写 NOT_PROVIDED。
- rejected_secondary_sources：记录打不开、不支持 core_fact、与 primary 同机构或疑似错误的 URL；多个使用 | 分隔，没有则留空。
- source_status：有 secondary_sources 时写 SECONDARY_UNVERIFIED；没有时写 PRIMARY_ONLY。不要写 CORROBORATED，该值只能由人工审核后设置。
- source_review_reason：简短说明来源状态。primary-only 时说明未找到可靠独立来源；有 secondary 时说明仍需人工验证独立性和支持范围。
- discovered_from：固定写 gemini-spark-scheduled-discovery。
- priority：1–5。5 为重大行业事件或高影响发布；4 为明确值得报道；3 为需编辑判断；2 为低优先级；1 为观察线索。
- duplicate_hint：只写 NEW、FOLLOW_UP、RELATED 或 POSSIBLE_DUPLICATE。
- review_status：固定写 PENDING。
- notes：记录来源冲突、事实不确定性、可能重复、paywall 或需要人工核验的内容。

candidate_add_command：
生成一条单行命令。基础格式：
uv run newsroom candidate add --candidate-title "..." --core-fact "..." --event-date "YYYY-MM-DD" --company "..." --product "..." --topic "..." --subtopic "..." --event-type "..." --primary-source "..." --keyword "..." --discovered-from "gemini-spark-scheduled-discovery" --priority N

有可靠 secondary source 时增加一个或多个：
--secondary-source "URL"

多个 secondary source 或 keyword 要重复对应参数。没有可靠 secondary source 时必须完全省略 --secondary-source，不要传空字符串。

命令只能包含 newsroom candidate add 及其参数。不得包含分号、管道、重定向、反引号、$() 或其他 shell 操作符。正确转义字段中的双引号。命令只供人工审核，不要尝试在本地电脑执行。

写入规则：
- 只向 Inbox 末尾追加新行。
- 不修改、排序、删除或覆盖已有行。
- 不修改 review_status 不为 PENDING 的行。
- 有 secondary_sources 时，确认它与 primary_source 不属于同一机构；无法确认时把 URL 移到 rejected_secondary_sources，并按 primary-only 写入。
- 如果 Sheet 不可访问或无法安全写入，不要创建其他 Sheet；在任务结果中报告错误。

完成后报告：
- 本次发现数量
- 实际写入数量
- 跳过的重复数量
- POSSIBLE_DUPLICATE 数量
- PRIMARY_ONLY 数量
- 需要人工注意的来源或事实问题
```
