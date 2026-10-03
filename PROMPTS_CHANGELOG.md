# Prompts changelog

## 2026-10-02 — onboarding v2 → v3

The user-supplied test screenshot included Sunday availability with a next-
Sunday exception, Wednesday/Thursday availability, and a January exclusion.
Clarify weekday mapping, all-day semantics and whole-month ISO exclusions.
Synthetic extraction/storage/eligibility tests cover these facts; a charged
live-model extraction remains unrun.

## 2026-10-02 — reply writer v2 → v3

Jacob supplied a confusing completion that asked YES/NO without a pending
offer and repeated STOP/HELP after opt-in. Preserve initial and pre-consent
disclosures, keep command guidance only in the first introduction, and
permit RSVP instructions only when the approved application facts contain
an offer or consent request. The completion now says preferences were saved
and a matching shift's details will arrive later. Command handling is unchanged.

## 2026-10-01 — signup reply writer v1

Added Gloo-generated signup wording for the live Mac test. Jacob requested
that replies come from Gloo rather than an operator. Code still owns consent,
allowlisted delivery and required disclosures; invalid model output sends nothing.

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

## 2026-10-01 — onboarding v1; reply writer v1 → v2

Added Gloo profile extraction for role interests, recurring service times,
explicit available/unavailable dates and monthly serving limits. Application
code validates catalogue IDs and dates; preferences never grant credentials.
Expanded the reply writer to validated assignment and staffing facts so Gloo
can compose transactional replies without deciding roster changes.

## 2026-10-01 — fill agent v3 → v4

Reserve space for code-generated YES/NO directions and an unambiguous offer
code. Bound asks to 260 characters before the suffix; no duplicated RSVP
instruction. The model still chooses eligible volunteers and writes each ask.

## 2026-10-01 — onboarding v1 → v2

A real Gloo smoke test returned both examples under stage keys rather than a
flat extraction. Explicitly require only the current stage; tolerate a known
current-stage wrapper while retaining every field check. Never read another
stage’s values or grant inferred preferences.

## 2026-10-02 — fill_agent v5

Replacement offers are sequential. Removed offer-code instructions so volunteers can reply naturally; application code appends the exact local deadline and enforces the response policy.
