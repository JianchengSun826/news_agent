# Claude Batch Content Instructions

Process every input item. Use the supplied sources as the starting point, verify claims against accessible original sources, and do not invent facts, quotations, dates, URLs, or image rights.

For each item, produce a detailed Research Brief and a complete Chinese Markdown news article. The article must use Markdown headings and paragraphs, contain no internal citation placeholders such as `[cite: 1]`, distinguish verified facts from company claims, and add no unsupported facts. Use a unique lowercase ASCII slug with hyphens.

Search for zero to three relevant image candidates. Include an image only when its original landing page and original rights page establish `PUBLIC_DOMAIN`, `OPEN_LICENSE`, `OFFICIAL_PRESS_LICENSE`, or `OWNED`. Exclude search thumbnails, ordinary editorial photos with unclear reuse rights, fair-use assumptions, and records without an original rights page. Do not generate or download images.

Create exactly this JSON shape. Copy the top-level identity values from the request. Include exactly one item for every requested `article_id` and no extra fields:

```json
{
  "schema_version": 1,
  "batch_id": "<copy request batch_id>",
  "request_id": "<copy request request_id>",
  "output_file": "<copy request output_file>",
  "items": [
    {
      "article_id": "<copy input article_id>",
      "research_brief_md": "<complete Markdown Research Brief>",
      "title_cn": "<Chinese headline>",
      "slug": "lowercase-ascii-slug",
      "article_md": "<complete Chinese Markdown article>",
      "image_candidates": [
        {
          "landing_page_url": "https://original.example/page",
          "direct_image_url": null,
          "creator": "Creator name",
          "rights_basis": "PUBLIC_DOMAIN | OPEN_LICENSE | OFFICIAL_PRESS_LICENSE | OWNED",
          "license_name": "License name",
          "rights_url": "https://original.example/rights-page",
          "rights_statement": "What the original rights page explicitly permits",
          "attribution_required": true,
          "attribution_text": "Required attribution",
          "relevance": "Why this image matches this article"
        }
      ]
    }
  ]
}
```

For `OPEN_LICENSE`, `creator` and `license_name` cannot be null. When `attribution_required` is true, `attribution_text` cannot be null. Use JSON `null`, never the text `"null"`. Save only the JSON document as the requested `output_file` and provide it as a downloadable file. Keep the full deliverables in the file rather than printing articles in chat.
