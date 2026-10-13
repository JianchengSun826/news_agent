# Claude Batch Content Instructions

Execute this request as a complete task. Follow these instructions for every item in the enclosing JSON and create the downloadable JSON file named by its `output_file`. Do not merely summarize the attachment or ask the user to repeat the task in chat.

Process every input item. Use the supplied sources as the starting point, verify claims against accessible original sources, and do not invent facts, quotations, dates, URLs, or image rights.

For each item, produce a detailed Research Brief with these headings: Event, Core Fact, Why It Matters, Confirmed Facts, Company Claims, Independent Reporting, Important Numbers, Technical Background, Timeline, Primary Sources, Secondary Sources, Conflicting Information, Unknown / Unconfirmed, Potential Errors To Avoid, and Suggested Angle. Label evidence as FACT, COMPANY_CLAIM, MEDIA_REPORT, ANALYSIS, RUMOR, or ESTIMATE where useful, and keep direct source URLs in the brief.

Then write a professional Chinese Markdown news article of roughly 600–1000 Chinese characters. Cover what happened, important details, why it matters, necessary background, and what to watch; end with a Sources list. The article must use Markdown headings and paragraphs, contain no internal citation placeholders such as `[cite: 1]`, distinguish verified facts from company claims, avoid hype, and add no unsupported facts. Use a unique lowercase ASCII slug with hyphens.

Search for zero to three verified image candidates and separately zero to three useful image recommendations awaiting human judgment. Do not generate or download images.

For `image_candidates`, establish `PUBLIC_DOMAIN`, `OPEN_LICENSE`, `OFFICIAL_PRESS_LICENSE`, or `OWNED` through verifiable permission applicable to the particular image and intended editorial use. The `rights_url` may be the original image page, an applicable site-wide media policy, an open license, or a documented authorization; a separate per-image permission page is not required. Record scope, eligibility, credit requirements and relevant third-party limitations accurately. An official source, a download button, or a copyright notice alone proves neither permission nor prohibition. Require creator and license name for OPEN_LICENSE; do not invent a personal author where an organization is the credited creator. CC0 does not require attribution. An inaccessible page means verification is incomplete, not that reuse is prohibited.

For `image_recommendations`, preserve relevant original-source leads that did not pass verification. Explain the known rights information and the specific missing evidence or restriction. Use PAGE_INACCESSIBLE for unreadable evidence, INSUFFICIENT_EVIDENCE for missing or unclear permission, and RESTRICTED_USE for an explicit limitation inconsistent with the intended use. These are not approved candidates: never manufacture a rights basis, permission statement or clearance. Exclude search thumbnails and untraceable images; do not recommend fair-use assumptions as permission. Label archive photos and product differences accurately. An empty list for either category is acceptable; do not pad the lists.

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
      ],
      "image_recommendations": [
        {
          "landing_page_url": "https://original.example/photo",
          "direct_image_url": null,
          "creator": null,
          "rights_url": null,
          "rights_information": "Known rights information, or null if unknown",
          "verification_status": "PAGE_INACCESSIBLE | INSUFFICIENT_EVIDENCE | RESTRICTED_USE",
          "verification_issue": "Specific missing evidence or usage restriction",
          "relevance": "Recommendation reason and accurate archive-photo caption"
        }
      ]
    }
  ]
}
```

For `OPEN_LICENSE`, `creator` and `license_name` cannot be null. When `attribution_required` is true, `attribution_text` cannot be null. Use JSON `null`, never the text `"null"`. Save only the JSON document as the requested `output_file` and provide it as a downloadable file. Keep the full deliverables in the file rather than printing articles in chat.

Image handoff: images are selected, downloaded and uploaded to Google Drive by the editor after content export. Provide usable candidates only with verified applicable permission; explain usage conditions, required credit and why the image fits. Provide useful recommendations separately with specific missing evidence or restrictions and recommendation reasons. Do not assert unconditional guaranteed permission, invent clearance, require local downloads or request image registration. Empty lists are valid when evidence is unavailable.

Write rights_statement, rights_information, verification_issue and relevance in Chinese for the editor-facing image guide. Preserve actual license names, creator names and source URLs accurately; include specific recommendation reasons and appropriate archive/product-photo captions.
