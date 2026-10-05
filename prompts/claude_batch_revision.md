# Claude Batch Revision Instructions

Revise every requested article using its fact-check report and every listed blocking issue. Correct the Research Brief first, then revise the Chinese Markdown article so that every material statement is supported by the supplied sources. Preserve accurate content, remove unsupported claims, keep company claims clearly attributed, and do not invent replacement facts, quotations, dates, numbers, or URLs.

Return exactly one revised item for every requested `article_id`, including the complete revised `research_brief_md`, `title_cn`, lowercase ASCII `slug`, and complete revised `article_md`. Do not return a patch or a list of suggested edits.

Return one JSON document that preserves `schema_version`, `batch_id`, and `request_id`, uses the requested `output_file`, matches the request membership exactly, and contains no extra fields. Save it as the requested filename and provide it as a downloadable file. Keep the complete revisions in that file rather than expanding them in chat.
