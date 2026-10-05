# Claude Batch Revision Instructions

Revise every requested article using its fact-check report and all listed blocking issues. Correct the Research Brief first, then revise the Chinese Markdown article so every material statement is supported by the supplied sources. Preserve accurate content, remove unsupported claims, keep company claims clearly attributed, and do not invent replacement facts, quotations, dates, numbers, or URLs.

Create exactly this JSON shape. Copy the top-level identity values from the request, include exactly one complete revision for every requested `article_id`, and add no extra fields:

```json
{
  "schema_version": 1,
  "batch_id": "<copy request batch_id>",
  "request_id": "<copy request request_id>",
  "output_file": "<copy request output_file>",
  "items": [
    {
      "article_id": "<copy input article_id>",
      "research_brief_md": "<complete revised Markdown Research Brief>",
      "title_cn": "<revised Chinese headline>",
      "slug": "lowercase-ascii-slug",
      "article_md": "<complete revised Chinese Markdown article>"
    }
  ]
}
```

Return complete files rather than patches or suggested edits. Save only the JSON document as the requested `output_file` and provide it as a downloadable file. Keep the complete revisions in that file rather than expanding them in chat.
