# Read-only admin notification status

`GET /api/notification-status?limit=100&offset=0` uses the existing verified
Supabase coordinator allowlist and backend bridge authentication. It reads only
the current configured church database. That store has no per-assignment tenant
field; this endpoint does not claim new account isolation or accept an owner or
church selector. It exposes no phones, text bodies, credentials, session IDs or
private native journals. No POST or activation operation is provided.

`limit` is an assignment count from 1 through 100; `offset` is nonnegative.
Each page contains two projections per recorded placement: `scheduled` and
`day_before`. Recorded approved, confirmed, cancelled and completed placements
with events ending within the past 24 hours or later are included, ordered by
event start and assignment ID. Proposed offers are excluded. Cancelled placements
are shown as suppressed. `next_offset` is null on the last page.

The endpoint reuses the actual assignment, eligibility, policy, exact-review and
reminder source checks. It links existing `job:assignment:{id}` /
`job:reminder:{id}` receipts, exact approvals, messages and `conversation_delivery`
reservations. Audit `conversation_source`, `human_review` and suppression records
never become extra notification items. There is no parallel scheduling system.
Terminal `blocked_policy` jobs expose known code-owned `policy_reason` labels;
unrecognized saved notes use the generic suppression label and remain private.

## Response contract

The envelope contains `generated_at`, `timezone`, `read_only: true`,
`runtime`, `notifications` and `next_offset`.

`runtime` has `provider`, `automation_configured`, `confirmation_required`,
`gloo_configured`, `messages_connection: "not_checked"` and
`scheduler_running: "not_checked"`. Configuration is not evidence of running
timers, working Messages, a successful Gloo call or delivery.

Each notification contains:

| Field | Meaning |
| --- | --- |
| `id` | Stable `assignment:{id}:scheduled` or `assignment:{id}:day_before` |
| `assignment_id`, `event_id`, `volunteer_id` | Existing saved record IDs |
| `recipient_name`, `role`, `event_title`, `starts_at` | Saved record labels and church-local start |
| `notice` | `scheduled` or `day_before` |
| `due_at`, `due_basis` | Zoned time and provenance described below |
| `state`, `reason`, `next_step` | Current projection and concise internal action |
| `approval_id`, `message_id` | Linked existing IDs, or null |
| `provider_message_status` | Raw recorded message status, or null; not delivery proof |
| `delivery_evidence` | `mock_only` or `not_recorded` |
| `recipient_session` | `not_required`, `not_selected`, `missing`, `not_started`, `active` or `expired` |

`due_basis` is `recorded_job`, `recorded_notification`, `assignment_created` or
`local_day_before_window`. Existing saved job/notification times take precedence.
Without those records the scheduled notice uses assignment creation time, and the
reminder uses midnight at the beginning of the local day-before window. Midnight
is the start of that date window, not a promise to send during quiet hours or at
an invented fixed hour. The view never prepares a review or advances a job.

| State | Interpretation |
| --- | --- |
| `scheduled` | Planned window or due workflow; running timer remains unverified |
| `held` | Paused configuration, consent/care/eligibility/session/source/review/Gloo/quiet-hours hold, or delivery awaiting verification |
| `awaiting-review` | Existing valid source-bound exact text awaits human review |
| `queued` | Existing approved provider queue or dispatch claim, still delivery-unverified |
| `suppressed` | Rejected/suppressed workflow, superseded notice, cancelled placement or ended window |
| `verified-delivered` | Reserved; not produced by the current backend |

Native acknowledgments currently persist `submitted` or `uncertain`; there is no
authoritative durable device-delivery proof exposed to this API. `sent`,
`submitted`, `delivered`, a provider SID and queue status alone cannot produce
`verified-delivered`. Submitted records remain `held` with the reason
“Submission recorded; device delivery is awaiting verification.” and a reconcile,
do-not-resend next step. Mock results are explicitly `mock_only`. Uncertain native
attempts remain held for reconciliation. Private delivery evidence stays outside
this API and Git; this change does not ingest or invent it.

## Validation

**23 notification-status checks passed** against the current central conversation
precheck. Fictional SQLite fixtures cover authenticated route
registration, missing/unverified/unallowlisted login, SQL write rejection during
GETs, repeat reads, mutation rejection, pagination, local due windows, audit/offer
exclusion, paused configuration, consent/care/qualification holds, pending exact
review, changed schedule/phone, expiry, quiet hours, recipient sessions, queue /
submitted / uncertain / mocked labels and terminal suppression.

These checks use model/provider doubles and mocked authentication. No real Gloo,
Supabase, Messages, assignment, database, text or scheduler operation was performed.
