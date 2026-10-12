# Phase 1.1 Finder-first 批处理操作流程

当前支持 macOS Finder 操作：候选选择后，内容、核验、图片登记、审核、批准及发布登记均以整批为单位。Gemini 只负责发现、整理 Google Sheet 和审核后的 CSV；CSV 导入后，Claude App 承担全部研究、写作、图片搜索和修订，一个全新 Claude 对话承担一次独立的整批事实核验。程序不调用模型 API、不联网下载图片、不自动发布。

`.command` 启动器仅支持 macOS；Terminal 可能短暂出现，选择文件、确认和取消通过原生对话框完成。初始化、CSV 导入、重复项歧义处理和人工发布后的 URL 登记保留明确 CLI 入口。SQLite 是状态来源，README 和 `batch-state.json` 是可重建视图。

## 1. 从 Gemini 获得 CSV

在 Google Sheet 的 `Inbox` 审核发现的新闻，确认 primary URL 支持 `core_fact`，移除错误 secondary URL，核对事件日期，把通过项标为 `READY_TO_IMPORT`。从保留表头的 `Ready to Import` worksheet 下载 CSV，至少包含：

```text
candidate_title,core_fact,event_date,company,product,topic,subtopic,event_type,keywords,primary_source,secondary_sources,discovered_from,priority
```

`keywords` 和多个 `secondary_sources` 用 `|` 分隔；只有 primary 可靠时可留空 secondary。全空或仅空白的行会忽略，部分填写但缺少必填字段的行会拒绝整批并报告行号。Gemini 不参与导入后的内容工作。

## 2. 初始化、导入和选题

```bash
cd /Users/jianchengsun/agent/news_agent
uv sync --locked --dev
uv run newsroom --root . init
uv run newsroom --root . batch prepare ~/Downloads/tech-newsroom-candidates.csv
```

`init` 初始化或升级 schema，并幂等写入配置的来源种子；它不移动文章素材。升级旧数据库前会把主数据库 `copy2` 到 `data/backups/` 作为升级安全快照，使用限制和验证步骤见第 9 节。全新根目录只创建 `data/` 与数据库/协调文件，不创建旧运行目录。`batch prepare` 验证整份 CSV、按指纹复用候选、自动选择确定性的 `NEW` 并创建文章及工作区。相同 CSV 字节返回原批次；以前执行过 `candidate import` 也可复用。结果包含 `batch_id`、`content_request_path`、`counts` 和 `items`。

`WAITING_DUPLICATE` 需要编辑 include 或 exclude，命令见第 8 节。全部歧义解决前不会生成 content request。候选级发现、来源审核、去重覆盖和选题仍是形成完整文章批次之前的输入步骤，之后只有整批操作。旧批次若已导入内容而仍有歧义，会在修改前拒绝继续 resolve，需要恢复。

## 3. 在 Finder 打开批次

打开 `workspace/README.md`，找到批次的相对链接，进入 `workspace/batches/BATCH_ID/` 并双击 `OPEN_BATCH.command`。它只打开目录与 README；在 README 的“当前 Claude 任务”中查看上传文件链接、任务 ID、预期返回文件名和对话要求。README 列出下一步、条目状态、版本、核验结果、图片、审批、发布包与历史交接。

所有批次操作文件均位于 `actions/`；文章目录没有单篇操作脚本：

| 文件名 | 人工操作与效果 |
| --- | --- |
| `02-导入Claude返回文件.command` | 从 inbox 或文件选择器选择当前结果，验证整份文档，明确确认后整批导入并归档。 |
| `03-打开整批审核材料.command` | 打开全部文章目录和批次 README，不改变状态。 |
| `04-登记整批图片.command` | 全部 PASS 后逐篇选择图片或明确无图；有图时确认候选、权利页及本地原图，完整计划一次登记。 |
| `05-确认并导出.command` | 一次确认整批审核与批准，生成正文和已登记图片，自动整理完成文章、清理本批次过程文件，刷新并打开交付目录。 |

工作区根目录另有 `workspace/Migrate Newsroom.command`，用于第 9 节的预检、报告、明确确认及验证备份/迁移。启动器通过向上查找 `pyproject.toml` 定位仓库，支持整个仓库搬迁；不要单独移动启动器到仓库之外。新批次准备/刷新会安装根启动器；尚未生成工作区的旧仓库可使用迁移 CLI。

批次素材集中在以下目录，当前 request/result 路径以 README 和 SQLite 为准：

```text
workspace/batches/BATCH_ID/
  README.md, OPEN_BATCH.command, batch-state.json
  actions/                         四个批次启动器
  input/candidates.csv             原始 CSV 副本
  input/source-metadata.json       原始文件名与 SHA-256
  claude/requests/                 content-request.json、fact-check-request-vN.json、revision-request-vN.json
  claude/inbox/                    将Claude返回的JSON放在这里.txt 和人工下载结果
  claude/archive/                  已导入原始结果、历史交接
  articles/ARTICLE_ID/             research-brief-vN.md、article-vN.md、fact-check-vN.md、image-candidates.json、hero.EXT
  image-selections.json            可恢复的图片选择草稿
  publish/ARTICLE_ID/              各篇完整发布包
  publish/batch-manifest.json      整批发布包索引
```

