# 已完成批次只保留最终成果 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native execution, or superpowers:subagent-driven-development if the user selects delegation. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 操作 05 导出验证成功后删除该批次过程文件，只保留最终正文、图片、授权说明和完整 SQL 去重记录。

**Architecture:** 增加独立的 SQLite 批次保留状态，先将文件引用切换到已验证的交付目录，再根据持久清单删除过程文件。状态查询、刷新、重复导出和迁移验证识别已清理批次，不重新生成过程材料；清理中断可重试。

**Tech Stack:** Python 3.12+、Pydantic v2、Typer、SQLite、标准库文件操作、pytest、uv。

**Spec:** [2026-10-12-completed-batch-retention-design.md](../specs/2026-10-12-completed-batch-retention-design.md)

## Global Constraints

- 阶段：Phase 1.1，本地、人工操作。
- 不增加模型 API、联网操作、自动发布或后台清理任务。
- 不得删除候选、文章或审计行，也不得通过清理改变文章版本、核验结论、审批状态、无图决定或候选去重字段。
- 原始 CSV、JSON 和图片若位于下载目录或其他外部位置，不属于程序管理的批次副本，不删除。
- 项目源码、配置、提示词模板、全局工作区入口、SQLite 及其备份、迁移恢复树、其他批次、未归属历史资料不在本次清理范围内。
- 仅运行 status、refresh 或 init 不触发删除。
- 先写失败测试，再修改可执行行为；保留当前工作区所有已有改动。

## Review Focus

- 已完成文章被新 CSV 复用：去重分类不变，链接现有最终正文，无须读取旧请求。
- 清理时另一个 CLI/Finder 操作正在写入：独占锁失败，不能开始删除或切换引用。
- SQLite 引用已切换后删除中断：查询和迁移准入仍能访问最终素材，重试只继续清理。
- 清理目录新增文件、路径变成符号链接或最终成果被编辑：保留意外对象并报告，不能覆盖或扩大删除范围。
- 旧 schema v4 数据库或既有迁移回执：升级保留记录，迁移验证正确区分正常旧批次和已清理批次。

---

## File responsibilities

- `src/newsroom/retention.py`（新增）：保留记录模型、清理预检、数据库引用切换、持久删除清单和恢复。
- `src/newsroom/db.py`：schema v5、v4 读取验证、v4→v5 升级及安全快照。
- `src/newsroom/coordination.py`：可重入独占操作锁及共享/独占调用的约束。
- `src/newsroom/batch_release.py`、`cli.py`、`macos.py`：操作 05 接入清理、重试和完成/失败提示。
- `src/newsroom/workspace.py`、`batch.py`、`service.py`：已清理状态、最终链接及过程操作的明确错误。
- `src/newsroom/migration.py`：新 schema、最终引用和保留状态的验证，不重建已清理材料。
- `tests/test_batch_retention.py`（新增）：真实 SQLite 与批次的成功、去重、失败恢复和路径保护测试。
- `tests/test_retention_schema.py`、`tests/test_retention_coordination.py`（新增）：数据库升级和跨进程互斥。
- 现有导出、交付、迁移及 Finder 测试：更新被本次需求替换的旧保留预期，保留低层导出原子性测试。

### Task 1: 持久保留状态与数据库升级

**Files:** `src/newsroom/db.py`, 新增 `src/newsroom/retention.py`, `tests/test_retention_schema.py`, `docs/DATA_MODEL.md`。

**Interfaces:** `BatchRetention(room: Newsroom).record(batch_id: str) -> dict[str, Any] | None`；模型 `CleanupManifest` 存储仓库相对路径、文件 SHA-256、大小及目录集合，不保存外部绝对路径。

- [x] 写失败测试：初始化后能读取空保留表，v4 升级保留原有文章/候选/审计，备份包含升级前 v4 数据；损坏或未知 schema 被拒绝。

```python
def test_new_database_has_empty_retention_table(newsroom):
    assert newsroom.db.validate_existing_schema() == 5
    assert newsroom.db.all('SELECT * FROM batch_retention') == []
```

- [x] 执行 `uv run pytest tests/test_retention_schema.py -q`，确认失败来自当前没有 schema v5/保留表。
- [x] 增加表，并将版本验证按版本构建契约，不能用 v5 契约误验证 v4。保留旧数据库约束；v4 升级前也生成安全快照。

