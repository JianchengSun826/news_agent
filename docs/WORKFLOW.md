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

迁移属于 Phase 1.1 的本地人工操作。先在复制的数据库和素材目录中演练，再操作真实工作区。预检报告审核通过后，可显式备份并执行：

```bash
uv run newsroom workspace migrate --backup-plan --plan data/workspace-migration-preflight.json
uv run newsroom workspace migrate --execute --plan data/workspace-migration-preflight.json --backup data/backups/WORKSPACE_MIGRATION_BACKUP
```

`--backup-plan` 仅接受仍然有效且没有阻塞问题的清单，在 `data/backups/` 创建 SQLite 备份、素材归档、报告和 manifest，并重新打开数据库、检查归档成员安全性、提取并核对所有文件哈希后才返回。失败的备份会删除或标记 `INCOMPLETE`，不得用于迁移。`--execute` 要求备份与清单、当前数据库及全部素材完全一致；复制前和数据库事务提交前均重新校验来源。完整批次与 archive 在 `workspace/.migration-staging-*` 暂存，用临时数据库重建视图并校验，随后在单个事务中切换所有路径和 `workspace_version=1`，再原子替换目录。现有 canonical 素材保留原字节，缺失的 CSV 仅记录元数据。发布包验证使用一次性目录，不推进流程状态、不替换当前发布包。

只有最终验证成功后，才把 research/、articles/、images/、output/ 的完整旧目录原子改名移入 journal 绑定的 data/workspace-migration-recovery/；移动后再次核对文件哈希与目录清单。旧源文件永不自动 unlink 或递归删除，原 inode 与编辑器打开句柄写入的字节持续保留，等待人工清理。移动时发现修改或新增内容则恢复原目录并记录恢复诊断。journal 位于 `data/workspace-migration-journals/`，依次记录 `BACKED_UP`、`STAGED`、`DATABASE_UPDATED`、`PROMOTED`、`VERIFIED`、`CLEANED`。数据库提交后的失败或中断会从验证备份恢复原数据库和素材；恢复本身失败时保留原错误、恢复错误、暂存和可操作 journal。发现未完成 journal 或暂存时会拒绝再次迁移，须检查 journal 指定的验证备份并完成恢复后重试，不会声称已完成。成功 receipt 位于 `data/workspace-migration-receipt.json`，同时绑定完整旧目录与 retained 恢复树。再次检查验证历史 receipt、manifest、终态标记及当前数据库身份、路径和 schema 安全，才返回 `ALREADY_MIGRATED`。迁移时的内容哈希只继续约束只读 archive，不冻结活动批次的文章、Claude 内容、图片选择草稿或重建发布包。当前 request/result 仍按 SQLite 中的身份与 result 哈希验证；发布包构建和登记继续由原有批次闸门验证。README、batch-state.json 和图片候选视图可缺失或过时，普通访问不依赖其内容，`batch refresh-workspace` 可直接重建。journal 与 receipt 的本地引用只在当前根目录下解析；记录原根目录以安全重定位旧绝对路径，旧版记录从固定 journal 布局确定原根目录。完整搬迁仓库后仍可运行 CLI 和 Finder 控制器，父目录穿越、外部引用与符号链接均拒绝。