`REUSED_COMPLETE` 不复制为本批次待编辑文章。有非空 `owning_batch_id` 时链接到原拥有批次；历史文章可能仍为 NULL，没有可用的批次工作区链接，需按数据库记录的规范/旧路径查找素材，或先完成迁移与归属核对。复用不会推断或转移拥有批次。`EXCLUDED` 不参与后续闸门。历史单篇及未归属文件迁移为 `workspace/archive/` 下只读参考。

## 4. 第一个 Claude 窗口：整批研究、写作与图片搜索

手动新建 Claude App 对话，上传 README“当前 Claude 任务”链接的 `content-request.json`。文件的 `instructions` 包含完整执行任务和输出要求，无需重复发送长指令。如果应用要求文字输入或没有开始执行，发送“执行附件任务”即可。

Claude 返回一个可下载 JSON，包含整批 Research Brief、完整 Markdown 文章和每篇最多三个图片候选。已核实候选包含原始落地页、适用的权利页（可为全站媒体政策）、许可/权利声明和必要署名；作者按许可要求填写。另可返回最多三个待人工判断的推荐，包含来源、推荐理由和核验未通过原因。推荐不作为授权候选。不能使用缩略图或把 fair use 假设当作许可。

把结果下载到当前批次 `claude/inbox/`，阅读其中 `将Claude返回的JSON放在这里.txt`，再双击操作 02。一个身份匹配的 JSON 会等待确认；多个匹配文件显示选择器；没有匹配文件显示本地文件选择器。匹配依据是 JSON 的 `batch_id`、当前 `request_id` 与声明的 `output_file`。按 request 的预期输出名下载；改本地文件名不会修复错误身份。

完整 schema、文章集合、slug 和权利元数据先验证，缺少、额外或重复文章与旧任务结果均拒绝，不部分写入。成功后原始字节归档到 `claude/archive/`，研究与稿件写入各文章目录，生成 fact-check request 并刷新视图。inbox 没有自动监听器，必须人工启动导入；取消不导入。

## 5. 第二个 Claude 窗口：一次独立整批核验

内容导入后，从 README 的“当前 Claude 任务”找到当前 `fact-check-request-v1.json`，必须手动新建独立 Claude 对话并上传；无需重复长指令。一次交接核验全部文章，各篇返回 PASS/FAIL 和 claim-level 报告；无需逐篇开窗口。下载 JSON 放入 inbox，再用操作 02 导入。

PASS 文章为 `FACT_CHECKED`，FAIL 为 `NEEDS_REVISION`。已通过文章保留结果，但只要还有 FAIL，整个批次不能登记图片、审核、批准或打包。Finder 导入失败核验结果后会生成只包含失败文章的 revision request；从 README 的“当前 Claude 任务”找到该文件并手动上传。在 Claude 修订后用操作 02 导入，保留 v1 并新增 `research-brief-vN.md` 与 `article-vN.md`。新 request 再交给全新 Claude 窗口独立核验，旧 request 的结果不能用于新版本。

若采用 CLI 导入事实核验，编辑需显式运行 `batch revision-request` 创建修订任务；打开操作本身不会创建请求。没有 FAIL 时只需一个独立整批核验窗口，不增加额外检查流程。

## 6. 图片可选，完整计划一次登记

全部有效文章 PASS 后，双击操作 04。每篇可选择“无图发布（不使用图片）”并确认，或从 `image-candidates.json` 选择一项，打开原始落地页和权利页，确认作者、许可及署名；人工下载原图并检查画面、格式和分辨率，然后选择本地文件并明确确认使用权利。

已确认选择保存在 `image-selections.json`。中途取消保留草稿，不部分登记；重启操作可继续未完成项。完整集合验证通过后只复制有图文章的原图，在一个事务内登记全部图片和无图选择，文章统一进入 `IMAGE_READY`（素材选择完成）。缺失图片不自动等于无图；无图选择必须显式为 `true`，提供非空理由，绑定当前稿件版本并保留审计，不创建虚假图片记录。缺少/重复/额外文章、跨文章候选、缺少原图或 `rights_confirmed` 不是显式 `true` 时整批拒绝。旧状态若混合已完成与未完成素材选择，需恢复或管理员修复，不能用此操作继续部分登记。图片规则见 [IMAGE_POLICY.md](IMAGE_POLICY.md)。

内容导入后补充候选使用：

```bash
uv run newsroom --root . batch image-candidates-import BATCH_ID SUPPLEMENT_JSON --actor editor --reason "核对权利后补充候选"
```

补充文件格式：

```json
{
  "schema_version": 1,
  "batch_id": "BATCH_ID",
  "items": [{
    "article_id": "ARTICLE_ID",
    "image_candidates": [{
      "landing_page_url": "https://example.com/photo",
      "direct_image_url": null,
      "creator": "作者",
      "rights_basis": "PUBLIC_DOMAIN",
      "license_name": "CC0 1.0 Universal Public Domain Dedication",
      "rights_url": "https://example.com/photo",
      "rights_statement": "原始文件页明确标注 CC0 1.0",
      "attribution_required": false,
      "attribution_text": "作者，CC0 1.0",
      "relevance": "资料图，具体设备与拍摄日期"
    }]
  }]
}
```

