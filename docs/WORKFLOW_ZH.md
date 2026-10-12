# 新闻编辑室中文操作手册：Finder 批次速查

Phase 1.1 仅支持 macOS Finder 启动器，可能短暂打开 Terminal；不需要在启动器内输入命令。Gemini 只负责发现、Sheet 来源审核和 CSV 导出；导入后全部内容交接使用 Claude App。第一个对话整批研究、写作、搜索图片；第二个全新对话一次独立核验整批、逐篇返回 PASS/FAIL。没有模型 API、网络抓取、自动图片下载或自动发布。

完整规则、恢复细节与备用 CLI 见 [WORKFLOW.md](WORKFLOW.md)。以下 `BATCH_ID`、`CANDIDATE_ID` 和文件路径是占位符，必须使用实际命令或批次 README 返回的值。

## 一、准备与导入

在 Sheet 确认 primary URL 支持 `core_fact`、事件日期正确、secondary 来源真实后，将条目标为 `READY_TO_IMPORT`。从 `Ready to Import` 导出保留表头的 CSV：

```text
candidate_title,core_fact,event_date,company,product,topic,subtopic,event_type,keywords,primary_source,secondary_sources,discovered_from,priority
```

多个 keywords 和 secondary URL 用 `|` 分隔；没有可靠 secondary 时留空。尾部全空行可忽略，部分数据缺必填字段拒绝整批。

```bash
cd /Users/jianchengsun/agent/news_agent
uv sync --locked
uv run newsroom --root . init
uv run newsroom --root . batch prepare ~/Downloads/tech-newsroom-candidates.csv
```

