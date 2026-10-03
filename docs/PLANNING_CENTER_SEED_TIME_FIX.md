# Seed-time defect handoff to cloud owner

The live demo source is repaired and verified; this document proposes the narrow
code change without competing with the cloud implementation owner.

`tools/planning_center_demo.py` currently passes `start.isoformat()` with a Denver
offset on create/onboarding patch. Its named-time branch then skips an existing
`Synthetic 9 AM service` regardless of its timestamps. Final verification checks
only that a service time exists, not that the saved instant equals the intended
start/end. Thus an incorrect named time persists across seed reruns.

The initial source returned 09:00Z despite the intended 09:00 Denver setup. Exact
causality of that original write is not proven. We did prove that canonical
15:00Z–16:00Z PATCH saves the intended 09:00–10:00Denver values on both existing
plans. This was a source setup/validation defect; the importer preserves the
actual API UTC timestamps correctly.

Minimal seed fix:

1. Convert aware local start/end to UTC and serialize ISO8601 with `Z` for all
   PlanTime POST/PATCH bodies. Do not replace tzinfo on a local value to pretend
   it is UTC; use `astimezone(timezone.utc)`.
2. Resolve exactly one target service time: unique named time, otherwise unique
   onboarding service time matching the intended instant, otherwise create it.
   Reject ambiguous duplicate named times rather than silently selecting one.
3. Compare the chosen time to both intended instants, name and empty reminder
   settings. Repair mismatches on rerun with PATCH; do not create another time.
4. Fresh GET the exact chosen ID and assert saved start/end equality using `_time`;
   verify service type, empty team reminders, private plan/reminders disabled.
   Wrong readback raises sanitized PlanningCenterError rather than success.
5. Preserve existing organization allowlist and no-scheduled-people boundary.
   Seed must not reset an occupied plan's time merely because its name matches.

Meaningful regression cases:

- October Denver 09:00 serializes 15:00Z, January 09:00 serializes 16:00Z, verifying
  both sides of daylight saving time with ZoneInfo rather than fixed offsets.
- Named synthetic time saved 09:00Z gets patched to 15:00Z with the same ID;
  repeat seed after correct readback produces no new PlanTime or duplicate need.
- Fresh API returns a different instant after write: seed raises, not success.
- Wrong end time is caught independently of start; duplicate named times hold.
- An occupied synthetic plan holds rather than mutating time or reminders.

Test fixtures should inspect canonical payload and emulate server normalization;
do not mirror the helper implementation as the sole assertion. No further live
mutations or people are needed to test these cases. Acceptance should read the
real repaired PlanTimes and assert the intended instant, without moving them again.

Current exact source/mapping:

| Plan | PlanTime | Local event | UTC start/end | Denver |
| --- | --- | --- | --- | --- |
| 92466235 | 229038886 | 1 | October 4 15:00–16:00Z | October 4 09:00–10:00 |
| 92466244 | 229038904 | 2 | October 11 15:00–16:00Z | October 11 09:00–10:00 |

Actual evidence: `evidence/planning-center/corrected-service-times.json`.
Historical defect evidence remains in `current-service-times.json`.