每项提供 1–3 个完整候选对象，可只补充部分有效文章；重复文章、外部文章和非法权利元数据整份拒绝。已完成素材选择的文章不能补充。只追加候选，不改稿件、版本、核验或原始返回文件；相同候选不会重复插入。不要修改归档 content result 再导入，也不要手改候选派生视图。

## 7. 确认并导出（内容任务终点）与可选发布登记

用操作 03 检查当前文章、研究简报、事实核验、图片和版权记录；双击操作 05，一次确认整批审核和批准，随后生成发布包、自动整理完成文章、清理本批次过程文件并打开 `workspace/completed/BATCH_ID/`。获得新闻正文和已登记图片即完成本地内容任务；明确无图的文章只导出正文与辅助材料。审核要求全部有效文章 `IMAGE_READY`，批准要求全部 `REVIEWED`，发布包要求全部 `APPROVED`。无图文章不生成 `hero.*`、`image-license.json` 或 `image-brief.md`，metadata 的 `image_id` 为 null，checklist 明确记录无图；有图文章继续验证原图与权利记录。先验证全部成员，再统一记录状态和相同 actor/reason 的审计；任何一篇不满足都不能部分通过。`EXCLUDED` 与 `REUSED_COMPLETE` 不阻塞，也不接收本批次包或 URL。

发布包先暂存并验证当前 article、Research Brief、fact-check、原图、metadata、来源、许可、checklist 和 manifest，再整体替换 `publish/`。失败/中断保留原发布目录并回滚；成功后批次 `PACKAGED`，文章仍 `APPROVED`。 此时下一步为 `complete`，README 显示“导出完成”。成功提交和刷新后，旧包备份清理失败不改变成功结果；残留 `.previous-publish-*` 仅为恢复数据。

操作 05 在推进审核前验证全部素材和 PASS 结果；已审核或已批准的旧批次可继续导出。审核与批准分别保留审计记录。打包失败保留已有发布包及已完成的审核/批准，重试操作 05 从当前阶段继续，不重复记录已完成的审批。刷新会移除旧 01 与 05–08 启动器并安装 02–05，其他自定义文件保留。单独重建视图仍可使用 `batch refresh-workspace`。

备用 CLI（执行命令即确认审核与批准）：

```bash
uv run newsroom --root . batch export BATCH_ID --actor editor --reason "检查整批内容和图片权利，确认并导出"
```


### 完成后的整理与自动清理

操作 05 成功后，自动将交付内容集中到 `workspace/completed/BATCH_ID/`，按文章 slug 分目录：

```text
workspace/completed/BATCH_ID/
  article-slug/
    article.md                 最终新闻正文
    hero.EXT                   有图文章的原图
    image-license.json         有图文章的版权及署名记录
  .newsroom-export.json        隐藏的导出完整性记录
```

无图文章只含 `article.md`。最终成果完整导出并验证后，操作 05 自动删除 `workspace/batches/BATCH_ID/`：CSV 副本、Claude 请求和返回 JSON、inbox、研究简报、核查报告、全部修订版本、图片推荐和选择草稿、过程原图、旧发布包、批次 README/状态视图及启动器都不再保留。该目录内自定义过程文件也会清理。

SQLite 保留候选和文章事件字段、指纹、来源、核验结论、审批、图片权利、请求身份和 SHA-256、审计及可选发布 URL。去重不依赖已删除文件；当前正文与选图路径改为最终目录。首页“已完成”只链接最终成果，刷新不会重建过程目录。未完成批次、下载目录的外部原文件、项目源码/配置、数据库及其备份、迁移恢复树和其他批次不清理。

清理前确认最终内容与当前批准稿件一致，并记录持久删除清单。数据库引用切换后，保留状态先为 `CLEANING`，全部删除成功后为 `FINAL_ONLY`，与 `PACKAGED`/`PUBLISHED` 状态独立。导出失败不清理；删除失败会提示“已导出，清理未完成”，重试 `batch export BATCH_ID --actor editor --reason "继续完成清理"`，不重复审批。现场新增/修改文件、符号链接和跨批次引用会阻止继续删除。清理中可以查询最终路径，完成清理前不能迁移。

完成后可以手动编辑最终正文或添加发布笔记；状态查询、首页刷新和登记发布地址只校验路径安全及成果身份，不要求正文保持原始哈希。再次导出、继续删除或完整迁移验证仍严格核对原始清单，遇到修改会保留文件并停止。已清理批次重复导出只检查并返回已有最终目录，不重新打包或覆盖。人工编辑或无效清单会明确报错并保留原文件；不要通过移走最终目录来要求重建，因为研究和核查材料已删除。最终稿可供人工发布，但不能依靠旧过程材料重新修订或生成完整历史包。旧批次只有显式执行操作 05 / `batch export` 才应用新策略，`init`、状态查询和刷新不触发删除。

