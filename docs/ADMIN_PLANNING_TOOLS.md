# Evidence-backed planning in Ask Text Monkey

The existing signed-in **Ask Text Monkey** form uses `POST /api/coordinator/command`
with `{ "coordinator_id": <saved active coordinator>, "command": "..." }`.
Its existing Gloo admin agent now exposes three tools after `read_context`:

- `learned_patterns({volunteer_id})` reads completed assignments and completed
  event evidence from the previous three calendar months. It returns the existing
  helper's proposal/evidence plus an opaque `evidence_hash`. Duplicate services
  and insufficient months never establish a serving rhythm.
- `stage_pattern_review({volunteer_id, evidence_hash})` accepts only that freshly
  read, unchanged learned proposal. It returns an existing `confirm_record`
  review ID, never an applied change. The ordinary proposal UI shows the exact
  before/after and requires signed-in human approval of its content hash.
- `seasonal_staffing_report({month: "YYYY-MM"})` compares recorded event/role
  slots from at least two previous years, for the current or next twelve months.
  It reports evidence and missing-history limits; it does not change staffing.

Example requests: “What serving rhythm does this volunteer's completed history
support?” or “Review December staffing against our recorded seasonal events.”
Choose the person from actual saved context. These tools do not accept arbitrary
model-written calendar patterns. Explicit new preferences remain a separate
human/sender review; one-time December absence is never an annual absence rule.
Existing explicitly saved annual restrictions may be preserved by the helper.

Pattern staging binds the coordinator, exact current profile, original completed
assignments, shifts, events and church timezone through existing
`admin_change_source` confirmation checks. Changed evidence between model turns
holds staging; changed original records or coordinator access hold exact approval.
No phone, private profile notes or full before/after profile is returned to Gloo
by the new tools. Reports can expose relevant calendar evidence to the authorized
coordinator. Gloo outage/step exhaustion expires newly staged reviews through the
existing admin-agent behavior; no canned response or automatic approval follows.

There are no assignments, texts, qualification grants, schedule publication,
remote writes, attendance estimates or calendar imports in these tools. Pending
review is not scheduling eligibility or consent. The existing UI/API paths are
unchanged; the new tool definitions are passed in the usual Gloo Responses call.