```sql
CREATE TABLE IF NOT EXISTS batch_retention (
  batch_id TEXT PRIMARY KEY REFERENCES batches(batch_id),
  state TEXT NOT NULL CHECK(state IN ('CLEANING','FINAL_ONLY')),
  delivery_directory TEXT NOT NULL,
  cleanup_manifest TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

- [x] `CleanupManifest` 使用 `BaseModel`、`ConfigDict(extra='forbid')`，拒绝绝对路径、`..`、清单外目录和非法 SHA-256；表的 `delivery_directory` 必须是 `workspace/completed/BATCH_ID`，其字段不采用表示单文件的 `_path` 后缀。
- [x] 执行新 schema 测试及现有数据库初始化/版本验证测试；更新 DATA_MODEL 的 v5 与已清理路径语义。提交时只选择本任务改动，避免混入未相关的已有修改。

### Task 2: 清理引擎与故障恢复

**Files:** 新增 `src/newsroom/retention.py`, `src/newsroom/delivery.py`, 新增 `tests/test_batch_retention.py`。

**Interfaces:** `BatchRetention.preflight(batch_id: str) -> CleanupManifest`；`finalize(batch_id: str, actor: str, reason: str) -> Path`；`validate(batch_id: str) -> Path`。`finalize` 在独占锁内调用；无记录时验证交付并事务写入 `CLEANING`，已有记录时恢复，不重新打包。

- [x] 写失败测试：调用清理引擎后批次目录消失，文章路径指向最终正文，核查/研究路径为空，请求路径清空但 SHA 与身份保留，图片引用指向最终原图。

```python
def test_finalization_removes_process_tree(ready_batch):
    from newsroom.retention import BatchRetention
    release = BatchReleaseWorkflow(ready_batch.room)
    release.review(ready_batch.batch_id, 'editor', 'checked')
    release.approve(ready_batch.batch_id, 'editor', 'checked')
    publish = release.build_packages(ready_batch.batch_id)
    articles = [ready_batch.room.get_article(a) for a in ready_batch.article_ids]
    CompletedDelivery(ready_batch.room).build(ready_batch.batch_id, articles, publish)
    target = BatchRetention(ready_batch.room).finalize(ready_batch.batch_id, 'editor', 'checked')
    assert target.is_dir()
    assert not BatchWorkspace(ready_batch.room).batch_root(ready_batch.batch_id).exists()
    article = ready_batch.room.get_article(ready_batch.article_ids[0])
    assert (ready_batch.room.root / article['draft_path']).is_file()
    assert article['research_brief_path'] is None
    assert article['fact_check_path'] is None
```

- [x] 执行 `uv run pytest tests/test_batch_retention.py -q`，确认缺少清理行为导致失败。
- [x] 实现预检：批次必须已导出，所有有效文章 PASS 且 APPROVED/PUBLISHED、无待处理请求；最终清单恰好覆盖有效文章和选图，最终字节与当前稿件/图片一致。拒绝清理目录内被其他批次引用的文件，以及没有交付的本批次拥有素材。检查清单内每个文件和祖先，无符号链接、不出仓库、不接受特殊文件。
- [x] 事务切换本批次文件引用，清空请求/返回/过程图片提示词路径，写入 `CLEANING` 和审计；保留候选、文章、事件字段、权利、请求 SHA 和身份。历史图片记录解除过程路径，已选图片指向交付。清单包括自定义过程文件，只删除本批次管理目录。
- [x] 按文件清单逐个核验并 `unlink()`，从最深目录向上 `rmdir()`；不要用无清单的 `rmtree()` 删除现场新增文件。已不存在的清单项可跳过，清理结束后记录 `FINAL_ONLY`。未完成删除时抛出带“已导出，清理未完成”的诊断，保留清理记录。
- [x] 注入删除失败和 `KeyboardInterrupt`，验证最终文件和数据库引用安全；重试完成。新增文件、修改文件、符号链接、交付篡改、事务失败、跨批次引用均有独立测试，不能无验证地继续删除。

```python
def test_cleanup_can_resume_after_delete_failure(ready_batch, monkeypatch):
    release = BatchReleaseWorkflow(ready_batch.room)
    original_unlink = Path.unlink
    root = BatchWorkspace(ready_batch.room).batch_root(ready_batch.batch_id)
    def fail_one_file(path, *args, **kwargs):
        if path == root / 'input/candidates.csv':
            raise OSError('disk error')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_one_file)
        with pytest.raises(WorkflowError, match='清理未完成'):
            release.export(ready_batch.batch_id, 'editor', 'checked')
    record = ready_batch.room.db.one('SELECT * FROM batch_retention WHERE batch_id=?', (ready_batch.batch_id,))
    assert record['state'] == 'CLEANING'
    assert release.export(ready_batch.batch_id, 'editor', 'retry').is_dir()
    assert not root.exists()