可选：人工把 `workspace/completed/BATCH_ID/文章slug/` 中正文和图片上传 CMS。四个 Finder 操作没有发布登记启动器；取得真实 URL 后用下面一个 JSON 与一条批次命令登记全部结果：

```json
{
  "schema_version": 1,
  "batch_id": "BATCH_ID",
  "items": [
    {"article_id": "ARTICLE_ID", "article_url": "https://news.example/article"}
  ]
}
```

`items` 必须恰好覆盖全部有效文章，每篇一个 HTTP(S) URL；占位例子需替换并补齐。缺失、额外或重复项整批拒绝。一次事务更新全部 URL、UTC 发布时间和审计，批次及文章到达 `PUBLISHED`：

```bash
uv run newsroom --root . batch publish-register BATCH_ID ~/Downloads/BATCH_ID-publication-result.json --actor editor
uv run newsroom --root . published list
uv run newsroom --root . batch refresh-workspace BATCH_ID
```

## 8. 候选歧义与备用批次 CLI

排除或包含 `FOLLOW_UP`/`RELATED`：

```bash
uv run newsroom --root . batch resolve BATCH_ID CANDIDATE_ID --decision exclude --actor editor --reason "与已选事件重复"
uv run newsroom --root . batch resolve BATCH_ID CANDIDATE_ID --decision include --actor editor --reason "有独立新闻价值的后续事件"
```

`POSSIBLE_DUPLICATE`/`DUPLICATE` 必须先记录人工覆盖再 include：

```bash
uv run newsroom --root . duplicate-check CANDIDATE_ID --override NEW --actor editor --reason "来源与日期证明为独立事件"
uv run newsroom --root . batch resolve BATCH_ID CANDIDATE_ID --decision include --actor editor --reason "已完成覆盖审核"
```

选择后改变内容和发布流程状态的备用入口均为整批操作；顶层只读 `published list` 与 `stats` 仍受支持。路径以当前 `batch status`、README/request 的输出名为准：

```bash
uv run newsroom --root . batch status BATCH_ID
uv run newsroom --root . batch content-import BATCH_ID RESULT_JSON
uv run newsroom --root . batch fact-check-import BATCH_ID RESULT_JSON
uv run newsroom --root . batch revision-request BATCH_ID
uv run newsroom --root . batch revision-import BATCH_ID RESULT_JSON
uv run newsroom --root . batch image-plan BATCH_ID PLAN_JSON
uv run newsroom --root . batch review BATCH_ID --actor editor --reason "检查整批素材和权利"
uv run newsroom --root . batch approve BATCH_ID --actor editor --reason "批准整批当前版本"
uv run newsroom --root . batch publish-packages BATCH_ID
uv run newsroom --root . batch publish-register BATCH_ID URLS_JSON --actor editor
uv run newsroom --root . batch refresh-workspace BATCH_ID
```

图片计划 `PLAN_JSON` 为 `schema_version: 1`、`batch_id` 和完整 `items`，每项包含 `article_id`、所属 `image_candidate_id`、本地 `image_file` 路径及 `rights_confirmed: true`。无图项使用 `{"article_id": "ARTICLE_ID", "no_image": true, "reason": "编辑明确选择无图发布"}`，不要同时提供候选或图片字段。有图与无图项可混合，仍须完整覆盖全部有效文章。Finder 自动准备的 `image-selections.json` 使用同一格式。

### 批次归档与缺失素材

已由操作 05 清理的批次仅保留最终成果和 SQL 记录，无需恢复过程目录。其他批次不要手动删除 `workspace/batches/` 下的材料：SQLite 仍引用这些素材，迁移准入会报告缺失路径并阻止整个工作区访问。先从废纸篓或验证备份恢复到原路径，不要删除数据库行、journal 或访问屏障，也不要创建空文件冒充素材。

整理旧批次使用可逆的索引归档，素材及数据库引用保留原位，文章状态和去重记录不变：

```bash
uv run newsroom --root . batch archive BATCH_ID --actor editor --reason "整理已完成批次"
uv run newsroom --root . batch unarchive BATCH_ID --actor editor --reason "恢复查看"
```

归档批次移到根 README 的“已归档批次”，仍能按 ID 查看和操作；归档不释放磁盘空间，也不能修复已删除文件。

### Finder 与导入反馈

当前任务文件位于 README 的“当前 Claude 任务”；也可在 `claude/requests/` 手动定位并上传。01 启动器已移除，打开批次不自动打开任务 JSON。02 成功后显示导入类型、篇数、批次状态和下一步，Terminal 同步打印；取消明确打印未导入，失败打印诊断。若看到“导入成功”后还有后续任务或视图错误，先查询 `batch status`，不要重复修改和导入原结果。

旧的 `brief create`、`prompt create`、`research register`、`article register`、`fact-check register`、`image register`、顶层 `review`、`approve`、`publish-package` 和 `publish` 不再注册为受支持命令。内部单篇服务仅用于恢复和批次构建，不能绕过整批闸门；历史手册仅留在 archive。

## 9. 旧工作区迁移与恢复

