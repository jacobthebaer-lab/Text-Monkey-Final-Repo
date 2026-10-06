# Preference and staffing connector activation

The profile publisher, staffing worker and held preference review are separate
connectors. Successful signup or Supabase identity publication does not start
staffing. Select one authoritative scheduling store before connecting them.
This runbook makes no native write, migration, polling or recipient authorization.

## What the source implements

| Universe feature | Supported path | Current boundary |
| --- | --- | --- |
| `pco-writeback` | Same-transaction confirmed/cancelled assignment intents; separate durable worker; cancellation-dependent replacement | Existing mapped person, reviewed plan/position/time, consent and local eligibility required |
| `pco-notifications` | Explicit false/null notification preparation fields plus fresh identity/status/readback | Other native account automations require separate actual notification evidence |
| `pco-position-mappings` | Verified identity mapping and exact role/plan/team/position/service time | Plan-wide teams and one service time only; singular/plural service associations validated |
| `pco-coverage` | Confirmed local assignment with verified native C and no unresolved latest intent | U stays reserved; live open-needs arithmetic remains an acceptance check |
| `pco-preferences` | Supabase publication of validated interests, role caps, recurring windows and dates; native GET comparison and signed held review | Native frequency release remains disabled; membership, qualifications and permissions are separate |
| `pco-future` | Unsupported split/per-time mapping explicitly held | No verified per-time write contract; do not substitute whole-plan C |

## Supabase preferences

Use `tools/publish_profiles.py` with the existing approved phone/project scope
and intended target env file. Its `--identity-only` mode intentionally leaves
preferences pending. Once the actual profile is complete and full publication is
authorized, use the explicit publisher without that option. No Gloo replay or
synthetic receipt is needed. Verify the latest same-source outbox revision and
target readback; a cloud ID alone establishes neither complete intake nor serving
eligibility. Held revisions need their real validation/conflict resolved rather
than being reset to pending.

Map each local role name to exactly one existing cloud role, using the private
scope's `role_map` where names differ. The publisher preserves independent cloud
fields and qualifications. It remaps role-cap IDs and recurring-window roles;
event-relative windows also require exactly resolved cloud event types. Do not
invent an event context or broaden qualifications to finish publication.

The publisher mirrors approved profile fields and availability, **not events or
assignments**. Running it alongside a Mac scheduler does not mirror that
scheduler's commitments into Supabase. For Supabase-authoritative scheduling,
run the existing backend/worker against the reviewed private scheduling store,
apply its reviewed additive migrations separately, and install mappings in that
same store. For Mac-authoritative scheduling, install the event and mappings in
the Mac store; cloud assignment mirroring would require a separately designed,
explicit cross-store event/shift contract. Never copy local numeric IDs across
stores or assume two workers own the same assignment.

## Minimal staffing setup in the authoritative store

1. Select the private database and scoped credential organization/service types.
   Check compatible staffing schema and actual backend permissions. Existing
   SQLite startup supplies the staffing tables; PostgreSQL uses the reviewed
   migration in the staffing runbook. Do not automatically migrate a live store.
2. Import only the selected existing native plan, using saved admin schedule and
   staffing settings. Verify canonical UTC starts/ends and event/shift links.
   A roster with no event cannot produce an event assignment.
3. Verify the existing Services Person ID for the actual volunteer, then call
   `map_volunteer`. Phone/name similarity, another demo profile and Supabase's
   independently allocated volunteer ID cannot select the native Person.
4. For each supported interested role, explicitly review the imported shift,
   native team/position/PlanTime and existing canonical local role using the
   [role-binding workflow](PLANNING_CENTER_ROLE_BINDINGS.md). Import initially
   creates separate namespaced roles. A reviewed binding connects every slot of
   that exact native need to the existing local role and survives later imports.
   Preserve required qualifications; interest and native membership are not
   qualification evidence. Legacy `map_position` does not establish this
   canonical role review.
5. With write/poll flags off, explicitly inspect current staffing using
   `refresh_staffing`, review conflicts and save exact mappings. This is a local
   reconciliation, so it needs its own authorized application step even though
   native requests are GET-only. Establish current native notification settings
   and separately scoped no-notification acceptance evidence.
6. Only the released runtime owner enables `PCO_STAFFING_WRITE_ENABLED` and/or
   `PCO_STAFFING_POLL_ENABLED`. The existing separate PCO job consumes these flags;
   it does not activate general scheduling or messaging. A genuine future
   confirmed/cancelled transition is captured automatically. Named existing
   confirmations use bounded `catch_up_assignments`, never broad backfill.
7. Verify native identity, status, silent fields, actual notification behavior,
   open needs and restart/idempotency. Unknown outcomes retain their barrier and
   require read-only reconciliation; never replay a create to force convergence.

## Native preferences are a separate release

The current authenticated held-preview route supports a reviewed file-backed
SQLite store, not PostgreSQL. Use it only after the reviewed feature schema,
private bindings/key, allowlists and committed receipt source are present in the
chosen SQLite store. Equal frequency is a no-op. A difference needs verified scoped
ownership, an execution-eligible source, exact authenticated execution review,
native notification evidence and coordinated native edits. The existing
`reviewed_held` receipt is not that authority, and
`authorize_frequency_execution` deliberately refuses release. Add an execution
adapter only when those actual evidence contracts are concrete and reviewed.

General role hours, global frequency limits, event-relative windows and blockouts
have different native contracts. Keep unsupported operations visibly held; do not
convert them to membership permission changes or claim complete availability sync.

See [staffing implementation](PLANNING_CENTER_STAFFING.md),
[held runtime](PLANNING_CENTER_HELD_RUNTIME.md),
[frequency executor](PLANNING_CENTER_FREQUENCY_EXECUTOR.md), and
[historical staffing contract](PLANNING_CENTER_STAFFING_CONTRACT.md).
