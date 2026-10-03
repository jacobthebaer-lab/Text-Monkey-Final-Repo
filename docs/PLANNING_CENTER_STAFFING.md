# Planning Center staffing slice

This implementation captures authoritative local assignment confirmation and
cancellation in the same database transaction, including the existing inbound
confirmation and fill replacement flows. It does not create People, import
contact details, grant consent, choose candidates or send PCO notifications.
Writes and polling remain off by default. There is no live staffing write proof.
The existing real webhook evidence verifies plan refresh only.

## Explicit mapping and activation

Use the current [staffing contract](PLANNING_CENTER_STAFFING_CONTRACT.md). Retain
its approved organization, plans and final UTC service times. Live activation
currently permits **Clyde only**; Noah texting is off. Code and synthetic fixtures
are generic, but they do not authorize another recipient or a live replacement.
No test here reads private credentials, contacts PCO or sends a real text.

1. Select the intended private store. Fresh SQLite startup creates the additive
   tables; PostgreSQL requires review/application of
   `supabase/migrations/20261003120000_pco_staffing_sync.sql`. This migration was
   not applied or verified against PostgreSQL. It targets the private `texty`
   schema, enables RLS, denies browser roles and grants CRUD plus serial sequence
   access to the existing `texty_backend` role. Confirm that role and permissions
   in the selected store before PostgreSQL activation; current proof uses SQLite.
2. Import the approved scoped plan. With an authenticated PCO client, explicitly
   call `map_volunteer(session, config, local_volunteer_id, services_person_id,
   now, client=client)`. It verifies the existing Services Person and organization;
   it cannot infer an ID from a name or phone, or grant SMS consent.
3. Call `map_position(session, client, config, shift_id=..., team_id=...,
   position_id=..., plan_time_id=..., now=...)` for each reviewed role/plan.
   It verifies the imported event/shift, team, position and exact service time.
   Only plan-wide teams with one service time are supported. Sparse/ambiguous
   PlanPerson time relationships, split teams and changed mappings are held.
4. Commit mappings. With both flags still off, call `refresh_staffing` explicitly
   for the reviewed service type/plan and commit. Inspect returned conflicts and
   durable links before enabling writes. This read does not produce outbound
   intents or change consent. Declines/removals retain cancelled local history;
   a subsequent remote confirmation can restore its unoccupied local slot.
   An existing local commitment without a remote link is held as a conflict;
   explicitly catch up that assignment rather than duplicating or adopting it.
5. Only under the current specific authorization, set
   `PCO_STAFFING_WRITE_ENABLED=true` and/or `PCO_STAFFING_POLL_ENABLED=true` in
   private configuration and restart the intended backend. Each flag is consumed
   by the application. A separate 60-second PCO job runs when enabled in real
   mode; `AUTOMATION_ENABLED=false` keeps general scheduling paused. The PCO job
   does not enable Messages, change sessions or authorize any texting.
6. New confirmed/cancelled transitions enqueue automatically. Already-confirmed
   assignments require explicit `catch_up_assignments(session, config,
   [assignment_id, ...], now)` and commit: at most 25 named IDs, no broad backfill.
   Identity-only signup does not create or schedule a PCO Person.

`app.jobs.process_pco_staffing(factory, settings, config, clock)` is the bounded
worker entry point for the same application job. It processes at most 10 intents
and polls at most 10 explicitly mapped plans per tick, at least 60 seconds apart.
Do not pass an inbound session: writes use separate durable transactions.
Flags alone do not prove an active process, working credentials or convergence.

## Verification and recovery

The worker fresh-reads credential organization, plan/time/team/position, Services
Person, current position membership, local consent/eligibility, staffing and open
needs. A create requires a current unfilled need. Confirmation of an existing
mapped unconfirmed reservation uses PATCH; it cannot silently adopt a different
external assignment. Status/identity/revision conflicts and missing mappings are
held with an explicit reason. Application metadata stores no new consent.

Each plan has one atomic database lease. Before any HTTP write, an intent's
`unknown` state is committed. A timeout, process crash or readback failure never
replays a create. Recovery reads staffing and reconciles the exact known response
ID when available; an absent or conflicting unknown outcome stays unsent for
review. The worker saves response IDs durably before further readback. Inspect
`pco_staffing_intents.state/reason/retry_at/expected/depends_on` and
`pco_staffing_polls.reason` for partial progress. Do not reset an unknown intent
to pending or delete its receipt to force a retry. Correct stale mappings/state
only after comparing PCO and local history; a new authoritative revision may then
be explicitly queued. Expired claims can be recovered after ten minutes; unknown
creates still cannot repeat. No automatic broad catch-up occurs.

Create uses documented `person_id`, `team_id`, `team_position_name` and status
attributes. Cancellation PATCH uses
`/services/v2/people/{person_id}/plan_people/{plan_person_id}` and records D.
Both operations explicitly write `prepare_notification:false` and
`notification_prepared_at:null`, then require those exact fields, identity and
status in fresh readback. No removal policy or DELETE operation is introduced.
These fields do not prove that other account automations are silent: live tests
must separately verify zero notification delivery and saved reminder settings.

Replacement acceptance depends on verified cancellation of the prior assignment;
unknown/held cancellation leaves the saga visibly partial. For PCO events, admin
coverage counts only a confirmed local assignment with verified remote C and no
unresolved latest intent. U remains reserved/unconfirmed and never counts as
covered. Required slots use fresh remote C/U reservations plus unfilled needs;
this conservative arithmetic must be checked against actual PCO open-needs
behavior during the separately authorized live acceptance. The importer preserves
occupied slots in addition to unfilled needs and never deletes assignment history.
An event-specific recipe prevents preserved historical slots inflating requirements.
No direct quantity adjustments or webhook loopback writes occur.

## Synthetic checks and remaining live acceptance

`pytest -q tests/test_planning_center_staffing.py` exercises real application
inbound confirmation/cancellation/fill acceptance, transaction rollback, flags,
independent scheduler wiring, unknown-before-HTTP visibility from another process,
file-store restart, concurrent claims, stale conflicts, notification readback,
covered/unconfirmed distinction, declines/reacceptance/deletion and preserved
occupied/open slots. All PCO and message delivery here use synthetic test doubles.

A specifically authorized live check still needs an existing eligible Services
Person mapped to the saved Clyde profile, reviewed position mappings, write
permissions, silent notification readback, observed open-needs behavior, native
Messages delivery and bounded inbound polling. Cloud receiver/storage cutover is
separate. Do not claim live two-way PCO or cloud-independent texting from mocks.

API fields/endpoints follow [PlanPerson](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan_person),
[Team](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/team),
and [position membership](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/person_team_position_assignment).