初始化会创建或升级 `data/` 中的数据库 schema，并幂等写入配置的来源种子；它不移动文章素材。旧 schema 升级前会生成主数据库的 `copy2` 安全快照，使用限制和核对步骤见第五节。批次准备按需创建 `workspace/`，不会创建旧的 `research/`、`articles/`、`images/` 或 `output/`。相同 CSV 返回原批次，候选按指纹复用，确定性 `NEW` 自动选择。`WAITING_DUPLICATE` 必须先由编辑 include/exclude；全部解决前没有 content request。歧义覆盖和命令见[主流程第 8 节](WORKFLOW.md#8-候选歧义与备用批次-cli)。这是选题输入，选择后的文章工作只能整批推进。

## 二、找到批次与全部操作

打开 `workspace/README.md`，进入 `workspace/batches/BATCH_ID/`，双击 `OPEN_BATCH.command` 打开批次目录、README 并定位当前待处理 request。每次以 README 的下一步与当前路径为准。README 和 `batch-state.json` 由 SQLite 生成，删除/过期后可以刷新，不要手改它们来推进状态。

`actions/` 包含四个启动器（保留 02–05 编号）：

| 文件名 | 使用时机 |
| --- | --- |
| `02-导入Claude返回文件.command` | 下载结果后，选择当前 JSON 并确认整批导入。 |
| `03-打开整批审核材料.command` | 检查全部当前文章与 README，不改变状态。 |
| `04-登记整批图片.command` | 全部 PASS 后，每篇选择图片或明确无图，有图时确认候选、权利与本地原图，完整计划一起登记。 |
| `05-确认并导出.command` | 一次确认整批审核与批准，生成正文和已登记图片，自动整理完成文章、清理本批次过程文件，刷新并打开交付目录。 |

`workspace/Migrate Newsroom.command` 是根目录迁移启动器，先预检并打开报告、再明确确认、验证备份并执行。尚未生成启动器的旧仓库可用迁移 CLI。整个仓库可以搬迁，启动器向上寻找 `pyproject.toml`；不要单独移动操作文件。文章文件夹没有单篇操作启动器。

## 三、两个 Claude 窗口与 inbox

在 README 的“当前 Claude 任务”找到 `claude/requests/content-request.json`，手动新建 Claude 对话并上传。文件已包含完整任务，无需重复长指令；如果应用要求文字输入或没有开始执行，发送“执行附件任务”。

Claude 返回全部 Research Brief、Markdown 稿件和每篇最多三个图片候选。将 JSON 下载到 `claude/inbox/`，阅读 `将Claude返回的JSON放在这里.txt`，再用操作 02 导入。一个身份匹配文件直接等待确认；多个匹配文件让编辑选择；没有匹配文件则显示本地文件选择器。目录不会自动监听或导入。

JSON 的 `batch_id`、`request_id` 和 `output_file` 必须属于当前任务，schema 和文章集合必须完整。改本地文件名不能修复旧任务身份。错误文件、缺少/额外/重复文章或不完整权利信息均先拒绝，不部分写入。成功导入保存原始字节到 `claude/archive/`，写入各篇版本文件并生成下一任务；取消不导入。

内容导入后，从 README 的“当前 Claude 任务”找到当前 `fact-check-request-v1.json` 并手动上传。必须用第二个全新 Claude 对话独立一次核验整批，再把返回 JSON 放 inbox，用操作 02 导入。每篇获得 PASS/FAIL 和 claim-level 报告。通过项保留，但任何 FAIL 都阻塞整批图片、审核和批准。

只有 FAIL 才修订：Finder 核验导入生成仅含失败文章的 `revision-request-vN.json`，从 README 的“当前 Claude 任务”找到它并手动上传，Claude 修订后操作 02 导入，保留历史版本，再交给全新窗口核验新的 request。CLI 核验导入后需显式运行 `batch revision-request BATCH_ID`；打开操作不会创建任务。没有 FAIL 时只需要一次独立批次核验。

## 四、图片与整批发布闸门

全部有效文章 PASS 后，操作 04 逐篇列出 `articles/ARTICLE_ID/image-candidates.json` 的候选。每篇可明确选择无图，或选一张图片，打开原始 `landing_page_url` 与 `rights_url`，确认作者、许可、权利声明和署名；人工下载并检查原图画面、格式、分辨率，选择本地文件，明确确认权利。不要采用搜索缩略图、未知版权媒体图或仅 fair use 的图片。详见 [IMAGE_POLICY.md](IMAGE_POLICY.md)。

选择草稿保存在批次 `image-selections.json`，中途取消可继续；不会部分登记图片。完整计划要求每篇恰好一项：有图项候选归属正确、文件可读、`rights_confirmed: true`；无图项 `no_image: true` 并提供非空 reason，验证全部后才暂存文件并一次事务登记。原图位于所属批次 `articles/ARTICLE_ID/hero.EXT`。旧状态混合已登记与未登记时要恢复/修复，不能继续部分登记。

检查操作 03 的全部当前素材后，双击 05，一次确认后完成审核、批准、导出和刷新。获得新闻正文和已登记图片即完成本地内容任务。审核要求全部 `IMAGE_READY`（已登记图片或已确认无图），批准要求全部 `REVIEWED`，发布包要求全部 `APPROVED`；任何一篇不满足就整批拒绝。`EXCLUDED` 与 `REUSED_COMPLETE` 不参与这些闸门，有非空 `owning_batch_id` 的复用文章仍由原批次拥有，本批次只提供链接。历史复用文章可能为 NULL，没有批次素材链接；应根据数据库记录的规范/旧路径查找，或先完成迁移与归属核对，不能假定它属于某个旧批次。

无图发布包不含 hero、image-license 或 image-brief 文件，checklist 明确记录无图。完整发布包位于 `publish/ARTICLE_ID/`，整批索引是 `publish/batch-manifest.json`。先暂存验证全部包再整体替换，失败保留旧包并恢复数据库。成功后批次为 `PACKAGED`，文章仍为 `APPROVED`。 此时下一步为 `complete`，README 显示“导出完成”。

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

可选：人工上传 CMS 后，用一个 JSON 登记全部真实 HTTP(S) URL；这不是本地内容任务完成的要求。四个 Finder 操作没有发布登记启动器，使用：

```bash
uv run newsroom --root . batch publish-register BATCH_ID ~/Downloads/BATCH_ID-publication-result.json --actor editor
uv run newsroom --root . published list
uv run newsroom --root . batch refresh-workspace BATCH_ID
```

JSON 含 `schema_version: 1`、`batch_id` 和 `items`；每项为 `article_id`、`article_url`，集合恰好覆盖全部有效文章。缺失、额外或重复项均拒绝，全部 URL、时间和审计在一次事务中写入，最后所有文章与批次为 `PUBLISHED`。

选择后的单篇 `brief/prompt/research/article/fact-check/image` 内容命令、顶层 `review`、`approve`、`publish-package`、`publish` 均已移除；保留的单篇服务只是内部恢复/构建工具。改变内容和发布流程状态的备用 CLI 必须使用 `batch`；顶层只读 `published list` 与 `stats` 仍受支持。

## 五、迁移、备份与恢复

任何 rehearsal copy 或 production init 前，先关闭所有 newsroom Terminal/Finder 操作、外部素材编辑器和 SQLite 写入者。复制过程取得 newsroom 独占 operation lock，检查 journal mode 与 WAL/SHM 状态，用 SQLite backup API 创建连贯数据库，再在同一静默窗口复制 `data/`、现有 `workspace/` 和全部旧素材。目录复制必须保留 symlink 本身，不能解引用；这样预检才能拒绝它。目标根路径本身也必须先用未解析路径检查，符号链接目标即使指向空目录也会拒绝。真实 production init 也必须保持静默，并直接运行[主流程第 9 节](WORKFLOW.md#9-旧工作区迁移与恢复)的普通 `newsroom init`，不要手工包裹锁或 checkpoint。该命令自行持有独占锁，先检查迁移准入与只读 schema snapshot，再让受支持的现有 v1-v5 数据库执行非 busy 的完整 `wal_checkpoint(TRUNCATE)`；新库跳过真实数据库的读写打开。连接关闭后只允许 WAL 不存在或为零字节，且 SHM 必须不存在，随后独立复验才允许备份、升级和来源写入。否则停止调查，不能手工删除 sidecar。

在代码仓库根目录运行 `uv run python scripts/create_rehearsal_copy.py "$production_root" "$rehearsal_root"` 创建连贯副本。脚本在独占锁内先检查迁移准入；持久 barrier、损坏/未完成 journal 或 recovery-required 状态都会在复制数据库和素材前停止。

`init` 前读取 `MAX(schema_version)`，只接受 1、2、3、4、5；每个受支持版本的历史记录必须恰好为 `1..max(version)`，重复、缺号或从非 1 开始都会拒绝。当前可执行程序最高支持 v5，并在备份、建表、升级或来源写入前拒绝未来版本、负版本、空或无效的版本表，以及含用户表却没有 `schema_version` 的未知数据库；只有不存在或没有用户表的空 SQLite 数据库可作为新库。标记为 v4 或 v5 的数据库必须先具备相应版本完整兼容的 schema，包括全部所需表、列约束（含隐藏/生成列）、CHECK 约束、索引和外键；额外用户对象也会阻塞，SQLite 内部对象会忽略。v1-v4 必须恰好生成一份带唯一后缀且绝不覆盖既有文件的 `copy2` 升级安全快照；运行前记录 schema、表行数和必要业务抽样，运行后只读打开快照并逐项比较。只有校验通过后，v5 init 才执行幂等来源核对，并让 schema 保持不变且不生成升级快照；其他版本立即停止。`PRAGMA integrity_check` 不能单独证明逻辑等价，且 init 安全快照不是 workspace migration 的验证备份。无论源版本为何，执行迁移前仍须创建并独立验证 `--backup-plan` 返回的 SQLite、manifest 和素材归档。完整、失败即停止的命令见[主流程第 9 节](WORKFLOW.md#9-旧工作区迁移与恢复)。根迁移启动器只读预检所有引用、归属、冲突、符号链接和路径安全；报告保存到 `data/workspace-migration-preflight.json`。有阻塞不会继续；取消只留报告。v3 升级到 v4 后 NULL `owning_batch_id` 不会由 init 自动填写：预检只把具有恰好一条有效 `batch_items` 关系的文章投影给该批次，忽略 `REUSED_COMPLETE` 和 `EXCLUDED`；没有有效关系的文章继续进入旧单篇 archive，两条或更多关系会阻塞，即使来自同一批次，已有 owner 也必须只有一条匹配关系。归属投影（包括 NULL）固定写入计划和 manifest；预检后关系变化会使执行失效。执行在同一事务回填 owner、切换路径和更新 workspace version，成功 journal/receipt 绑定投影摘要。

若归属明确的同一旧单篇文章中，一个数据库绑定文件占用首选 archive 路径，其他未绑定旧文件碰巧投影到同一路径，首选文件保留规范名称，额外文件分别保存到文章的 `legacy-files/原始相对目录/`，并在文件名加入按原始 UTF-8 路径生成的稳定摘要；大小写或 Unicode 规范等价的源路径仍有不同目标名称。超长名称会按目标文件系统限制和 UTF-8 字节边界确定性截短并尽量保留扩展名；复制的父目录组件必须是有效 UTF-8 且分别符合目标限制，不会被静默改名。无法读取可用限制、摘要无法容纳或父目录组件超限时预检阻断。两个数据库记录即使引用完全相同的源、目标和字段也会阻塞；仅含未绑定文件的冲突、归属歧义、祖先文件、符号链接、路径逃逸或已有目标同样阻塞。仅同一已验证条目的源和目标完全相同时才允许原地已有目标，计划移走的其他源路径不能复用。归属推断会按 macOS 大小写规则收集全部文章和批次 ID；多个候选或文章/批次不一致会在分流前阻断。安全分流的所有源文件分别进入清单与备份，只有数据库绑定条目更新 SQLite 路径，不会覆盖、删除或按内容合并。原 CSV 缺失不阻塞，提供时必须与已记录 SHA-256 一致。

```bash
NEWSROOM_ROOT="/path/to/old-repository"
uv run newsroom --root "$NEWSROOM_ROOT" init
# 核对升级安全快照后，严格执行主流程的 preflight → backup-plan → 独立验证 → execute 命令块。
```

确认后创建并重新打开验证 SQLite、素材归档及 manifest，校验所有哈希。迁移暂存规范文件并重建视图，事务切换相对路径与 `workspace_version=1`，整体提升并核对引用；没有推进文章状态。未归属文件和旧单篇资料复制为 `workspace/archive/unclaimed-files/` 和 `legacy-single-articles/` 下只读参考。

发布验证区分当前批次与旧 archive。具有 owner 的文章必须有恰好一条指向该 owner 的有效关系，同一批次的重复有效记录也会阻塞；`APPROVED`/`PUBLISHED` 仍执行当前 PASS、图片、审核素材和发布包检查。NULL owner 只在没有有效成员关系时成立；`REUSED_COMPLETE`/`EXCLUDED` 不转移归属。未归属旧包按原字节归档，不重新生成，也不要求补齐新版 fact-check、图片或 package 字段；已有文章、图片身份字段和可比较素材必须与数据库一致。缺少 `metadata.json.image_id` 或 `revision_number` 仍兼容旧格式，但存在时须分别精确匹配图片身份，以及整数类型和值都相同的 revision。旧路径、版本化 hero 名称及缺少可选成员可以保留。

完整旧 `research/`、`articles/`、`images/`、`output/` 树在验证成功后原子改名保留到 journal 绑定的 `data/workspace-migration-recovery/`，包括空目录和文件身份；程序不会自动 unlink 或递归删除。改变、新增或缺失 archive/recovery 文件会阻止普通访问；活动批次素材和发布包可继续按批次规则编辑或重建。备份与 retained 树均需人工保管。

迁移可能把版本控制中的旧零字节 `output/.gitkeep` 一并归档，使 Git 显示预期删除。代码改动应保留并提交这个删除，让整个顶层 `output/` 由 `.gitignore` 忽略；已有成功 receipt 后不要在运行目录手工重建 placeholder。

`data/workspace-migration-journals/`、成功 receipt、验证回滚 proof 和持久 `workspace-migration-lock.json` 记录恢复证据。迁移用独占 `flock`，普通 CLI/Finder 操作持共享锁到对话和写入结束，帮助命令不创建锁。未完成、损坏或恢复失败的 journal/屏障会拒绝普通访问，不能删 flags 或反复执行来绕过。失败保留备份、暂存、隔离内容与诊断，验证恢复完成才允许重试；最终已提交后的收尾中断只继续恢复收尾。终态准入继续验证不可变 archive 与 retained recovery proof，同时允许活动包按正常闸门变化。详细阶段、终态准入和恢复树规则见[主流程第 9 节](WORKFLOW.md#9-旧工作区迁移与恢复)。


## 六、补充候选、归档和操作反馈

补充候选使用 `batch image-candidates-import BATCH_ID SUPPLEMENT_JSON --actor editor --reason "核对后补充"`，不重新导入已完成的整批内容结果。支持的 JSON 格式见主流程第 6 节。

整理旧批次用 `batch archive BATCH_ID --actor editor --reason "整理旧批次"`；恢复索引可用 `batch unarchive`。这些命令保留原素材及去重记录，不释放空间。不要手动删除批次文件夹；文件丢失会使数据库引用失效，须从废纸篓或验证备份恢复。

01 启动器已移除；在批次 README 的“当前 Claude 任务”链接定位 JSON，手动打开 Claude 并上传。02 在成功、取消和失败时都有 Terminal 提示；成功显示篇数和下一步。Terminal 退出本身不代表导入成功，用 `batch status BATCH_ID` 核实。

图片核验补充（Phase 1.1）：内容返回可选 `image_recommendations`（每篇最多 3 条），独立于已核实 `image_candidates`。推荐包含来源 URL、可空直接链接/作者/权利线索、verification_status（PAGE_INACCESSIBLE / INSUFFICIENT_EVIDENCE / RESTRICTED_USE）、非空 verification_issue 和 relevance。原始结果归档为推荐事实来源；不新增 SQLite 表或改变 schema。工作区生成 image-recommendations.json/md 审核视图及一次性 manual-image-candidates.json 可编辑草稿。人工补全适用授权后通过现有候选补充命令加入，记录 actor/reason；04 才确认实际图片使用。适用全站媒体政策可作为 rights_url，不要求独立逐图许可页。详见 IMAGE_POLICY.md。
