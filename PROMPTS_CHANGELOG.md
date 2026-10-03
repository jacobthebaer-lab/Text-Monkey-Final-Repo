# Prompts changelog

## October 3, 2026 — nullable frequency contradiction

Profile interpreter v7 requires an actual bounded frequency value for its known
flag. Checked code treats known=true with max_per_month=null as unknown, retaining
an already validated prior frequency when one exists. Independently valid windows
survive; other invalid numeric values and invalid windows still reject. A trusted
Python-only recovery hook can consume a persisted, operator-bound Gloo decision
and compose the missing-question reply once without another parser call.

## October 3, 2026 — personalized exception recovery and role-window integration

Jacob authorized customized redirects for off-topic or incomplete signup replies
while preserving the four original messages for the normal path. Reply writer
v9 returns a checked short acknowledgment and only the code-selected missing
question; stage, missing fields, wording, claims and length are validated. Name
parser v5 distinguishes real name parts from unrelated phrases. Profile parser
v6 receives verified role/type catalogues and preserved recurring windows using
the separate checked availability module. Unknown times/frequency stay unknown,
and a frequency-only followup never erases role/time/context constraints.
Newly stated role interests are preserved without granting clearance or shifts.
Jacob's hard prohibition on em dashes is enforced in this reply writer and its
prompts; rejected output is held without substitution. No real texts or model
calls were made for this conversation-editor release.

## October 3, 2026 — restore original submitted signup copy

The previous concise release added wording Jacob had not submitted. Reply writer
v8 adds verbatim exact-copy enforcement through Gloo. Signup parser v4 extracts
identity from a real name reply to the disclosed invitation; application code
checks the same recipient, scoped app invitation, send receipt and actual newer
incoming message. Explicit recipient exact mode removes the added YES step and
deleted clarification texts, retaining backend opt-out. Original punctuation,
quotes and emojis are preserved, with only recipient name substitution. No
template fallback or real sends were performed by this release.

## October 3, 2026 — concise signup copy and fewer texts

Jacob consolidated the introduction and consent request, removed the repeated
availability message, and allowed light monkey and other friendly emoji. Signup
parser v3 recognizes a full name plus YES in one incoming message; code validates
the exact affirmative token, independently of the model. Reply writer v7 keeps
that request in one text, uses at most one permitted emoji with spacing, and
never adds a new question to a completed profile. Profile interpreter v5 names
Anything as the flexible-role alias. Newly started onboarding completes after
known availability even if no frequency was supplied; frequency stays unknown
and the established scheduler cap still applies. Existing partial legacy
conversations retain their targeted missing-facts handling. No template fallback
or real delivery was introduced.

## October 3, 2026 — onboarding interpreter v3 → v4

Natural multiday and all-day answers now carry validated partial availability
between replies. The interpreter receives saved facts, extracts corrections
without erasing other days, and distinguishes missing frequency from missing
availability. Application code requests only the missing information and keeps
explicit date exclusions during FLEXIBLE/SKIP. Synthetic regressions and three
real-Gloo/mock-delivery scenarios verified the change; no real text was sent.

## October 3, 2026 — signup reply writer v5 → v6

Added separate administrator draft wording preferences to the Gloo reply facts.
Canonical application facts remain authoritative: draft copy cannot establish
consent, clearance, a booked shift, or a completed profile. Only an explicitly
bound recipient uses that administrator’s saved draft; unbound inbound signups
retain canonical wording. The existing occasional, varied emoji rules remain.

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

## October 3, 2026 — parser v2

A live synthetic self-harm cancellation received non-schema guarded output; the earlier parser escalated care but lost the explicit cancellation. Added a prompt instruction to classify logistics separately, and a strict code backstop for explicit first-person cancellations when a sensitive model response cannot be parsed. Care remains human-only. No case criteria were changed.

## Signup reply composition follow-up, October 3, 2026

Collected the completed signup-reply prompt and regression checks from the integrated demo: emoji-free copy is the default; at most one permitted, non-repeating monkey emoji may be retained after spacing checks. The prompt and code preserve consent instructions and exact application facts. The earlier full-body forced suffix remains in Git history for comparison.
