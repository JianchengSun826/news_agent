# Independent Claude Batch Fact-check Instructions

Open this request in a new Claude conversation with no reliance on the content-writing conversation. Check every article independently against its supplied primary and secondary sources. Verify each material factual claim, date, number, attribution, product name, quotation, and the distinction between confirmed facts and company claims.

Return exactly one result for every requested `article_id`. Set `outcome` to `PASS` only when the article has no blocking factual problem. Set it to `FAIL` when a material claim is unsupported, contradicted, misleading, or cannot be verified; list each blocking problem in `blocking_issues`. Write a concise claim-level Markdown report in `report_md`. Do not research, select, or assess images in this conversation.

Return one JSON document that preserves `schema_version`, `batch_id`, and `request_id`, uses the requested `output_file`, matches the request membership exactly, and contains no extra fields. Save it as the requested filename and provide it as a downloadable file. Keep the complete reports in that file rather than expanding them in chat.
