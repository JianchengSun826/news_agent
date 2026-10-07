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

打开 `workspace/README.md`，找到批次的相对链接，进入 `workspace/batches/BATCH_ID/` 并双击 `OPEN_BATCH.command`。它打开目录与 README，并在存在任务时定位当前 Claude request。README 列出下一步、条目状态、版本、核验结果、图片、审批、发布包与历史交接。

所有批次操作文件均位于 `actions/`；文章目录没有单篇操作脚本：

| 文件名 | 人工操作与效果 |
| --- | --- |
| `01-打开下一个Claude任务.command` | 定位当前待处理 content、fact-check 或 revision request，提示固定指令与预期返回文件。打开本身不创建任务或改变状态。 |
| `02-导入Claude返回文件.command` | 从 inbox 或文件选择器选择当前结果，验证整份文档，明确确认后整批导入并归档。 |
| `03-打开整批审核材料.command` | 打开全部文章目录和批次 README，不改变状态。 |
| `04-登记整批图片.command` | 全部 PASS 后逐篇确认候选、权利页及本地原图，完整计划一次登记。 |
| `05-完成整批审核.command` | 全部 IMAGE_READY 后确认整批检查，统一记录审核理由与状态。 |
| `06-批准整个批次.command` | 全部 REVIEWED 后确认整个批次的当前版本和素材。 |
| `07-生成全部发布包.command` | 全部 APPROVED 后确认、暂存验证并生成整批发布包，打开 publish 目录。 |
| `08-刷新批次视图.command` | 从 SQLite 重建 README、状态和图片候选视图，不推进流程。 |

工作区根目录另有 `workspace/Migrate Newsroom.command`，用于第 9 节的预检、报告、明确确认及验证备份/迁移。启动器通过向上查找 `pyproject.toml` 定位仓库，支持整个仓库搬迁；不要单独移动启动器到仓库之外。新批次准备/刷新会安装根启动器；尚未生成工作区的旧仓库可使用迁移 CLI。

批次素材集中在以下目录，当前 request/result 路径以 README 和 SQLite 为准：