```

- [x] 将上述 export 集成恢复测试安排在 Task 3 接入后运行；本任务先使用 `finalize` 对相同删除故障执行单元验证。

### Task 3: 操作 05、互斥及完成状态

**Files:** `src/newsroom/coordination.py`, `batch_release.py`, `cli.py`, `macos.py`, `workspace.py`, `batch.py`, `service.py`, `tests/test_batch_retention.py`, `tests/test_retention_coordination.py`, 现有导出/交付测试。

**Interfaces:** `operation_lock(root, exclusive=True)` 支持同一调用上下文已有独占锁时复用；共享锁上下文不能升级。`BatchReleaseWorkflow.export(batch_id, actor, reason) -> Path` 继续返回最终交付路径，新增自动清理及恢复；CLI `_room` 接受独占参数，Finder export 在同一独占生命周期内确认并执行。

- [x] 写失败测试：export 删除过程目录；snapshot 下一步为 complete，refresh 不重建目录；二次 export 不重复审批；CLI/Finder 打开最终目录，取消不变；另一个进程持有共享锁时 export 被拒绝且文件/引用不变。

```python
def test_refresh_does_not_recreate_completed_process_files(ready_batch):
    release = BatchReleaseWorkflow(ready_batch.room)
    target = release.export(ready_batch.batch_id, 'editor', 'checked')
    workspace = BatchWorkspace(ready_batch.room)
    state = workspace.refresh(ready_batch.batch_id)
    assert state['next_action'] == 'complete'
    assert state['retention_state'] == 'FINAL_ONLY'
    assert not workspace.batch_root(ready_batch.batch_id).exists()
    assert target.is_dir()
```

- [x] 执行相应新测试验证失败；实现独占上下文标记，不新增另一个与迁移锁不协调的锁文件。独占锁期间仍检查迁移准入；CLI/Finder 避免先持共享锁再进入 export，内部直接调用 export 也取得独占锁。
- [x] export 若存在保留记录，先验证并恢复清理/返回最终目录；否则沿用完整素材预检和审批，交付成功后调用 finalize，更新首页。低层 build_packages 仍可用于尚未清理批次；已清理批次明确拒绝需要历史素材的操作。
- [x] snapshot 添加 `retention_state`、最终链接和 cleaning 提示，复用文章链接最终正文；refresh 对已清理批次仅更新全局首页，不 ensure_layout。首页去掉不存在的历史记录链接。单独的 ensure_layout/store_source_csv 也必须拒绝重新创建已清理过程目录。
- [x] BatchWorkflow.prepare 同 CSV 重入不重新保存 CSV；summary 的 request_path 空字符串返回 None。内容/核查/修订导入、请求生成和完整包重建明确报已清理；可选 URL 登记和 published list 保持可用。
- [x] 更新旧交付测试：最终清理预期取代保留核查/归档预期；未知自定义过程文件现在删除，符号链接预检拒绝且目标保留。交付原子替换测试直接使用 CompletedDelivery 或尚未清理的底层发布路径，不通过已关闭批次再次重建。
- [x] 执行 `uv run pytest tests/test_batch_retention.py tests/test_retention_coordination.py tests/test_batch_export.py tests/test_completed_delivery.py tests/test_macos.py -q`。

### Task 4: 去重、复用与迁移兼容

**Files:** `src/newsroom/migration.py`, `batch.py`, 新增保留测试、现有 `tests/test_migration.py` 和数据库测试。

**Interfaces:** `WorkspaceMigrator._validate_materials(room, terminal=False)` 支持 schema v5 及保留记录，迁移 preflight 保持最终文件原位；已清理批次不要求过程目录。`BatchRetention.validate(batch_id)` 验证最终成果和当前 SQL 引用关系，CLEANING 验证剩余过程对象属于持久清单。

- [x] 写失败测试：清理前后去重查询结果一致、相同 CSV 重入无过程文件、不同 CSV 对相同事件分类一致；终态检查和完整迁移验证通过，缺失图片/正文、伪造清理记录或外部路径仍被拒绝。

```python
def test_final_cleanup_preserves_deduplication(ready_batch):
    room = ready_batch.room
    candidate_ids = [row['candidate_id'] for row in room.db.all('SELECT candidate_id FROM candidates')]
    before = {candidate_id: room.check_duplicate(candidate_id) for candidate_id in candidate_ids}
    BatchReleaseWorkflow(room).export(ready_batch.batch_id, 'editor', 'checked')
    after = {candidate_id: room.check_duplicate(candidate_id) for candidate_id in candidate_ids}
    assert after == before