任何 rehearsal copy 或 production init 前，先关闭所有 newsroom Terminal/Finder 操作、外部素材编辑器和 SQLite 写入者。复制过程应取得 newsroom 独占 operation lock，检查 `PRAGMA journal_mode` 与 WAL/SHM 状态，用 SQLite backup API 创建连贯的 rehearsal 数据库，并在同一静默窗口复制 `data/`、现有 `workspace/` 和全部旧素材。目标路径本身也必须在 `resolve()` 前检查；符号链接目标即使指向空目录也会拒绝。目录复制必须使用 `symlinks=True` 保留符号链接身份，让预检拒绝它们，不能解引用。若选择 checkpoint/close/copy，必须先正常关闭写入者、完成 checkpoint，再把相关 SQLite 状态作为一个整体处理；不能先复制活动主数据库。

从代码仓库根目录执行连贯副本脚本；它在独占锁内先调用迁移准入检查，发现持久 barrier、损坏/未完成 journal 或 recovery-required 状态时，会在复制 SQLite 或素材前退出：

```bash
production_root="/path/to/newsroom-root"
rehearsal_root="$(mktemp -d /tmp/newsroom-migration-rehearsal.XXXXXX)"
uv run python scripts/create_rehearsal_copy.py "$production_root" "$rehearsal_root"
```

在完整副本演练并核对恢复证据后，再使用真实工作区的 `workspace/Migrate Newsroom.command`。普通 `newsroom init` 已是唯一 guarded 入口：它自行取得独占 operation lock，在同一锁内检查迁移屏障与 journal、只读校验 schema、处理 WAL、关闭并检查 sidecar、复验、创建升级快照、升级和写入来源种子。受支持的现有 v1-v5 数据库才允许 checkpoint；新库跳过真实数据库的读写打开。WAL checkpoint 必须非 busy 且完整；连接关闭后只允许 WAL 不存在或为零字节，并要求 SHM 不存在。任何失败都在备份、schema 或来源写入前退出；不要手工删除 sidecar，也不要在外层再套 operation lock。

```bash
set -euo pipefail
NEWSROOM_ROOT="/path/to/newsroom-root"
uv run newsroom --root "$NEWSROOM_ROOT" init
```

`Database.initialize` 在 service 锁之外被直接调用时也执行同一 schema/WAL/sidecar/复验序列；`Newsroom.init` 额外提供仓库独占锁和迁移准入。校验返回 0 时代表缺失或无用户表的新库，跳过 `mode=rw` 与 checkpoint。当前可执行程序支持的最高版本是 v5，并会在备份、建表、迁移或来源写入前拒绝未来版本、负版本、空或无效的版本表，以及含用户表却没有 `schema_version` 的未知数据库。对每个受支持版本，历史记录必须恰好为 `1..max(version)`；重复、缺号或从非 1 开始都会拒绝。标记为 v4 或 v5 的数据库还必须先通过对应版本完整兼容的 schema 语义校验：所需表、列类型/非空/default/主键（包括隐藏/生成列）、CHECK 约束、索引和外键必须齐全兼容；额外用户对象会失败，SQLite 内部对象不参与比较。v1-v4 升级时必须恰好生成一份使用唯一后缀命名且不会覆盖旧文件的 `data/backups/newsroom-v旧版本-时间-标识.db`；这份 `copy2` 是 checkpoint 后的完整安全快照，并非迁移系统的验证备份。只读打开它，检查 `PRAGMA integrity_check`，并与升级前记录的 schema、各表行数和必要业务抽样逐项比较。只有通过上述校验，v5 初始化才继续幂等来源核对，且不生成新的升级快照并让 schema 保持不变。其他版本立即停止。完整性检查单独通过不证明逻辑等价。任何差异都必须停止；无论源版本为何，执行迁移前仍必须运行 `--backup-plan` 并独立验证其 SQLite、manifest 与素材归档。启动器会保存预检报告、要求明确确认，然后创建这份验证备份。取消只留下报告，不移动素材、不改变数据库。迁移不是发布，也不推进文章状态。

完成 init 安全快照核对后，可运行 `uv run newsroom --root "$NEWSROOM_ROOT" workspace migrate --preflight`。此命令只读取数据库与文件，并把确定性的 JSON 清单保存到 `$NEWSROOM_ROOT/data/workspace-migration-preflight.json`；它不会更新流程状态、移动素材或创建迁移暂存目录。v3 升级到 v4 时新增的 `owning_batch_id` 默认仍为 NULL；预检会读取 `batch_items` 形成明确的归属投影。NULL 文章只有恰好一条非 `REUSED_COMPLETE`、非 `EXCLUDED` 关系时归入该批次，零条时保留为 `legacy-single-articles`，两条或更多时阻塞，即使这些关系来自同一批次。已有 owner 也必须与恰好一条有效关系一致；复用和排除关系永远不能取得或转移归属。完整投影（包括 NULL）写入计划与 manifest，当前及历史 request/result 也按投影 owner 分流。预检后任何 `batch_items` 或其他数据库变化都会令执行失效；执行只在同一数据库事务中回填 NULL owner、切换路径和写入 `workspace_version=1`，不会在预检阶段修改数据库。journal 和成功 receipt 绑定投影摘要，后续准入重新验证实际 owner。

