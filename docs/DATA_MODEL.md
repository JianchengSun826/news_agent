# Data Model

SQLite schema version 2 由 `newsroom init` 幂等创建。再次运行 `init` 会把 version 1 数据库迁移到 version 2。

写入 SQLite 前，候选与图片 metadata 通过 Pydantic v2 模型校验日期、URL、必填文本、priority 范围和图片 provenance。SQLite 保留 canonical representation，Pydantic 模型不替代数据库约束与 audit trail。

## candidates

保存 `candidate_id`、status、discovered/event timestamps、title、core_fact、company/product/topic/subtopic、event_type、JSON keywords、primary/secondary source URLs、discovery channel、priority、duplicate_status、matching_event_id、event_id、selected、rejection reason、notes 和 audit timestamps。`core_fact` 是无营销语言的一句话事实。`primary_source` 必填；`secondary_sources` 是 JSON 数组，primary-only 候选使用空数组 `[]`。

Duplicate status 仅允许 `NEW`、`FOLLOW_UP`、`RELATED`、`POSSIBLE_DUPLICATE`、`DUPLICATE`。人工覆盖必须记录 actor 和 reason。

## articles

一条 selected candidate 对应至多一条 article。保存 specification 要求的 event/status/publish 字段、中文标题、slug、摘要、分类、来源、artifact 路径、image ID 和 timestamps。Research Brief 与最终稿始终为不同文件。

## sources

独立 registry 保存 source_id、name、domain、type、reliability、usage policy、notes 和 active。类型为 PRIMARY/WIRE/MAINSTREAM_MEDIA/TECH_MEDIA/RESEARCH/COMMUNITY/SOCIAL；可靠性为 HIGH/MEDIUM/LOW。

## images

保存 image type、项目内 `image_path`、source/creator/license/attribution 或 AI provider/model/prompt path、生成/下载时间和 notes。实际文件保存在 `images/ARTICLE_ID/hero.EXT`。AI 图片需要文件、provider、model 与 prompt；其他图片需要文件、URL、creator、license 与 license URL。

## state_transitions

Append-only audit log：entity type/id、from/to status、actor、reason、created_at。重要决定不得通过覆盖此表删除历史。

## Identifiers and interchange

业务记录使用随机前缀 ID，如 `cand-…`、`art-…`、`img-…`。`event_id` 为 `company-product-or-topic-event-type-YYYYMMDD`，小写 ASCII 且以连字符分隔。CSV 使用 JSON 数组字符串表示 keywords 与 secondary_sources，以便无损导入。