数据库提交前创建持久访问屏障 `data/workspace-migration-lock.json`，绑定本次 journal、manifest 与唯一 owner；提交结果尚不确定时也按可能已提交处理并验证恢复。屏障持续到目录替换、验证及清理完成，或 journal 已记录完整回滚；只有持有相同 owner 与原文件身份的执行者可删除屏障。普通 CLI 和批次 Finder 操作发现屏障后，在访问 Newsroom 路径前返回退出码 2 与恢复指引；迁移预检、备份和迁移控制器保留检查入口，执行仍拒绝未恢复的屏障。即使屏障缺失，普通访问仍只读扫描全部迁移 journal；不可读、损坏、恢复失败或收尾未完成的记录均拒绝访问。仅具有独立持久回滚 receipt、明确 `ROLLED_BACK` 终态/历史及已释放标记，且备份绑定、完整性、恢复后的数据库/schema/相对路径与原素材完整清单均再次验证通过的 `ROLLED_BACK`，或具有已释放/已提交标记且历史 receipt、备份与当前数据库身份、规范路径/schema 安全检查匹配的 `CLEANED` 允许访问。可重建视图及活动素材的迁移时哈希不属于终态准入条件。journal 阶段和历史先在副本中持久写入，成功后才更新内存；`CLEANED` 写入中断时检查实际持久记录，以决定提交收尾或回滚。崩溃、恢复失败或屏障 owner 改变时保留屏障和诊断。原目录改名为 retained 后、整体移出规范工作区前后都会复查文件哈希和目录清单；新增文件、目录或修改会中止清理。成功时 retained 暂存整体原子移入 journal 绑定的 `data/workspace-migration-recovery/`，路径同时写入 journal 和 receipt，永不自动递归删除。全部改名完成后，对 retained 与完整旧目录重复读取最终清单，记录文件类型、相对身份、权限、大小、SHA-256 与空目录，并与验证备份的原素材清单再次匹配；清单先持久写入 journal，再以其摘要绑定 receipt。每次终态准入都检查该恢复快照，缺失、修改、新增文件/目录、符号链接或非普通文件均阻止普通访问，且不删除恢复数据。活动规范素材仍按当前流程验证，不套用恢复快照的历史哈希。回滚优先整体移回 retained 原目录，保留人工新增和修改；仅缺失素材从验证归档补回。完整回滚 proof 位于对应 recovery 子目录的 rollback-receipt.json，绑定原数据库指纹、原目录/文件清单、备份 manifest、journal 与阶段历史；恢复数据不能与原快照完全验证时继续要求人工恢复，不能仅修改终态 flags 绕过。通过验证后可检查原状态并重试迁移；成功后新的 CLEANED journal/receipt 明确绑定先前回滚 proof 与相同原数据库备份，终态准入验证该继承关系，允许规范路径迁移及后续活动素材编辑。缺少新增 proof 的旧终态记录仍须恢复检查，不推断安全。无法验证原快照时保留素材、暂存和屏障，标记需要人工恢复，不记录成功清理。

`data/workspace-operation.lock` 使用本地 `flock` 协调完整操作生命周期。普通 CLI 首次使用 Newsroom 时才取得共享锁，并持有至命令上下文结束；包括嵌套命令在内的 `--help` 不创建目录或锁文件。批次 Finder 操作也持有共享锁直到命令、确认对话框和写入全部结束；迁移执行在任何暂存或屏障创建前取得非阻塞独占锁，并持有至最终成功或回滚。若操作仍在等待确认，迁移返回退出码 2，要求先完成或取消操作再重试；迁移期间新操作同样拒绝。进程退出会释放操作锁，持久 JSON 屏障及未解决 journal 继续保护崩溃后的工作区。回滚始终整体原子移走已替换的规范子目录至 journal 指定的 `data/workspace-migration-recovery/`，保留全部内容，再恢复原目录与数据库；移动前后重新核对文件哈希和目录清单，发现编辑、新增文件、符号链接、空目录或无法读取时标记 `RECOVERY_REQUIRED` 并保留屏障。隔离内容及归档恢复暂存永不自动删除。验证 receipt 与 `CLEANED` journal 持久写入后即为最终提交点；此后屏障删除或收尾中断不会回滚数据库和目录。独占锁内再次核验 receipt、目录和屏障，确认屏障已删除时返回成功并持久记录访问已释放；屏障仍在或无法验证时报告迁移已提交但收尾需要恢复，并继续阻止普通访问，即使重新创建屏障失败也由 journal 拒绝访问。

刷新批次工作区会在 `workspace/README.md` 旁安装可执行、可搬迁的 `workspace/Migrate Newsroom.command`。Finder 启动器仅分派隐藏 CLI 控制器；控制器先预检、保存并打开报告，存在阻塞时拒绝继续，再要求编辑明确确认，之后才创建验证备份并执行迁移。取消只保留报告，不移动文件、不更改数据库。

## 9. 验证与离线 Demo

```bash
uv run pytest -q
bash scripts/demo.sh
```

Demo 在 `demo-workspace/` 内生成合成 Claude JSON，完整走通批次准备、内容导入、独立核验、图片登记、批准、发布包和发布登记，不访问网络或模型服务。
