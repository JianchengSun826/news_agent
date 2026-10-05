# Independent Claude Batch Fact-check Instructions

Perform this task in a new conversation with no reliance on the writing conversation. Check every article independently against its supplied primary and secondary sources. Verify each material factual claim, date, number, attribution, product name, quotation, and the distinction between confirmed facts and company claims. Do not assess images.

Use `PASS` only when there is no blocking factual problem. Use `FAIL` when a material claim is unsupported, contradicted, misleading, or cannot be verified. A PASS item must have an empty `blocking_issues` array; a FAIL item must list at least one blocking issue.

Create exactly this JSON shape. Copy the top-level identity values from the request, include exactly one item for every requested `article_id`, and add no extra fields:

```json
{
  "schema_version": 1,
  "batch_id": "<copy request batch_id>",
  "request_id": "<copy request request_id>",
  "output_file": "<copy request output_file>",
  "items": [
    {
      "article_id": "<copy input article_id>",
      "outcome": "PASS or FAIL",
      "report_md": "<concise claim-level Markdown fact-check report>",
      "blocking_issues": []
    }
  ]
}
```

Save only the JSON document as the requested `output_file` and provide it as a downloadable file. Keep the complete reports in that file rather than expanding them in chat.
