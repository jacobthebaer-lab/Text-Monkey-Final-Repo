# Planning Center staffing contract

Verified October 3, 2026. This is an implementation handoff for the cloud owner,
not working two-way code. The current bridge imports service times and open
needs only. No people, roster, assignment writeback or consent import exists.
The initial contract audit was read-only. The separately authorized repair
changed only the two empty synthetic PlanTimes and refreshed the isolated import.

## Live scope and timing

Church of Clyde organization 545298, synthetic service 1826236, plans 92466235
and 92466244. Both have zero scheduled people, private visibility, disabled
reminders, and 2 Greeter/2 Usher/1 Production Operator needs. All three teams
are plan-wide and have `default_prepare_notifications:true`, `default_status:U`.
Do not rely on those defaults for outbound operations.

The initial audit found 09:00–10:00 UTC / 03:00–04:00 Denver on October 4/11.
Authorized repair at 12:52 PM Denver saved **15:00–16:00Z / 09:00–10:00 Denver**,
verified fresh API readback and explicit isolated resync. The final 12:55 PM user override sets October 4 to 10–11 AM Denver / 16:00–17:00Z;
October 11 remains 09–10 AM Denver / 15:00–16:00Z. Source/mapping IDs are unchanged.
See `evidence/planning-center/final-demo-service-times.json`.
The cause of the initial discrepancy is not proven. API times are
returned in UTC; use canonical UTC Z writes and assert equality.
[Dates & Times](https://api.planningcenteronline.com/docs/overview/dates-times),
[PlanTime](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan_time).

## Identity and inbound staffing

Use an explicit organization-scoped local volunteer ↔ Services Person ID map;
never infer identity from display name. Verify an existing Person with
`GET /services/v2/people/{id}`. Services Person is an app-specific resource;
the docs do not expose a mobile-phone association on this vertex. A separate
authorized People/contact mapping may be needed; do not assume IDs across
apps or that roster presence grants SMS consent.
[Person](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/person).

Eligibility is separate from a plan assignment. Read
`/services/v2/service_types/{service_type_id}/team_positions/{team_position_id}/person_team_position_assignments`:
these link an existing person to a team position, including scheduling preferences.
Creating that membership is a separate write and is outside the present audit.
[PersonTeamPositionAssignment](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/person_team_position_assignment).

Read plan staffing with `GET /services/v2/service_types/{service_type_id}/plans/{plan_id}/team_members`.
Import authoritative assignment/status, person, team and position into distinct
external mappings. Retain declined records for history; do not count them as
coverage. Resolve each membership's service times before attaching it to a
local event.
[Plan](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan).

## Minimum outbound API

For an already mapped eligible person, create a PlanPerson using
`POST /services/v2/service_types/{service_type_id}/plans/{plan_id}/team_members`.
The documented assignable fields include `person_id`, `team_id`,
`team_position_name`, `status`, `prepare_notification`, `notification_prepared_at`,
`decline_reason`, `notes`, and `responds_to_id`. Send a JSON-API `PlanPerson` body.
Set `status:C` only after acceptance, `U` for unconfirmed, `D` for declined.
Explicitly set `prepare_notification:false` and `notification_prepared_at:null`;
never inherit the team default. Save the returned PlanPerson ID and read back.

Update a mapped assignment with
`PATCH /services/v2/people/{person_id}/plan_people/{id}`; removal uses `DELETE`
at that same path. The public reference documents these operations but no
atomic replace action or generic outbound idempotency key.
[PlanPerson](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan_person).

Partial acceptance needs a separate contract: PlanPersonTime has per-time
status and read endpoints, but no public write endpoint in the current reference.
Begin with plan-wide teams and one service time per plan; hold split-team changes
until verified rather than pretending whole-plan status means one-time status.
[PlanPersonTime](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan_person_time),
[Team](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/team).

## Proposed reconciliation rules

These are application design requirements, not guarantees supplied by PCO:

- Persist an outbox intent keyed by organization, local assignment and revision;
  allow one serialized writer per plan/team/position. Store external IDs,
  expected prior state and verified outcome. Preserve consent in application code.
- Fresh-read staffing, eligibility and open need before writing. Ambiguous mapping,
  duplicate matching assignments or administrator changes produce a conflict.
  Do not overwrite newer PCO changes simply because a local intent exists.
- On uncertain POST completion, query staffing and reconcile before retrying.
  Do not blindly repeat creates. Absolute PATCH state is easier to reconcile;
  confirm removal after DELETE rather than expecting a JSON response body.
- A cancellation retains local history and maps an explicit decline to D; actual
  removal is a separately defined policy. Replacement is a non-atomic saga:
  verify the accepted replacement, record the outgoing change, verify both sides,
  and expose partial completion for repair. Never mark coverage complete early.
- Reconcile inbound staffing into stable external links rather than recreating
  all local shifts. A decrease in open needs must not delete occupied local
  assignments. The current open-needs-only importer needs redesign for this case.

NeededPosition is an **unfilled** count. The reference allows quantity PATCH and
DELETE at `/services/v2/series/{series_id}/plans/{plan_id}/needed_positions/{id}`;
get the series ID through the plan relationship rather than guessing it.
Our earlier live creation used a real `team_position_id`; plan-wide needs forbid
`time_id`. Automatic decrement/increment behavior after scheduling/declining
has not been exercised here. Re-read after staffing writes; do not also adjust
quantity unless observed behavior proves a separate update is necessary.
[NeededPosition](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/needed_position).

## Notifications, polling and acceptance limits

`prepare_notification:false` is a documented field, not proof that every external
automation is silent. Before a synthetic write test, verify saved notification
fields, plan reminder settings, team defaults and account automations. Do not
invoke email/notification actions or create people/invitations. Actual notification
suppression and write permissions still require a specifically authorized live
test with an existing synthetic person. No such test was performed here.

The real `/webhooks/v2/available_events` listing currently has Plan events but
no Services PlanPerson, PlanPersonTime or NeededPosition events. Existing Plan
subscriptions therefore do not prove roster-change coverage. Use bounded polling
and post-write reads for staffing; do not claim instantaneous two-way sync.
[AvailableEvent](https://api.planningcenteronline.com/docs/apps/webhooks/versions/2022-10-20/vertices/available_event).

Keep current raw-body signature verification and durable EventDelivery dedup.
Use webhooks as scoped refresh signals; avoid loopback writes from every refresh.
Cloud cutover must install private credentials/signing keys and a durable store,
then verify real delivery before retiring the Mac tunnel. Read response rate
headers and honor Retry-After; retry writes only after reconciliation.
[Webhooks](https://api.planningcenteronline.com/docs/overview/webhooks),
[Rate Limiting](https://api.planningcenteronline.com/docs/overview/rate-limiting).

Minimum live acceptance remains: existing synthetic person mapped; accepted
assignment create/readback; replay without duplicate; decline/removal/replacement
readback; observed open-needs behavior; zero notification delivery; inbound
external change via polling; database/process restart; cloud receiver delivery.
The original webhook proof verifies inbound plan refresh only.
