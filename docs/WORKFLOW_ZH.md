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
uv sync --locked --dev
uv run newsroom init
uv run newsroom batch prepare ~/Downloads/tech-newsroom-candidates.csv
```

初始化只创建 `data/` 的数据库及协调文件，旧 schema 升级前会备份。批次准备按需创建 `workspace/`，不会创建旧的 `research/`、`articles/`、`images/` 或 `output/`。相同 CSV 返回原批次，候选按指纹复用，确定性 `NEW` 自动选择。`WAITING_DUPLICATE` 必须先由编辑 include/exclude；全部解决前没有 content request。歧义覆盖和命令见[主流程第 8 节](WORKFLOW.md#8-候选歧义与备用批次-cli)。这是选题输入，选择后的文章工作只能整批推进。

## 二、找到批次与全部操作

打开 `workspace/README.md`，进入 `workspace/batches/BATCH_ID/`，双击 `OPEN_BATCH.command` 打开批次目录、README 并定位当前待处理 request。每次以 README 的下一步与当前路径为准。README 和 `batch-state.json` 由 SQLite 生成，删除/过期后可以刷新，不要手改它们来推进状态。

`actions/` 包含全部八个启动器：

| 文件名 | 使用时机 |
| --- | --- |
| `01-打开下一个Claude任务.command` | 打开当前内容、核验或修订 request，查看固定指令及预期返回名。 |
| `02-导入Claude返回文件.command` | 下载结果后，选择当前 JSON 并确认整批导入。 |
| `03-打开整批审核材料.command` | 检查全部当前文章与 README，不改变状态。 |
| `04-登记整批图片.command` | 全部 PASS 后，每篇确认一个候选、权利与本地原图，完整计划一起登记。 |
| `05-完成整批审核.command` | 全部 IMAGE_READY 后确认整批审核。 |
| `06-批准整个批次.command` | 全部 REVIEWED 后确认批准整批当前版本。 |
| `07-生成全部发布包.command` | 全部 APPROVED 后生成并打开完整 publish 目录。 |
| `08-刷新批次视图.command` | 重建全部派生视图，不改变状态。 |

`workspace/Migrate Newsroom.command` 是根目录迁移启动器，先预检并打开报告、再明确确认、验证备份并执行。尚未生成启动器的旧仓库可用迁移 CLI。整个仓库可以搬迁，启动器向上寻找 `pyproject.toml`；不要单独移动操作文件。文章文件夹没有单篇操作启动器。

## 三、两个 Claude 窗口与 inbox

操作 01 打开 `claude/requests/content-request.json`。在第一个 Claude 新对话上传这一个整批文件，只发送：

```text
严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。
```

Claude 返回全部 Research Brief、Markdown 稿件和每篇最多三个图片候选。将 JSON 下载到 `claude/inbox/`，阅读 `将Claude返回的JSON放在这里.txt`，再用操作 02 导入。一个身份匹配文件直接等待确认；多个匹配文件让编辑选择；没有匹配文件则显示本地文件选择器。目录不会自动监听或导入。

JSON 的 `batch_id`、`request_id` 和 `output_file` 必须属于当前任务，schema 和文章集合必须完整。改本地文件名不能修复旧任务身份。错误文件、缺少/额外/重复文章或不完整权利信息均先拒绝，不部分写入。成功导入保存原始字节到 `claude/archive/`，写入各篇版本文件并生成下一任务；取消不导入。

内容导入后，操作 01 打开 `fact-check-request-v1.json`。必须用第二个全新 Claude 对话独立一次核验整批，再把返回 JSON 放 inbox，用操作 02 导入。每篇获得 PASS/FAIL 和 claim-level 报告。通过项保留，但任何 FAIL 都阻塞整批图片、审核和批准。

只有 FAIL 才修订：Finder 核验导入生成仅含失败文章的 `revision-request-vN.json`，操作 01 定位它，Claude 修订后操作 02 导入，保留历史版本，再交给全新窗口核验新的 request。CLI 核验导入后需显式运行 `batch revision-request BATCH_ID`；打开操作不会创建任务。没有 FAIL 时只需要一次独立批次核验。

## 四、图片与整批发布闸门

全部有效文章 PASS 后，操作 04 逐篇列出 `articles/ARTICLE_ID/image-candidates.json` 的候选。每篇选一张，打开原始 `landing_page_url` 与 `rights_url`，确认作者、许可、权利声明和署名；人工下载并检查原图画面、格式、分辨率，选择本地文件，明确确认权利。不要采用搜索缩略图、未知版权媒体图或仅 fair use 的图片。详见 [IMAGE_POLICY.md](IMAGE_POLICY.md)。

选择草稿保存在批次 `image-selections.json`，中途取消可继续；不会部分登记图片。完整计划要求每篇恰好一项、候选归属正确、文件可读、`rights_confirmed: true`，验证全部后才暂存文件并一次事务登记。原图位于所属批次 `articles/ARTICLE_ID/hero.EXT`。旧状态混合已登记与未登记时要恢复/修复，不能继续部分登记。

检查操作 03 的全部当前素材后，依次使用 05、06、07。审核要求全部 `IMAGE_READY`，批准要求全部 `REVIEWED`，发布包要求全部 `APPROVED`；任何一篇不满足就整批拒绝。`EXCLUDED` 与 `REUSED_COMPLETE` 不参与这些闸门，有非空 `owning_batch_id` 的复用文章仍由原批次拥有，本批次只提供链接。历史复用文章可能为 NULL，没有批次素材链接；应根据数据库记录的规范/旧路径查找，或先完成迁移与归属核对，不能假定它属于某个旧批次。

完整发布包位于 `publish/ARTICLE_ID/`，整批索引是 `publish/batch-manifest.json`。先暂存验证全部包再整体替换，失败保留旧包并恢复数据库。成功后批次为 `PACKAGED`，文章仍为 `APPROVED`。

人工上传 CMS 后，用一个 JSON 登记全部真实 HTTP(S) URL。八个 Finder 操作没有发布登记启动器，使用：

```bash
uv run newsroom batch publish-register BATCH_ID ~/Downloads/BATCH_ID-publication-result.json --actor editor
uv run newsroom published list
uv run newsroom batch refresh-workspace BATCH_ID
```

JSON 含 `schema_version: 1`、`batch_id` 和 `items`；每项为 `article_id`、`article_url`，集合恰好覆盖全部有效文章。缺失、额外或重复项均拒绝，全部 URL、时间和审计在一次事务中写入，最后所有文章与批次为 `PUBLISHED`。

选择后的单篇 `brief/prompt/research/article/fact-check/image` 内容命令、顶层 `review`、`approve`、`publish-package`、`publish` 均已移除；保留的单篇服务只是内部恢复/构建工具。改变内容和发布流程状态的备用 CLI 必须使用 `batch`；顶层只读 `published list` 与 `stats` 仍受支持。

## 五、迁移、备份与恢复

旧运行素材先在数据库和目录副本中演练，再在真实工作区操作。根迁移启动器只读预检所有引用、归属、冲突、符号链接和路径安全；报告保存到 `data/workspace-migration-preflight.json`。有阻塞不会继续；取消只留报告。v3 升级到 v4 后 NULL `owning_batch_id` 不会由 init 自动填写：预检只把具有恰好一条有效 `batch_items` 关系的文章投影给该批次，忽略 `REUSED_COMPLETE` 和 `EXCLUDED`；没有有效关系的文章继续进入旧单篇 archive，两条或更多关系会阻塞，即使来自同一批次，已有 owner 也必须只有一条匹配关系。归属投影（包括 NULL）固定写入计划和 manifest；预检后关系变化会使执行失效。执行在同一事务回填 owner、切换路径和更新 workspace version，成功 journal/receipt 绑定投影摘要。

若归属明确的同一旧单篇文章中，一个数据库绑定文件占用首选 archive 路径，其他未绑定旧文件碰巧投影到同一路径，首选文件保留规范名称，额外文件分别保存到文章的 `legacy-files/原始相对目录/`，并在文件名加入按原始 UTF-8 路径生成的稳定摘要；大小写或 Unicode 规范等价的源路径仍有不同目标名称。超长名称会按目标文件系统限制和 UTF-8 字节边界确定性截短并尽量保留扩展名；复制的父目录组件必须是有效 UTF-8 且分别符合目标限制，不会被静默改名。无法读取可用限制、摘要无法容纳或父目录组件超限时预检阻断。两个数据库记录即使引用完全相同的源、目标和字段也会阻塞；仅含未绑定文件的冲突、归属歧义、祖先文件、符号链接、路径逃逸或已有目标同样阻塞。仅同一已验证条目的源和目标完全相同时才允许原地已有目标，计划移走的其他源路径不能复用。归属推断会按 macOS 大小写规则收集全部文章和批次 ID；多个候选或文章/批次不一致会在分流前阻断。安全分流的所有源文件分别进入清单与备份，只有数据库绑定条目更新 SQLite 路径，不会覆盖、删除或按内容合并。原 CSV 缺失不阻塞，提供时必须与已记录 SHA-256 一致。

```bash
uv run newsroom workspace migrate --preflight
uv run newsroom workspace migrate --backup-plan --plan data/workspace-migration-preflight.json
uv run newsroom workspace migrate --execute --plan data/workspace-migration-preflight.json --backup data/backups/WORKSPACE_MIGRATION_BACKUP
```

确认后创建并重新打开验证 SQLite、素材归档及 manifest，校验所有哈希。迁移暂存规范文件并重建视图，事务切换相对路径与 `workspace_version=1`，整体提升并核对引用；没有推进文章状态。未归属文件和旧单篇资料复制为 `workspace/archive/unclaimed-files/` 和 `legacy-single-articles/` 下只读参考。

发布验证区分当前批次与旧 archive。具有 owner 的文章必须有恰好一条指向该 owner 的有效关系，同一批次的重复有效记录也会阻塞；`APPROVED`/`PUBLISHED` 仍执行当前 PASS、图片、审核素材和发布包检查。NULL owner 只在没有有效成员关系时成立；`REUSED_COMPLETE`/`EXCLUDED` 不转移归属。未归属旧包按原字节归档，不重新生成，也不要求补齐新版 fact-check、图片或 package 字段；已有文章、图片身份字段和可比较素材必须与数据库一致。缺少 `metadata.json.image_id` 或 `revision_number` 仍兼容旧格式，但存在时须分别精确匹配图片身份，以及整数类型和值都相同的 revision。旧路径、版本化 hero 名称及缺少可选成员可以保留。

完整旧 `research/`、`articles/`、`images/`、`output/` 树在验证成功后原子改名保留到 journal 绑定的 `data/workspace-migration-recovery/`，包括空目录和文件身份；程序不会自动 unlink 或递归删除。改变、新增或缺失 archive/recovery 文件会阻止普通访问；活动批次素材和发布包可继续按批次规则编辑或重建。备份与 retained 树均需人工保管。

`data/workspace-migration-journals/`、成功 receipt、验证回滚 proof 和持久 `workspace-migration-lock.json` 记录恢复证据。迁移用独占 `flock`，普通 CLI/Finder 操作持共享锁到对话和写入结束，帮助命令不创建锁。未完成、损坏或恢复失败的 journal/屏障会拒绝普通访问，不能删 flags 或反复执行来绕过。失败保留备份、暂存、隔离内容与诊断，验证恢复完成才允许重试；最终已提交后的收尾中断只继续恢复收尾。终态准入继续验证不可变 archive 与 retained recovery proof，同时允许活动包按正常闸门变化。详细阶段、终态准入和恢复树规则见[主流程第 9 节](WORKFLOW.md#9-旧工作区迁移与恢复)。

## 六、离线验收

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 每次重建独立 `demo-workspace/`，用两个合成候选、本地 SVG 和合成 Claude JSON 走完所有批次操作。两篇均到达 `PUBLISHED`，验证批次 README 与 manifest 存在且没有 `research/`、`articles/`、`images/` 或 `output/` 四个旧 artifact 根目录。它不打开原生窗口、不访问网络、不迁移真实工作区；示例新闻、权利及 URL 仅用于验证。原 Phase 1 手册保存在 `docs/archive/WORKFLOW_PHASE1_ORIGINAL.md`，不是受支持操作路径。
