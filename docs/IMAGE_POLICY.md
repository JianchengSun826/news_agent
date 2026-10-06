# Image Policy

Phase 1.1 的图片搜索由第一个 Claude App 整批内容对话完成。每篇文章返回最多三个候选，编辑最终选择并登记一张本地原图。Gemini 的角色止于发现和审核后的 CSV，不参与导入后的图片搜索或内容处理。程序不访问网络、不下载图片，也不替编辑判断使用权利。

候选必须包含原始 `landing_page_url`、`rights_url`、作者、权利依据、权利声明、相关性及署名要求。支持的依据仅为 `PUBLIC_DOMAIN`、`OPEN_LICENSE`、`OFFICIAL_PRESS_LICENSE`、`OWNED`。开放许可和官方新闻素材必须有明确许可；需署名时必须给出完整署名文字。可检索 Wikimedia Commons、Unsplash、Pexels 或明确允许媒体使用的官方 press kit，但平台名称、搜索结果或官方身份本身不能证明授权。

禁止采用搜索缩略图、来源不明的媒体页面图片、Reddit/X 图片、YouTube 截图或仅声称 fair use 的图片。`direct_image_url` 只提供定位线索，不能代替原始落地页和权利页。历史 AI 图片 provenance 保留在旧记录及归档中；当前支持流程通过 Claude 图片候选和整批本地图片计划登记，不提供单篇图片生成或登记命令。

全部有效文章通过独立事实核验后，双击 `actions/04-登记整批图片.command`。逐篇选择候选，打开原始落地页和权利页，确认作者、许可和署名一致；人工下载原图，检查画面、格式、分辨率、新闻相关性和是否误导，再选择本地文件并明确确认权利。对真实照片不得用插画冒充现场，不得把未经证实的图像内容当作新闻事实。

确认过的选择先保存到批次 `image-selections.json`，取消时保留草稿，不改变文章状态。整批每篇恰好一个选择、全部文件可读且 `rights_confirmed` 显式为 `true` 后，才统一暂存复制并在一个事务内登记。缺少或重复文章、错误候选归属、缺少文件或权利确认均拒绝整批，不部分登记。`EXCLUDED` 和 `REUSED_COMPLETE` 不需要当前批次图片。CLI 备用入口为 `newsroom batch image-plan BATCH_ID PLAN_JSON`。

原图保存到 `workspace/batches/OWNING_BATCH_ID/articles/ARTICLE_ID/hero.EXT`，永久权利记录保存于 SQLite `images`。发布包复制同一图片及 `image-license.json` 到批次 `publish/ARTICLE_ID/`；审核与批准要求所有有效文章分别达到 `IMAGE_READY` 和 `REVIEWED`。图片候选视图可以重建，已登记的图片与版权记录不依赖 Claude 返回文件继续存在。混合已登记与未登记的旧状态需要恢复或管理员修复，Finder 操作会拒绝继续部分登记。
