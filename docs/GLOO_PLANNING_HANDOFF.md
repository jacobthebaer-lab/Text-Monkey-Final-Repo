# Connected planning and reminder composition

Baseline: `9ed9d71203ed86989455e81a6fca2717a8017682`. This isolated patch changes
planning/reminder composition and scheduling guardrails, not Clyde's outreach
selection, ranking, tranche or cadence algorithm. No transport worker, signup
parser, admin UI, Planning Center code or shared checkout was changed.

## Ready core paths

- `planning_agent.collect`: requires an approved `collect_availability` parent,
  current consent, active volunteer, and missing availability. Gloo must compose
  the month and reply options. Connected transports additionally require exact
  confirmation mode and an active recipient scope. The reminder requires an
  initial submitted/sent message and a three-day wait; an unreviewed, queued or
  uncertain initial ask cannot trigger it.
- `reminders.process`: planner confirmations, day-before reminders and Saturday
  coordinator summaries require Gloo composition. Church policy timezone is used.
  Existing eligibility, consent, care holds, ask limits and quiet hours stay in code.
- `planning_agent.plan_month` in exact mode: builds an in-memory monthly plan,
  preserving the existing monthly ranking and load preference. Gloo inspects and
  can repair/swap proposals within deterministic qualification, availability,
  overlap, occupancy and monthly limits. It stages individual `confirm_record`
  assignment reviews only after successful model review and hard-rule validation.
  It writes no Assignment until human review. Legacy bulk publication is blocked
  in exact mode. A failed model review stages no publication.
- `scheduler.validate` excludes an assignment from its own conflict query without
  temporarily mutating its status or creating unintended record reviews.

Every workflow receipt stores composed copy, source fingerprint and review ID.
Repeated ticks reuse pending reviews. Rejected reviews do not auto-recreate.
Expired reviews require fresh human confirmation of cached, still-current copy.
Changed facts/recipient/session require new composition and invalidate old reviews.
Gloo copy failures never send seed text; retry is bounded to three attempts with
two-minute spacing, then a system escalation holds the receipt for operator review.
An uncertain direct queue submission is held without automatic retry.

The small `confirmations.py` hooks bind workflow metadata into the content hash,
check source state at exact approval/claim/native preflight, and validate planning
source/consent/care/load limits before applying a reviewed assignment. Mutable
schedule records and coverage are read fresh, including after model network calls.

## Integration and activation boundaries

The broad connected guards in `app/jobs.py` and `app/web/operations.py` are
**unchanged** in this patch. Do not remove them wholesale: unrelated legacy admin,
capacity and calendar controls were not released here. Existing fill/event jobs
and newer user-facing UI stay intact.

The integration owner can call the released helpers from the connected scheduler
only with exact confirmation enabled. A narrow connected job branch may run
`reminders.process(ctx)`, `collect(ctx, approved_collection)` and
`plan_month(ctx, month)` while retaining guards for other legacy controls.
Record a plan job as completed only when review succeeded (`pending_exact_review`
or an explicitly handled no-change result), not when Gloo failed; otherwise the
old job receipt would prevent a retry after an outage.

Availability collection still needs an explicitly authorized coordinator parent
approval. The existing generic `collect_availability` approval is not a supported
exact-record action in the signed-in review endpoint. Do not auto-approve it or
enable the legacy Basic-auth operations page in confirmation mode. An authenticated
request/collection-approval endpoint is a separate integration requirement. Each
resulting text must still use its own signed-in exact recipient/content review.

No scheduler, Messages session, runtime, account or real delivery was activated.
Queue submission/native submission do not prove carrier delivery. Tests use
fictional recipients, mock/queue-only doubles and isolated SQLite.

## Verification

Full isolated backend: **583 passed, 1 unchanged expected failure**, on the final
code. The frozen quiet-hours eval still conflicts with the newer immediate
sender-reply policy; no eval expectations or case criteria were changed.
Focused regressions cover Gloo outage/invalid copy, bounded retries, repeated ticks,
rejected/expired reviews, quiet-hour approval, consent/recipient changes, answered
availability, unsubmitted initial asks, stale schedule/qualification/care/overlap,
changed summary coverage, stale ORM caches, model latency past event time, missing
native recipient scope, and exact monthly assignment publication/load checks.

[Sanitized real-Gloo evidence](evidence/gloo-planning-composition.json): five actual
Gloo API calls passed availability composition, confirmation/reminder composition,
monthly plan review, repeat-tick dedup and exact approval to **mock transport only**.
Three mock messages were submitted and zero real messages were sent. Usage was
11,453 input and 1,236 output tokens. The evidence contains no credentials, private
paths, native receipts, personal records or raw model/conversation content.

Run `pytest -ra` in the isolated checkout to reproduce the credential-free suite.
Private Gloo credentials were loaded through the existing configuration for the
targeted model test, never printed or committed. Generated raw logs remain ignored.
