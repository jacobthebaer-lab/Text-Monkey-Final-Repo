# Prompts changelog

Every change to a file in `/prompts` gets an entry here: which prompt, old →
new version, what was wrong, and what changed (including the eval failure or
observation that prompted it).

## fill_agent.md

- **v1** (2026-09-29): Initial version. The agent writes tranche outreach and
  may adjust urgency with a reason; all membership, timing, and eligibility
  limits are stated as code-enforced so the model doesn't try to negotiate.

## parser.md

- **v1** (2026-09-29): Initial version. Strict-JSON intent classifier with a
  sensitive flag and confidence score; explicitly forbidden from replying or
  counseling. Written against the sample texts in `data/sample_texts.json`.

## 2026-10-01 — signup v1

Added a dedicated Gloo signup extraction prompt. The earlier unknown-number
route only sent a church-office template, so new volunteers could not request
an account by text. Signup now creates a human approval and never grants admin
access, qualifications, an assignment, or automatic messaging consent.

## 2026-10-01 — fill agent v1 → v2

Jacob requested that Gloo choose replacements rather than only write asks for
fixed ranked batches. Gloo now selects from the full eligible pool and logs a
reason before outreach; tools retain eligibility, consent and batch limits.

## 2026-10-01 — signup v1 → v2

Jacob requested signup entirely by text. Gloo now reads the short SMS
conversation, including JOIN followed by a name. It requests missing details;
code creates an inactive profile and activates it only after an explicit
consent reply. Signup no longer requires a coordinator approval.

## 2026-10-01 — fill agent v2 → v3

A real Gloo run selected eligible replacements but supplied a description in
request_send_text.purpose. All asks were rejected. Constrained that parameter
to the outreach enum, made the prompt explicit, and added a completed-outreach
check so the app cannot report a sent batch when Gloo did not finish its tools.