缺失的数据库引用、路径逃逸、符号链接、归属歧义或目标冲突会阻塞迁移并返回非零状态。唯一自动拆分的冲突是：同一旧单篇文章归属明确、一个数据库绑定文件占用首选 archive 目标，而其他未绑定旧文件也投影到该目标。首选文件保留规范名称，额外文件分别进入该文章的 `legacy-files/原始相对目录/`，文件名附加由原始 UTF-8 相对路径生成的稳定摘要；大小写或 Unicode 规范等价的源路径仍会得到不同目标名称。名称超过目标文件系统限制时按 UTF-8 字节边界确定性截短并尽量保留扩展名；复制的原始父目录组件也必须是有效 UTF-8 并分别符合目标限制，程序不会静默改名。无法取得可用限制、连摘要都放不下或任一父目录组件超限时预检阻断。这会同时保留所有不同字节，并避免 macOS 路径碰撞。两个数据库记录即使引用完全相同的源、目标和字段也会阻塞；仅由未绑定文件组成的冲突、跨文章或不明归属、文件/祖先冲突、符号链接、路径逃逸及已有目标同样阻塞。仅同一已验证条目的源和目标完全相同时，已有目标才视为原地保留，其他计划移动走的源路径不能被复用。程序不会覆盖、删除或按内容去重。归属推断会收集路径中的全部文章和批次 ID，并按 macOS 大小写规则比较；出现多个候选或文章与批次候选不一致时，在归档分流前阻断。只有数据库绑定条目会更新 SQLite 路径，全部安全分流的条目都会进入清单和验证备份。可重复使用 `--source-csv PATH` 指定原始 CSV，只有字节级 SHA-256 与已记录批次一致时才会列为可复制输入；缺失原始 CSV 不会阻塞。

迁移属于 Phase 1.1 的本地人工操作。先在完整副本中演练，再操作真实工作区。下面的 CLI 流程启用 `set -euo pipefail`：预检失败或包含阻塞项时，不会创建迁移备份；备份创建、JSON 解析或独立验证失败时，不会执行迁移。`PLAN` 来自预检 JSON 的 `report_path`，`BACKUP` 来自 `--backup-plan` JSON 的 `backup_path`，不能手写猜测：

```bash
set -euo pipefail
NEWSROOM_ROOT="/path/to/old-repository"
PREFLIGHT_JSON="$(mktemp /tmp/newsroom-preflight.XXXXXX)"
BACKUP_JSON="$(mktemp /tmp/newsroom-backup.XXXXXX)"
trap 'rm -f "$PREFLIGHT_JSON" "$BACKUP_JSON"' EXIT

uv run newsroom --root "$NEWSROOM_ROOT" workspace migrate --preflight > "$PREFLIGHT_JSON"
PLAN="$(python3 - "$NEWSROOM_ROOT" "$PREFLIGHT_JSON" <<'PY'
from pathlib import Path
import json
import sys

root = Path(sys.argv[1]).resolve()
payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
plan = (root / payload["report_path"]).resolve()
expected = (root / "data/workspace-migration-preflight.json").resolve()
if plan != expected or not plan.is_file():
    raise SystemExit("preflight returned an unsafe or missing plan path")
print(plan)
PY
)"

uv run newsroom --root "$NEWSROOM_ROOT" workspace migrate --backup-plan --plan "$PLAN" > "$BACKUP_JSON"
BACKUP="$(python3 - "$NEWSROOM_ROOT" "$BACKUP_JSON" <<'PY'
from pathlib import Path
import json
import sys

root = Path(sys.argv[1]).resolve()
payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
backup = Path(payload["backup_path"]).resolve()
backup_root = (root / "data/backups").resolve()
if not backup.is_relative_to(backup_root) or not backup.is_dir():
    raise SystemExit("backup command returned an unsafe or missing directory")
print(backup)
PY
)"

uv run python - "$NEWSROOM_ROOT" "$BACKUP" <<'PY'
from pathlib import Path
import sys

from newsroom.migration import WorkspaceMigrator
from newsroom.service import Newsroom

WorkspaceMigrator(Newsroom(Path(sys.argv[1]))).verify_backup(Path(sys.argv[2]))
print(f"verified migration backup: {sys.argv[2]}")
PY

uv run newsroom --root "$NEWSROOM_ROOT" workspace migrate --execute --plan "$PLAN" --backup "$BACKUP"
```

`--backup-plan` 仅接受仍然有效且没有阻塞问题的清单，在 `data/backups/` 创建 SQLite 备份、素材归档、报告和 manifest，并重新打开数据库、检查归档成员安全性、提取并核对所有文件哈希后才返回。失败的备份会删除或标记 `INCOMPLETE`，不得用于迁移。`--execute` 要求备份与清单、当前数据库及全部素材完全一致；复制前和数据库事务提交前均重新校验来源。完整批次与 archive 在 `workspace/.migration-staging-*` 暂存，用临时数据库重建视图并校验，随后在单个事务中切换所有路径和 `workspace_version=1`，再原子替换目录。每个暂存批次始终重新生成根目录/批次 README、batch-state.json、图片候选视图、操作启动器和 inbox 指引；源视图缺失、过时或包含旧路径不阻塞有效迁移，原视图字节仍保存在验证备份及恢复树。现有 canonical 输入和素材保留原字节并严格验证，生成视图按当前渲染结果及暂存文件哈希验证，缺失的 CSV 仅记录元数据。发布包验证使用一次性目录，不推进流程状态、不替换当前发布包。

