# Event-follow availability and role-scoped frequency

Base: `b4b0b6953471baf505f690a0f7558ac90647ea2b`.
Worktree: `/Users/jacob/.codex/worktrees/event-relative-role-caps/Text Monkey`.
Branch: `codex/event-relative-role-caps`; local commit recorded in final receipt.

Owned files are `app/core/recurring_availability.py`,
`app/core/eligibility.py`, `tests/test_event_role_availability.py` and this
contract. No onboarding, signup, profile_sync, ranking, scheduler or live data
was edited. No actual conversations or credentials are included.

## Window schema

All existing windows remain valid and unchanged. Optional `time_mode` accepts
`clock` (legacy default) or `event`. Event mode is only for an explicit willingness
to follow a named group's actual schedule. It requires a named role, any_role=false,
named event_context, all_day=false and null start/end times. It does not supply
all-day availability. A matching known role ID, mapped event-type ID and weekday
are required. Empty unknown IDs remain stored but ineligible. An event-type ID
must come from the genuine catalogue/mapping, never an invented live fixture.
The matching actual event supplies its interval, including a DST change.
Ordinary unknown clock hours remain held under the prior fix.

Example fictional normalized coffee window:

```json
{"weekday":2,"role_ids":[2],"role_label":"Coffee","any_role":false,
 "start_time":null,"end_time":null,"all_day":false,"time_mode":"event",
 "event_context":{"label":"men's group","event_type_ids":[]}}
```

This example is held until a verified group mapping exists. Prior Sunday
08:00–10:00 greeting remains its own clock window. Keep explicit December date
exclusions in the existing date snapshot; they override matching windows.

## Role frequency schema and API

New optional `role_frequency_caps` in the sender's draft and final preferences:

```json
[{"role_id":1,"role_name":"Greeter","max_per_month":2}]
```

IDs and names must match the real catalogue, cap must be an integer 1..8 and
each role may appear once. `normalize_role_frequency_caps(caps, roles)` validates
these facts. `merge_role_frequency_caps(data, previous, roles)` preserves caps
when omitted and validates a supplied complete corrected snapshot. Frequency
and unrelated followups preserve event mode, other windows and exclusions.

Shared eligibility checks that role's proposed/approved/confirmed/completed
assignments in the event's church-local calendar month. Cancelled assignments
and other roles do not consume the cap. Existing assignment exclusion is honored
when validating the assignment itself. Qualifications, signup completion, role
pauses and date exclusions remain independent. No cap supplies global frequency.

`WINDOW_SCHEMA_INSTRUCTIONS` includes both additions for the editor's existing
real Gloo interpreter. Preserve both fields in availability context, partial
snapshot and final preferences. A stated event-follow mode supplies a time
relationship; do not repeatedly ask for fabricated numeric hours. A role cap
supplies frequency only for that role. Keep global frequency unknown/null when
not separately supplied, rather than copying Greeting's cap to Coffee. Unknown
group mapping remains a hold even after other signup facts are complete.

## Required consumer integration before live replay

`global_frequency_limit(preferences, legacy_default=3)` returns the supplied
global cap, the old default for legacy profiles, or None for new role-cap
profiles with no stated global cap. It rejects malformed limits. Ranking and
all planning consumers must use it and compare their all-role count only when
the result is not None. Do not invent a large numeric sentinel. Those files
belong to other owners and were not changed here.

Current canonical consumer sites are ranking.py's candidate count and
scheduler.py's candidates, propose, validate and preview_problem checks. The
plan-preview owner must also check role-specific counts plus same-role proposed
choices; live assignment writes/reviews recheck shared eligibility. A nullable
new-schema global limit is not safe for the old direct numeric comparisons.

The editor and live owner must integrate these consumers and the Gloo schema
before interpreting the preserved actual inbound once. Existing erroneous
global-cap extraction is not a new user-provided global preference.

## Checks

70 availability/eligibility tests and 86 fill/review tests passed, **156 focused
tests total**. Checks include mixed-role normalized shape, prior Sunday range,
all 31 December exclusions, unmapped group hold, context/role/day separation,
DST event-follow versus unknown clock hours, Coffee not consuming Greeting's
cap, third Greeting blocked, self-exclusion, cancelled bookings, local-month
and year boundaries, malformed modes/caps and legacy global-cap compatibility.
`git diff --check` passed. No broad suite rerun, real Gloo/API call, transport,
live DB, Planning Center, Supabase, Docker or Noah activity occurred.
