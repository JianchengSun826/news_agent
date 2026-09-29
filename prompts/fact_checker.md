---
version: 1
provider: generic
purpose: fact_check
---

Check the registered draft claim by claim against the Research Brief and source URLs. Report incorrect dates or numbers, unsupported claims, missing attribution, source gaps, misleading wording, and duplicate-event risk. Return PASS only when no blocking issue remains; otherwise return FAIL with exact corrections. Do not treat repeated media reports as independent confirmation.