```

- [x] 测试额外比较 candidates 和 articles 的 event_id、事件日期、公司、产品、类型、core_fact、keywords、指纹及候选/文章身份；新 CSV 测试用原事件修改非去重标题/priority 形成新批次，验证 REUSED_COMPLETE 或既有去重规则，没有请求重建。
- [x] 执行失败测试；修改迁移读取契约和 `_path` 遍历，只允许保留状态匹配的文章/图片使用 completed 路径；保留表 directory 需单独验证为目录。已清理请求的空路径仅在对应保留记录存在时合法。
- [x] 跳过已清理批次的启动器/CSV/派生视图/历史包要求，用最终交付和 SQL 归属验证替代；验证克隆中的 refresh 不创建过程目录，不再从清理后的输入重建发布包。CLEANING 清单允许部分文件已经删除，但不允许路径逃逸或意外对象。
- [x] 调整迁移计划中已清理批次的预期根目录，确保执行迁移不会创建空过程树；保护原有备份和恢复树，旧迁移回执准入继续正常。
- [x] 执行新保留测试和 `uv run pytest tests/test_migration.py -q`，然后运行完整 `uv run pytest`。

### Task 5: 操作说明、隔离验收与真实测试批次

**Files:** `README.md`, `docs/WORKFLOW.md`, `docs/WORKFLOW_ZH.md`, `docs/ROADMAP.md`, `docs/PRODUCT_OVERVIEW_ZH.md`, `docs/DATA_MODEL.md`, `scripts/demo.sh`。

- [x] 更新文档说明完整清理范围、最终路径、失去过程修订材料、清理中断重试、旧批次显式执行 export 才清理；替换“只删除 inbox 重复文件”和“历史材料继续保留”的旧描述。保留迁移备份规则，区分批次过程清理和迁移恢复树。
- [x] demo 断言过程目录不存在、retention 状态 FINAL_ONLY、最终正文/图片存在，并在清理后继续可选 publish-register。执行 `bash scripts/demo.sh` 和 `git diff --check`。
- [x] 在测试全部通过后，对真实工作区升级 init，保存升级前安全快照；读取 `batch-da8521c036fe` 的 8 篇最终正文 SHA-256、SQL 去重字段和去重结果，确认未完成批次/跨批次引用。
- [x] 执行已授权命令：

```bash
uv run newsroom --root . batch export batch-da8521c036fe --actor editor --reason '按已确认规则保留最终正文和图片，清理批次过程文件'
```

- [x] 核对最终正文 SHA-256 与清理前一致、文章数 8、无图选择不变、去重结果和元数据一致；`batch status`、`batch refresh-workspace` 与迁移准入不报缺失过程文件，不创建原批次目录。若真实数据预检拒绝，保留现场并报告具体阻断，不能绕过保护规则。
- [x] 使用完成前验证技能检查实际证据。报告最终成果目录、清理是否完成、完整测试/demo 结果和材料修订限制；不提交真实数据库或生成文章。

## Execution handoff

计划与书面设计已自查：五项 Review Focus 分别由 Task 3/4 的复用、Task 3 的跨进程锁、Task 2/4 的中断恢复、Task 2 的路径/变更、Task 1/4 的升级和回执测试覆盖。

建议 Native：本次清理、数据库引用和迁移验证相互依赖较多，由当前会话逐项实现可保留现有工作区上下文。实现前需要用户审阅计划并选择 Native 或 Subagent-driven；尚未修改程序、数据库或批次文件。

## 完成记录

2026-10-12：五项任务完成。独立审查的两个问题已通过一次修复解决；新增回归先失败后通过。完整测试 `uv run pytest`：689 passed；`bash scripts/demo.sh`、文档检查和 `git diff --check` 通过。真实批次清理 123 个文件（795067 字节），8 篇无图正文和隐藏回执 SHA-256 不变，SQL 业务元数据及 8 项去重分类一致。状态、刷新及再次导出通过，过程目录保持不存在；v4 升级安全快照只读验证通过。修改保留在现有工作区，未提交真实数据。