发布验证按归属分开处理。具有 `owning_batch_id` 的当前批次文章必须与恰好一条指向 owner 的有效 `batch_items` 记录一致；同一批次的重复有效记录也会阻塞。`APPROVED`/`PUBLISHED` 文章仍须满足当前 PASS、图片、审核素材与现有发布包检查。NULL owner 只有在没有有效成员关系时才是旧单篇 archive；任何有效关系都会投影 owner，不能借 NULL 绕过闸门。`REUSED_COMPLETE` 与 `EXCLUDED` 不取得归属，也不触发复用批次的包验证。未归属的历史发布包按原字节归档，不重新生成，也不要求旧记录补写当前 `fact_check_outcome`、图片或新版 package 字段；若旧包中存在文章/图片身份或可与数据库当前引用比较的素材，二者必须一致。旧 `metadata.json` 可省略 `image_id` 和 `revision_number`；一旦存在，前者须精确匹配文章图片身份，后者须以整数类型和值精确匹配当前 revision。旧路径字段、版本化 hero 文件名和缺少新版可选成员均可保留。

只有最终验证成功后，才把 research/、articles/、images/、output/ 的完整旧目录原子改名移入 journal 绑定的 data/workspace-migration-recovery/；移动后再次核对文件哈希与目录清单。旧源文件永不自动 unlink 或递归删除，原 inode 与编辑器打开句柄写入的字节持续保留，等待人工清理。移动时发现修改或新增内容则恢复原目录并记录恢复诊断。journal 位于 `data/workspace-migration-journals/`，依次记录 `BACKED_UP`、`STAGED`、`DATABASE_UPDATED`、`PROMOTED`、`VERIFIED`、`CLEANED`。数据库提交后的失败或中断会从验证备份恢复原数据库和素材；恢复本身失败时保留原错误、恢复错误、暂存和可操作 journal。发现未完成 journal 或暂存时会拒绝再次迁移，须检查 journal 指定的验证备份并完成恢复后重试，不会声称已完成。成功 receipt 位于 `data/workspace-migration-receipt.json`，同时绑定完整旧目录与 retained 恢复树。再次检查验证历史 receipt、manifest、终态标记及当前数据库身份、路径和 schema 安全，才返回 `ALREADY_MIGRATED`。迁移时的内容哈希只继续约束只读 archive，不冻结活动批次的文章、Claude 内容、图片选择草稿或重建发布包。当前 request/result 仍按 SQLite 中的身份与 result 哈希验证；发布包构建和登记继续由原有批次闸门验证。README、batch-state.json 和图片候选视图可缺失或过时，普通访问不依赖其内容，`batch refresh-workspace` 可直接重建。journal 与 receipt 的本地引用只在当前根目录下解析；记录原根目录以安全重定位旧绝对路径，旧版记录从固定 journal 布局确定原根目录。完整搬迁仓库后仍可运行 CLI 和 Finder 控制器，父目录穿越、外部引用与符号链接均拒绝。

若旧 `output/` 中只有版本控制的零字节 `output/.gitkeep`，迁移仍会把它作为旧树内容归档，因此 Git 可能显示该 placeholder 被删除。这是预期的代码清理：保留并提交删除，同时让顶层 `output/` 继续由 `.gitignore` 忽略；成功 receipt 后不要在运行目录手工重建 `.gitkeep`。

