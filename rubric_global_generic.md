# Research Report Grading Rubric

This rubric is source-agnostic. Apply it to any research report, paper, or
synthesis, regardless of the tool or pipeline that produced it.

## Criteria (0–10 each)

| Criterion | Weight | 10 means | Penalize |
|---|---:|---|---|
| Factual accuracy | 25% | Core facts and calculations are correct; claims are supported; there are no contradictions | Wrong numbers, wrong dates, internal contradictions, unsupported claims |
| Coverage | 20% | All parts of the request and required tradeoffs are addressed | Missing major dimension, shallow treatment |
| Critical analysis | 20% | Distinguishes measured from estimated; notes uncertainty, confounders, limitations, and contrary evidence | Overconfident single-number answers, no caveats |
| Source quality and citations | 15% | Relevant, credible sources; citations support claims; good citation density | SEO or irrelevant sources, citation mismatch, unsupported paragraphs |
| Structure and readability | 10% | Logical sections, scannable, coherent | Dense wall of text, poor organization, repetition |
| Actionable conclusion | 10% | Clear recommendation or decision framework with tradeoffs | No conclusion, data dump, vague advice |

**Overall** = `0.25*accuracy + 0.20*coverage + 0.20*critical + 0.15*sources + 0.10*structure + 0.10*action`.

## Grading discipline

- Grade only the supplied text. Do not infer the producing model or pipeline.
- If the report has no independent fact checklist, use internal consistency and
  its own cited sources rather than external knowledge.
- Mark factual confidence `low` when there is no checklist.
