# Image Policy

Phase 1.1 的图片搜索由第一个 Claude App 整批内容对话完成。每篇文章返回最多三个候选，编辑可明确选择无图发布，或选择并登记一张本地原图。Gemini 的角色止于发现和审核后的 CSV，不参与导入后的图片搜索或内容处理。程序不访问网络、不下载图片，也不替编辑判断使用权利。

候选必须包含原始 `landing_page_url`、适用的 `rights_url`、权利依据、权利声明、相关性及署名要求。开放许可须有作者和许可名称；其他依据不强制提供个人作者，不得猜测作者。支持的依据仅为 `PUBLIC_DOMAIN`、`OPEN_LICENSE`、`OFFICIAL_PRESS_LICENSE`、`OWNED`。开放许可和官方新闻素材必须有明确许可；需署名时必须给出完整署名文字。可检索 Wikimedia Commons、Unsplash、Pexels 或明确允许媒体使用的官方 press kit，但平台名称、搜索结果或官方身份本身不能证明授权。

禁止采用搜索缩略图、来源不明的媒体页面图片、Reddit/X 图片、YouTube 截图或仅声称 fair use 的图片。`direct_image_url` 只提供定位线索，不能代替原始落地页和权利页。历史 AI 图片 provenance 保留在旧记录及归档中；当前支持流程通过 Claude 图片候选和整批本地图片计划登记，不提供单篇图片生成或登记命令。

全部有效文章通过独立事实核验后，双击 `actions/04-登记整批图片.command`。逐篇明确选择无图，或选择候选，打开原始落地页和权利页，确认作者、许可和署名一致；人工下载原图，检查画面、格式、分辨率、新闻相关性和是否误导，再选择本地文件并明确确认权利。对真实照片不得用插画冒充现场，不得把未经证实的图像内容当作新闻事实。

确认过的选择先保存到批次 `image-selections.json`，取消时保留草稿，不改变文章状态。整批每篇恰好一个选择：有图项文件可读且 `rights_confirmed` 显式为 `true`；无图项 `no_image` 显式为 `true` 并提供非空理由。全部通过后，才统一暂存复制并在一个事务内登记。缺少或重复文章、错误候选归属、缺少文件或权利确认均拒绝整批，不部分登记。`EXCLUDED` 和 `REUSED_COMPLETE` 不需要当前批次图片。CLI 备用入口为 `newsroom batch image-plan BATCH_ID PLAN_JSON`。

原图保存到 `workspace/batches/OWNING_BATCH_ID/articles/ARTICLE_ID/hero.EXT`，永久权利记录保存于 SQLite `images`。发布包复制同一图片及 `image-license.json` 到批次 `publish/ARTICLE_ID/`；审核与批准要求所有有效文章分别达到 `IMAGE_READY` 和 `REVIEWED`。图片候选视图可以重建，已登记的图片与版权记录不依赖 Claude 返回文件继续存在。混合已登记与未登记的旧状态需要恢复或管理员修复，Finder 操作会拒绝继续部分登记。

无图选择绑定当前稿件版本并保存在 SQLite 审计中，缺失图片不自动等于无图。无图文章不生成 hero、image-license.json 或 image-brief.md，checklist 明确记录无图。没有合法候选可选择无图，不得用虚假许可或占位图片绕过权利检查。

内容导入后，可通过整批候选补充命令 `batch image-candidates-import` 追加已验证候选；不修改稿件、核验或已归档返回文件。详见 WORKFLOW 第 6 节。

## 适用授权与待人工判断推荐

核验接受图片文件页、适用的全站媒体政策、开放许可证或可记录的书面授权，不要求每张图片都有单独许可页面。官方媒体政策须适用于具体图片和预期编辑用途，记录资格、使用范围、第三方限制及必要署名。CC0 不强制署名。下载按钮、官方身份或版权标记本身均不能单独证明许可或禁止；页面无法访问代表待核验。

Claude 返回中 `image_candidates` 仍为最多三个已核实候选；可另提供最多三个 `image_recommendations`，记录原始来源、相关性、权利线索与未通过原因。原因分为 `PAGE_INACCESSIBLE`（页面无法访问）、`INSUFFICIENT_EVIDENCE`（证据不足）、`RESTRICTED_USE`（存在不适用的明确限制）。推荐不会写入 SQLite 图片候选，不会出现在 04 的可选列表或发布包中，也不阻塞无图发布。

原始推荐保存在已归档 content result，刷新生成文章目录的 `image-recommendations.json` 和 `image-recommendations.md`，批次 README 提供链接。原始图片与产品、事件的差异必须标明；推荐不等于确认授权。

文章目录还生成 `manual-image-candidates.json` 人工补充草稿，其中权利依据、声明和署名要求初始为 null，不可直接导入。用户可复制到自己的文件，删掉不采用项，核对并填写适用许可、权利链接、使用范围与署名，再通过 `batch image-candidates-import`（含 actor 和判断理由）加入候选。存在明确使用限制时，应取得适用的另行授权后填写依据。刷新不覆盖已有草稿；补充只追加候选，不修改原始推荐或稿件/核验。最终使用仍通过 04 的人工权利确认。