```text
workspace/batches/BATCH_ID/
  README.md, OPEN_BATCH.command, batch-state.json
  actions/                         八个批次启动器
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

双击操作 01，新建 Claude App 对话，上传当前 `content-request.json`，只发送：

```text
严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。
```

Claude 返回一个可下载 JSON，包含整批 Research Brief、完整 Markdown 文章和每篇最多三个图片候选。候选必须包含原始落地页、原始权利页、作者、许可/权利声明及署名，不能使用缩略图、版权不明的媒体图片或仅 fair use 的图片。

把结果下载到当前批次 `claude/inbox/`，阅读其中 `将Claude返回的JSON放在这里.txt`，再双击操作 02。一个身份匹配的 JSON 会等待确认；多个匹配文件显示选择器；没有匹配文件显示本地文件选择器。匹配依据是 JSON 的 `batch_id`、当前 `request_id` 与声明的 `output_file`。按 request 的预期输出名下载；改本地文件名不会修复错误身份。

完整 schema、文章集合、slug 和权利元数据先验证，缺少、额外或重复文章与旧任务结果均拒绝，不部分写入。成功后原始字节归档到 `claude/archive/`，研究与稿件写入各文章目录，生成 fact-check request 并刷新视图。inbox 没有自动监听器，必须人工启动导入；取消不导入。

## 5. 第二个 Claude 窗口：一次独立整批核验

内容导入后双击操作 01，必须新建独立 Claude 对话，上传当前 `fact-check-request-v1.json`，发送同一句固定指令。一次交接核验全部文章，各篇返回 PASS/FAIL 和 claim-level 报告；无需逐篇开窗口。下载 JSON 放入 inbox，再用操作 02 导入。

PASS 文章为 `FACT_CHECKED`，FAIL 为 `NEEDS_REVISION`。已通过文章保留结果，但只要还有 FAIL，整个批次不能登记图片、审核、批准或打包。Finder 导入失败核验结果后会生成只包含失败文章的 revision request；操作 01 打开该任务。在 Claude 修订后用操作 02 导入，保留 v1 并新增 `research-brief-vN.md` 与 `article-vN.md`。新 request 再交给全新 Claude 窗口独立核验，旧 request 的结果不能用于新版本。

若采用 CLI 导入事实核验，编辑需显式运行 `batch revision-request` 创建修订任务；打开操作本身不会创建请求。没有 FAIL 时只需一个独立整批核验窗口，不增加额外检查流程。

## 6. 每篇一张图片，完整计划一次登记

全部有效文章 PASS 后，双击操作 04。逐篇从 `image-candidates.json` 选择一项，打开原始落地页和权利页，确认作者、许可及署名；人工下载原图并检查画面、格式和分辨率，然后选择本地文件并明确确认使用权利。

已确认选择保存在 `image-selections.json`。中途取消保留草稿，不部分登记；重启操作可继续未完成项。完整集合验证通过后才暂存复制每篇一张图、在一个事务内登记，文章统一进入 `IMAGE_READY`。缺少/重复/额外文章、跨文章候选、缺少原图或 `rights_confirmed` 不是显式 `true` 时整批拒绝。旧状态若混合已登记与未登记图片，需恢复或管理员修复，不能用此操作继续部分登记。图片规则见 [IMAGE_POLICY.md](IMAGE_POLICY.md)。

## 7. 整批审核、批准、发布包与登记

用操作 03 检查当前文章、研究简报、事实核验、图片和版权记录；用操作 05 完成整批审核，操作 06 批准整个批次，操作 07 生成全部发布包。审核要求全部有效文章 `IMAGE_READY`，批准要求全部 `REVIEWED`，发布包要求全部 `APPROVED`。先验证全部成员，再统一记录状态和相同 actor/reason 的审计；任何一篇不满足都不能部分通过。`EXCLUDED` 与 `REUSED_COMPLETE` 不阻塞，也不接收本批次包或 URL。

发布包先暂存并验证当前 article、Research Brief、fact-check、原图、metadata、来源、许可、checklist 和 manifest，再整体替换 `publish/`。失败/中断保留原发布目录并回滚；成功后批次 `PACKAGED`，文章仍 `APPROVED`。成功提交和刷新后，旧包备份清理失败不改变成功结果；残留 `.previous-publish-*` 仅为恢复数据。

人工把 `publish/ARTICLE_ID/` 上传 CMS。八个 Finder 操作没有发布登记启动器；取得真实 URL 后用下面一个 JSON 与一条批次命令登记全部结果：

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

图片计划 `PLAN_JSON` 为 `schema_version: 1`、`batch_id` 和完整 `items`，每项包含 `article_id`、所属 `image_candidate_id`、本地 `image_file` 路径及 `rights_confirmed: true`。Finder 自动准备的 `image-selections.json` 使用同一格式。

旧的 `brief create`、`prompt create`、`research register`、`article register`、`fact-check register`、`image register`、顶层 `review`、`approve`、`publish-package` 和 `publish` 不再注册为受支持命令。内部单篇服务仅用于恢复和批次构建，不能绕过整批闸门；历史手册仅留在 archive。

## 9. 旧工作区迁移与恢复

先把 `data/`、现有 `workspace/` 和全部旧素材复制到独立目录演练并核对恢复证据，再使用真实工作区的 `workspace/Migrate Newsroom.command`。旧 schema 仓库必须在副本和真实根目录分别先运行 `uv run newsroom --root "$NEWSROOM_ROOT" init`。`init` 初始化或升级 schema，并幂等写入配置的来源种子；它不移动文章素材。升级前必须停止所有 SQLite 写入者。若仍有 `newsroom.db-wal`/`newsroom.db-shm`，先正常关闭并 checkpoint，或用 SQLite backup API 创建一致副本；不能把单独复制主数据库当成完整状态。

`init` 在升级前生成的 `data/backups/newsroom-v旧版本-时间.db` 是 `copy2` 安全快照，并非迁移系统的验证备份。运行前记录原数据库的 schema、各表行数和必要业务抽样；运行后只读打开安全快照，检查 `PRAGMA integrity_check`，并逐项对比这些证据。完整性检查单独通过不证明逻辑等价。任何差异都必须停止；即使安全快照匹配，执行迁移前仍必须运行 `--backup-plan` 并独立验证其 SQLite、manifest 与素材归档。启动器会保存预检报告、要求明确确认，然后创建这份验证备份。取消只留下报告，不移动素材、不改变数据库。迁移不是发布，也不推进文章状态。

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

Demo 重建独立 `demo-workspace/`，准备两个候选，按当前 request 生成并导入合成 content 和独立 fact-check JSON，用一个完整图片计划，再依次整批 review、approve、publish-packages、publish-register。脚本断言两篇及批次均为 `PUBLISHED`、批次 README 和 publish manifest 存在，且没有顶层 `research/`、`articles/`、`images/` 或 `output/`。发布包位于 `demo-workspace/workspace/batches/BATCH_ID/publish/`。整个 demo 使用本地 SVG、合成权利记录和示例 URL，不访问网络或模型、不打开原生对话框、不迁移真实数据。
