# Prompts changelog

Every change to a file in `/prompts` gets an entry here: which prompt, old →
new version, what was wrong, and what changed (including the eval failure or
observation that prompted it).

## fill_agent.md

- **v1** (2026-09-29): Initial version. The agent writes tranche outreach and
  may adjust urgency with a reason; all membership, timing, and eligibility
  limits are stated as code-enforced so the model doesn't try to negotiate.

## planning_agent.md

- **v1** (2026-10-01): Initial version. Draft-review role only: the solver
  and validator are code; the agent proposes swaps (each re-validated) and
  writes the coordinator summary. Explicitly told not to chase a perfect
  schedule so it doesn't burn review rounds on optional-role gaps.

## parser.md

- **v1** (2026-09-29): Initial version. Strict-JSON intent classifier with a
  sensitive flag and confidence score; explicitly forbidden from replying or
  counseling. Written against the sample texts in `data/sample_texts.json`.
