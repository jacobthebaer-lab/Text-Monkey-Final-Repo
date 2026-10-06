# Saved schedule readiness and bounded demo handoff

Audited against integration `d0cb5f72874088c8c806ed4b4122918a34ade8a6`.
This lane changed no live database, recipient session, consent, qualification,
runtime, model connection or delivery worker. It sent no texts.

## Concrete readiness fix

`GET /api/setup/admin-texts` now distinguishes saved service-time preferences
from actual future, scheduled `Event` records. With zero events, scheduled-update
readiness is false even when transport and scheduling configuration are enabled.
The response includes `upcoming_event_count` and `next_event_at`, with an actionable
schedule check. A one-time connection check remains independent of the schedule.
The scheduling label describes configuration rather than claiming timer uptime.
An event without role slots can still produce the existing truthful "No required
staffing plan" update; its presence does not prove coverage.

The previous isolated John snapshot contained zero events despite saved Sunday
9AM/11AM preferences. Setup deliberately does not publish events from free text.
No automatic publication or destructive `app.db.seed` invocation is appropriate
for that private database.

## Explicit fictional preview from saved setup

Export the current setup JSON privately, or create a schedule-only JSON file with
the exact saved `timezone` and `service_times`. The tool accepts either a details
object or the `/api/setup` response's `details` object. Only the two schedule
fields are emitted; contact/profile fields are ignored.

```sh
python tools/preview_saved_schedule.py \
  --settings-json /absolute/path/to/private-schedule-settings.json \
  --sunday 2026-10-11 --duration-minutes 75 --role-id 1
```

The date and 75-minute duration above are **explicit fictional choices**, not facts
inferred from setup. Replace role ID 1 with an existing role verified in the current
runtime. Sunday 9AM/11AM in America/Denver yields 09:00–10:15 and 11:00–12:15,
admin windows opening at 06:00 and 08:00, and volunteer reminders on Saturday.
The 06:00 admin window is subject to saved quiet hours; no promise of delivery
at exactly 06:00 is made. The existing admin replay verifies fresh staffing after
that quiet-hours delay.

This CLI is a stdout-only, read-only proposal. It opens no database, loads no
credentials, calls no model and initializes no transport. It includes no people
or assignments. It refuses ambiguous service times, duplicate times, non-Sunday
dates, implicit durations and ambiguous clock-change times. It supports only the
narrow unambiguous Sunday grammar; other event schedules need explicit review.

For a bounded connected rehearsal, the runtime owner must:

1. Re-read saved settings and compare the preview's source before staging anything.
   Verify the chosen date is still in the future, duration, existing role and its
   current requirements. Publish only the specifically chosen fictional event;
   the two-event preview is not authorization to publish or contact broadly.
2. Stage the chosen event's ordinary exact `confirm_record` review. Exclude preview
   metadata (`role_slots` and reminder/admin window fields) from the Event record.
   After approval, use its actual applied event ID to stage and review the one
   `Shift` record. Never create a second event on a repeat invocation; retain the
   applied IDs and exact hashes in the private rehearsal receipt.
3. Stage a participant assignment through the existing planner review contract
   only after current eligibility passes. Event publication does not create an
   assignment or grant availability, qualifications or consent.
4. Run the intended bounded job against those actual IDs and the real clock. Review
   the exact Gloo-composed body and hash. Verify native delivery separately. Keep
   general scheduling paused until its own authorized activation and uptime check.

No one-step apply flag is offered. General record review, assignment eligibility,
Gloo failure holds, source hashes and native preflight remain authoritative.
The Google Voice acceptance workflow is not a fallback. Automated Google Voice
transport remains held; John may manually receive the laptop's SMS in his inbox.

## Participant prerequisites

| Participant | Required state before the corresponding demo step |
| --- | --- |
| Clyde, admin recipient | Completed saved church workspace; exact existing-roster admin enrollment review; actual current opt-in and no STOP; coordinator status; enabled bounded laptop session. Existing opt-in enrollment uses the operator-consent review contract and does not invent new consent. Coordinator status excludes him from volunteer replacement pools. |
| Noah, replacement volunteer | Actual current opt-in and no STOP; active, completed signup; matching interests and full-event availability; current admin-verified qualifications required by the actual role; frequency, overlap and care guards; enabled bounded session. An invitation or existing Messages thread alone proves none of these. |

The runtime owner separately verified one-to-one iMessage routes for Clyde and
Noah through the intended church sender. Only John was active in that snapshot.
This route evidence does not enroll or activate either additional participant.
Use private records for identities/phones; do not put them in public fixtures.

## Verified features and explicit holds

| Area | Source behavior and evidence |
| --- | --- |
| Three-hour admin updates | Actual scheduled events and current staffing; durable event/admin dedup; missing plan reported accurately; quiet-hours deferral; Gloo outage holds; changed facts require a new exact review. Existing real-Gloo admin replay remains mock-delivery evidence only. |
| Per-event reminders | Actual approved/confirmed placement, saved timezone and local day-before date; distinct source-bound reminder per assignment; current recipient/eligibility and exact literal Gloo composition; cancellation or changed event invalidates the source. |
| Cancellation | Existing setup regression suite verifies sole-booking cancellation, held ambiguous scope, current booking-source checks, internal sensitive care and fill transitions. Algorithm/outreach and native delivery are separately owned and are not certified here. |
| Monthly collection/followup | Owner/month/recipient scope review remains required. Followup requires an initial submitted message and a three-day wait; queued/uncertain asks are insufficient. Current conversation policy suppresses collection asks and followups before Gloo. This is an intentional hold, not an active text campaign. |
| Proactive planning | Connected jobs report `held_for_authorized_parent_approval`; mock legacy jobs model midmonth collection/planning and weekly capacity scanning. Eight-week capacity flags remain internal. This is not connected autonomous planning readiness. |
| Seasonal events | Existing Christmas Eve recipe/fixture passes. Broader Easter, summer/VBS and adaptive seasonal forecasts remain partial; preview does not invent them. |

John's additional requirement is **paired local service dates**: Greeting first
service twice monthly, Production second service on those same chosen days,
childcare for the monthly women's event, and no December dates. The audited
planner enforces per-role monthly caps and mapped recurring windows, but has no
`same_day` paired-assignment contract or joint choice validator. Ordinary independent
Greeter and Production proposals can select different Sundays. Explicit unavailable
dates are supported; a whole-month verbal exclusion needs a validated mapping.
The intake agent's pending constraint must stay held rather than be flattened
into independent windows. Paired scheduling needs a separately reviewed contract
shared by draft/repair, record approval, replacement eligibility and revalidation.
Keeping text in a pending draft is not evidence that this planning feature works.

## Focused verification

```sh
python -m pytest -q tests/test_saved_schedule_preview.py tests/test_admin_text_settings.py \
  tests/test_pre_event_updates.py tests/test_exact_day_before_reminder.py \
  tests/test_notification_status.py tests/test_planning_workflows_api.py \
  tests/test_planning_composition.py tests/test_capacity_role_caps.py \
  tests/test_seed.py tests/test_setup_regressions.py
```

All 228 focused tests passed on October 6, 2026, without network, real Gloo or native
delivery. The new cross-feature fixture uses the same saved 9AM/11AM settings to
build the two actual in-memory events, stage two assignment-bound exact reminder
reviews and exercise two deduplicated mocked admin updates across a session reload.
It does not authorize test-policy settings or fictional participant facts in a
connected runtime. No feature-universe status was upgraded from these fixtures.
All four `web/texty/tests/admin-readiness-ui.test.js` checks also passed, including
the direct Schedule action and escaped next-step text for a missing event schedule.
