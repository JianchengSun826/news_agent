# Image Policy

优先顺序为：原创 AI editorial illustration、Wikimedia Commons、Unsplash、Pexels、明确允许媒体使用的官方 press kit。禁止默认使用 Google Images 结果、媒体页面图片、Reddit/X 图片、YouTube 截图和未知来源图片。

AI 图片记录 provider、model、prompt path 和 generated_at；外部图片记录 source URL、creator、license、license URL 和 attribution。任何字段不足都不得进入 `IMAGE_READY`。

默认 image brief：16:9、editorial illustration、no text、no logos、no trademarks、no copyrighted characters、no identifiable celebrity unless justified、no imitation of a living artist。不得复刻已有新闻图片，应表达新闻概念而非复制产品宣传图。Phase 1 只生成 prompt，图片由人工生成或选择。