数据库提交前创建持久访问屏障 `data/workspace-migration-lock.json`，绑定本次 journal、manifest 与唯一 owner；提交结果尚不确定时也按可能已提交处理并验证恢复。屏障持续到目录替换、验证及清理完成，或 journal 已记录完整回滚；只有持有相同 owner 与原文件身份的执行者可删除屏障。普通 CLI 和批次 Finder 操作发现屏障后，在访问 Newsroom 路径前返回退出码 2 与恢复指引；迁移预检、备份和迁移控制器保留检查入口，执行仍拒绝未恢复的屏障。即使屏障缺失，普通访问仍只读扫描全部迁移 journal；不可读、损坏、恢复失败或收尾未完成的记录均拒绝访问。仅具有独立持久回滚 receipt、明确 `ROLLED_BACK` 终态/历史及已释放标记，且备份绑定、完整性、恢复后的数据库/schema/相对路径与原素材完整清单均再次验证通过的 `ROLLED_BACK`，或具有已释放/已提交标记且历史 receipt、备份与当前数据库身份、规范路径/schema 安全检查匹配的 `CLEANED` 允许访问。可重建视图及活动素材的迁移时哈希不属于终态准入条件。journal 阶段和历史先在副本中持久写入，成功后才更新内存；`CLEANED` 写入中断时检查实际持久记录，以决定提交收尾或回滚。崩溃、恢复失败或屏障 owner 改变时保留屏障和诊断。原目录改名为 retained 后、整体移出规范工作区前后都会复查文件哈希和目录清单；新增文件、目录或修改会中止清理。成功时 retained 暂存整体原子移入 journal 绑定的 `data/workspace-migration-recovery/`，路径同时写入 journal 和 receipt，永不自动递归删除。全部改名完成后，对 retained 与完整旧目录重复读取最终清单，记录文件类型、相对身份、权限、大小、SHA-256 与空目录，并与验证备份的原素材清单再次匹配；清单先持久写入 journal，再以其摘要绑定 receipt。每次终态准入都检查该恢复快照，缺失、修改、新增文件/目录、符号链接或非普通文件均阻止普通访问，且不删除恢复数据。活动规范素材仍按当前流程验证，不套用恢复快照的历史哈希。回滚优先整体移回 retained 原目录，保留人工新增和修改；仅缺失素材从验证归档补回。完整回滚 proof 位于对应 recovery 子目录的 rollback-receipt.json，绑定原数据库指纹、原目录/文件清单、备份 manifest、journal 与阶段历史；恢复数据不能与原快照完全验证时继续要求人工恢复，不能仅修改终态 flags 绕过。通过验证后可检查原状态并重试迁移；成功后新的 CLEANED journal/receipt 明确绑定先前回滚 proof 与相同原数据库备份，终态准入验证该继承关系，允许规范路径迁移及后续活动素材编辑。缺少新增 proof 的旧终态记录仍须恢复检查，不推断安全。无法验证原快照时保留素材、暂存和屏障，标记需要人工恢复，不记录成功清理。

`data/workspace-operation.lock` 使用本地 `flock` 协调完整操作生命周期。普通 CLI 首次使用 Newsroom 时才取得共享锁，并持有至命令上下文结束；包括嵌套命令在内的 `--help` 不创建目录或锁文件。批次 Finder 操作也持有共享锁直到命令、确认对话框和写入全部结束；迁移执行在任何暂存或屏障创建前取得非阻塞独占锁，并持有至最终成功或回滚。若操作仍在等待确认，迁移返回退出码 2，要求先完成或取消操作再重试；迁移期间新操作同样拒绝。进程退出会释放操作锁，持久 JSON 屏障及未解决 journal 继续保护崩溃后的工作区。回滚始终整体原子移走已替换的规范子目录至 journal 指定的 `data/workspace-migration-recovery/`，保留全部内容，再恢复原目录与数据库；移动前后重新核对文件哈希和目录清单，发现编辑、新增文件、符号链接、空目录或无法读取时标记 `RECOVERY_REQUIRED` 并保留屏障。隔离内容及归档恢复暂存永不自动删除。验证 receipt 与 `CLEANED` journal 持久写入后即为最终提交点；此后屏障删除或收尾中断不会回滚数据库和目录。独占锁内再次核验 receipt、目录和屏障，确认屏障已删除时返回成功并持久记录访问已释放；屏障仍在或无法验证时报告迁移已提交但收尾需要恢复，并继续阻止普通访问，即使重新创建屏障失败也由 journal 拒绝访问。

刷新批次工作区会在 `workspace/README.md` 旁安装可执行、可搬迁的 `workspace/Migrate Newsroom.command`。Finder 启动器仅分派隐藏 CLI 控制器；控制器先预检、保存并打开报告，存在阻塞时拒绝继续，再要求编辑明确确认，之后才创建验证备份并执行迁移。取消只保留报告，不移动文件、不更改数据库。

## 10. 验证与离线 Demo

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 重建独立 `demo-workspace/`，准备两个候选，按当前 request 生成并导入合成 content 和独立 fact-check JSON，用一个完整图片计划，再用 batch export 完成整批审核、批准和导出，随后验证可选 publish-register。脚本断言两篇及批次均为 `PUBLISHED`、批次 README 和 publish manifest 存在，且没有顶层 `research/`、`articles/`、`images/` 或 `output/`。发布包位于 `demo-workspace/workspace/batches/BATCH_ID/publish/`。整个 demo 使用本地 SVG、合成权利记录和示例 URL，不访问网络或模型、不打开原生对话框、不迁移真实数据。

### 人工判断推荐图片

操作 03 打开的批次 README 链接到各篇 `image-recommendations.md`，其中列出待判断图片的来源、相关性、权利线索和核验问题。已核实的图片继续位于 `image-candidates.json`；推荐不会自动出现在操作 04 中。

想手动加入推荐时，复制文章目录中的 `manual-image-candidates.json` 到你自己的文件，删除不采用项，填写 rights_basis、rights_url、rights_statement、attribution_required 及必要作者、许可名称和署名。初始 null 字段会阻止未完成的草稿被导入。使用第 6 节 `batch image-candidates-import` 命令，actor 标明操作者，reason 记录人工判断依据。成功后用操作 04 选择本地图片并确认权利。不采用推荐时，直接选择无图即可。已有人工草稿不会被刷新覆盖；推荐视图可从原始归档重建。
