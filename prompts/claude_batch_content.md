# Claude Batch Content Instructions

Process every item in the request. Use the supplied sources as the starting point, verify claims against accessible original sources, and do not invent facts, quotations, dates, URLs, or image rights.

For each item, produce a detailed research brief and a complete Chinese Markdown news article. The article must use Markdown headings and paragraphs, contain no internal citation placeholders such as `[cite: 1]`, and distinguish verified facts from company claims. Do not add facts that the research does not support.

Search for up to three relevant image candidates. Include only images whose original landing page and original rights page show a clear basis of `PUBLIC_DOMAIN`, `OPEN_LICENSE`, `OFFICIAL_PRESS_LICENSE`, or `OWNED`. Exclude search thumbnails, ordinary editorial photos with unclear reuse rights, fair-use assumptions, and any image whose rights cannot be verified. Do not generate images or download files.

Return one JSON document that exactly follows the result schema implied by the request: preserve `schema_version`, `batch_id`, and `request_id`; use the requested `output_file`; include exactly one result item for every requested `article_id`; and add no extra fields. Save the JSON as the requested output filename and provide it as a downloadable file. Keep full deliverables in that file rather than printing the articles in chat.
