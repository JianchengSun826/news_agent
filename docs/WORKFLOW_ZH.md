# 新闻编辑室中文操作手册

本手册说明如何从已审核的 CSV 开始，一次性处理整批新闻。Gemini 只负责新闻发现和生成 CSV；CSV 之后的研究、写作和图片搜索在第一个 Claude App 对话完成，事实核验在一个全新的 Claude 对话完成。程序不会调用付费模型 API，不会自动下载图片，也不会自动发布文章。

## 一、准备 CSV

先在 Google Sheet 中完成来源审核，再从 `Ready to Import` worksheet 下载 CSV。必须保留第一行列名，至少包含：

```text
candidate_title,core_fact,event_date,company,product,topic,subtopic,event_type,keywords,primary_source,secondary_sources,discovered_from,priority
```

注意：

- `primary_source` 必须真实存在并支持 `core_fact`。
- 只有一个可靠来源时，`secondary_sources` 留空即可。
- 多个关键词或 secondary URL 使用 `|` 分隔。
- 错误、伪造或无法访问的 secondary URL 不要导入。
- `event_date` 应填写事件发生日期，而非媒体转载日期。

## 二、初始化项目

```bash
cd /Users/jianchengsun/agent/news_agent
uv sync --locked --dev
uv run newsroom init
```

数据库位于 `data/newsroom.db`。再次运行 `init` 不会清空数据；旧版数据库升级前会备份到 `data/backups/`。

## 三、准备整批候选

```bash
uv run newsroom batch prepare \
  ~/Downloads/Tech\ Newsroom\ Candidates\ -\ read-to-import.csv
```

命令会：

1. 先验证整份 CSV；
2. 复用以前导入过的候选；
3. 对每条候选执行确定性去重；
4. 自动选择 `NEW`；
5. 为可处理候选创建文章记录；
6. 生成一个供 Claude 使用的 content request JSON。

保存输出中的两个值：

```text
batch_id
content_request_path
```

随时查看状态：

```bash
uv run newsroom batch status BATCH_ID
```

`REUSED_COMPLETE` 表示已有文章，不会重复写作；`READY_FOR_CONTENT` 表示已进入本批 Claude 请求；`WAITING_DUPLICATE` 表示需要人工判断。

## 四、处理重复项

没有 `WAITING_DUPLICATE` 时可跳过本节。

排除重复或不值得继续的候选：

```bash
uv run newsroom batch resolve BATCH_ID CANDIDATE_ID \
  --decision exclude \
  --actor editor \
  --reason "与已有事件重复"
```

`FOLLOW_UP` 或 `RELATED` 可以直接纳入：

```bash
uv run newsroom batch resolve BATCH_ID CANDIDATE_ID \
  --decision include \
  --actor editor \
  --reason "属于有独立新闻价值的后续事件"
```

`POSSIBLE_DUPLICATE` 或 `DUPLICATE` 必须先记录覆盖理由：

```bash
uv run newsroom duplicate-check CANDIDATE_ID \
  --override NEW \
  --actor editor \
  --reason "核对来源和日期后确认是独立事件"

uv run newsroom batch resolve BATCH_ID CANDIDATE_ID \
  --decision include \
  --actor editor \
  --reason "已完成人工重复项覆盖"
```

处理后重新运行 `batch status`，使用最新的 `content_request_path`。

## 五、第一个 Claude 对话：研究、写作和图片搜索

新建一个 Claude App 对话，上传 `content_request_path` 指向的 JSON 文件，只发送下面一句：

```text
严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。
```

Claude 应返回一个 JSON 文件，其中包含整批文章的：

- Research Brief；
- 完整中文 Markdown 文章；
- 中文标题和英文 slug；
- 每篇最多三个具有明确权利依据的图片候选。

下载结果后一次性导入：

```bash
uv run newsroom batch content-import BATCH_ID \
  ~/Downloads/BATCH_ID-content-result.json
```

CLI 会验证 request ID、文章数量、字段、slug 和图片权利信息。任何一条不合格时，整批不会写入。成功后会返回 `fact_check_request_path`。

## 六、第二个 Claude 对话：独立事实核验

必须新建 Claude 对话，不要继续使用写作窗口。上传 `fact_check_request_path`，仍然只发送同一句固定指令：

```text
严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。
```

下载后导入：

```bash
uv run newsroom batch fact-check-import BATCH_ID \
  ~/Downloads/BATCH_ID-fact-check-result-v1.json
```

结果含义：

- `PASS`：文章进入 `FACT_CHECKED`；
- `FAIL`：文章进入 `NEEDS_REVISION`；
- 一篇失败不会阻止同批其他文章继续。

## 七、修订失败文章

只有出现 `NEEDS_REVISION` 时才执行：

```bash
uv run newsroom batch revision-request BATCH_ID
```

把返回的 revision request 上传到 Claude，发送固定指令，下载结果后运行：

```bash
uv run newsroom batch revision-import BATCH_ID \
  ~/Downloads/BATCH_ID-revision-result-v2.json
```

CLI 会保留旧版本，写入新的 `research-brief-v2.md` 和 `article-v2.md`，然后生成新的 fact-check request。再次新建 Claude 对话完成核验。不要把第一轮 fact-check JSON 用于第二轮。

## 八、人工选择并登记图片

查看某篇文章的图片候选：

```bash
uv run newsroom image candidates ARTICLE_ID
```

对准备使用的候选，人工完成：

1. 打开 `landing_page_url`；
2. 打开 `rights_url`；
3. 核对作者、许可证和署名要求；
4. 下载原始图片；
5. 检查图片内容、格式、尺寸和水印。

确认后登记：

```bash
uv run newsroom image register ARTICLE_ID \
  --candidate IMAGE_CANDIDATE_ID \
  --file ~/Downloads/hero.jpg \
  --rights-confirmed
```

`--rights-confirmed` 表示编辑已亲自检查原始权利页面。CLI 不会代替人工做版权判断。

## 九、最终审核和发布包

```bash
uv run newsroom review ARTICLE_ID \
  --actor editor \
  --reason "已检查文章、来源、核验报告、图片和权利页面"

uv run newsroom approve ARTICLE_ID \
  --actor editor \
  --reason "当前版本及发布素材已确认"

uv run newsroom publish-package ARTICLE_ID
```

发布包位于：

```text
output/ARTICLE_ID/
```

主要文件包括：

```text
article.md
research-brief.md
fact-check.md
hero.EXT
metadata.json
image-license.json
sources.md
publication-checklist.md
```

生成发布包不等于已经发布。人工上传到 CMS 并获得真实 URL 后，再登记：

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

## 十、常用排错命令

```bash
uv run newsroom batch status BATCH_ID
uv run newsroom candidate list
uv run newsroom candidate show CANDIDATE_ID
uv run newsroom image candidates ARTICLE_ID
```

- 找不到候选：确认使用真实的 `cand-...`，不要输入占位符 `CANDIDATE_ID`。
- JSON 导入失败：查看错误字段，修正 Claude 输出后重新导入；不要手动跳过校验。
- 找不到结果文件：Claude App 不会自动保存到项目目录，需要先下载文件。
- 图片无法登记：确认文章为 `FACT_CHECKED`、候选属于该文章，并提供 `--rights-confirmed`。
- 发布包已存在：保留旧包后再处理，不要直接覆盖审计材料。

## 十一、测试

代码或流程变更后运行：

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 只使用本地合成数据，不调用网络或模型服务，也不会修改正式数据库。
