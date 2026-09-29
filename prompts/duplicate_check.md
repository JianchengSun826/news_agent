---
version: 1
provider: generic
purpose: duplicate_check
---

Compare the candidate with known events using company, product, event type, event date, core fact, entities, keywords, and primary source. Return exactly one classification: NEW, FOLLOW_UP, RELATED, POSSIBLE_DUPLICATE, or DUPLICATE, followed by evidence and uncertainty. Do not rely on title similarity alone.
