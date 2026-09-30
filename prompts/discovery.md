---
version: 1
provider: generic
purpose: discovery
---

Find recent technology events within the supplied date window. Return candidate metadata only: title, one-sentence neutral core fact, event date, company, product, topic, event type, keywords, primary URL, optional secondary URLs, and discovery URL. Open every retained URL and do not invent facts or URLs. Keep a candidate with a valid primary source when no reliable secondary source exists; return an empty secondary list and mark it PRIMARY_ONLY. Mark rumors clearly.
