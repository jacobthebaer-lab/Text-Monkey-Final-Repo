# Planning Center availability preview

`app.integrations.planning_center_availability` builds a provenance-bound diff
and a transaction-local preview outbox. It does not execute that diff, send
notifications, start a worker, import itself into application startup, create
tables, change the live database, or modify staffing and consent. Native reads
use the existing paginated Services client and issue GET requests only.

## Inputs and lifecycle

1. Supply the existing organization/service scope and verified local-volunteer
   to Services-person mapping. Never infer a Services identity from another
   application's person ID. `capture_source` requires the committed source ID,
   receipt ID and revision, an explicit IANA timezone, and an aware clock.
2. Capture saved availability through the shared profile validators. The frozen
   JSON snapshot contains unavailable dates, role frequency limits and recurring
   windows, with provenance and scope. It omits phone numbers, names, messages,
   consent, qualifications and privileges. Dates must be canonical ISO local
   dates within the existing 366-day horizon.
3. Supply reviewed role/service/team/position/membership bindings. `read_remote`
   verifies organization, person, nonarchived team, service type, position name
   and exact membership person/position. Read all blockouts and their generated
   dates, without a future filter. Ambiguous duplicate blockouts or role mappings
   are rejected.
   Team scope accepts the documented singular `service_type` or plural
   `service_types` relationship, including a null singular value with a matching
   plural association. Malformed, duplicate, missing or contradictory scope is
   held. A shared team does not expand the configured service allowlist or
   replace the exact service-scoped position and membership checks.
4. `build_preview` captures the source and complete remote hashes, reviewed
   ownership baselines, policy evidence, candidate operations and holds. A
   `FrozenSnapshot` exposes copies, so mutating the caller's dictionary cannot
   change the snapshot. This is tamper detection within the application, not a
   cryptographic signature or proof that a receipt was legitimately issued;
   the caller must verify receipt provenance before capture.
5. Immediately before saving or consuming a preview, recapture the committed
   source and reread native state. `verify_current` rejects changed source,
   changed native state and modified operations by rebuilding the diff.
6. `enqueue_preview` flushes preview/intent rows into the caller's transaction;
   the caller owns commit/rollback and explicitly initializes these tables in
   an isolated test database or a separately reviewed migration. No schema or
   runtime wiring is supplied by this module.

Example call sequence, using caller-provided verified inputs:

```python
source = capture_source(session, config, volunteer_id=volunteer_id,
    provenance=verified_receipt, tz=timezone_name, now=now)
remote = read_remote(client, config, source, verified_bindings)
preview = build_preview(source, remote, owned=verified_ownership)
# Review preview.value; default policy keeps candidate mutations held.
current_source = capture_source(session, config, volunteer_id=volunteer_id,
    provenance=current_verified_receipt, tz=timezone_name, now=now)
current_remote = read_remote(client, config, current_source, verified_bindings)
enqueue_preview(session, preview, source=current_source,
    remote=current_remote, now=now)
```

## Supported projection and preservation

Consecutive unavailable local dates become one candidate `Blockout` with
`starts_at`, `ends_at`, `reason`, `repeat_frequency: no_repeat` and `share: false`.
The proposed interval uses local midnight through 23:59:59 on the last
intended local day, converted to UTC with DST. The native API includes this
final second. Incoming finite ranges starting at local midnight and ending at that exact
local boundary, with an explicit native timezone, normalize to an exclusive next-midnight
cache interval so the last second is blocked and the next day remains clear.
Timed, recurring and unknown-timezone ranges retain their existing endpoints.
`time_zone`, `group_identifier` and `all_day` are not
submitted as writable API fields. Unowned native exclusions, including the
union of generated recurrence dates, can satisfy a desired exclusion without
being adopted. An unresolved recurrence holds a possible duplicate creation.

Only an explicit verified ownership record permits a proposed PATCH or DELETE
of an existing blockout. It contains the exact organization/person, logical
key, resource ID and complete last verified native hash. A native edit or
missing owned blockout yields a conflict. Absence of a saved date field is not
an authoritative clear. Unowned reasons, blockouts and preferences remain
untouched. Removing a role frequency limit requires a reviewed restore, never
deleting the membership or inventing an `Every week` reset.

Role limits of one, two and three times per month map to the corresponding
official `PersonTeamPositionAssignment.schedule_preference` labels on that
role's verified membership only. A different unowned preference is held; an
equal preference is a no-op. Native frequency is a preference, not a guaranteed
hard maximum. Other limits, global limits, clock hours and group/event-follow
context remain held for local application enforcement. They are never broadened
to another role, written as a person-wide limit, or converted to global weekly
blockouts. No permissions, qualification fields, emails, plan assignments or
notification preparation fields are written.

## Review and retry boundaries

Intent states are `preview`, `held`, `noop`, `conflict` or `superseded`, never
runnable. Exact replay uses the same deterministic preview/intent keys and does
not add rows. A changed revision supersedes earlier untouched preview/held
intents for the same scoped resource. A prior `unknown` outcome for the person
holds every new intent, including a changed date range, until reconciliation.
The unique keys also reject concurrent duplicate inserts; callers must roll
back and reload after a uniqueness race rather than inventing a new key.

Default policy holds all candidate mutations. Any review evidence must have a
SHA-256 receipt hash and match the complete current native snapshot. Separate
blockout and membership notification checks are required. `share: false`
controls sharing and does not establish notification suppression. Native
blockouts can notify leaders or affect pending requests. The API does not expose
a documented suppression flag for these operations. An unavailable policy
proof must remain a hold.

Date policy evidence must identify `inclusive_local_end_second` and verify
the actual generated `BlockoutDate` span. Evidence for the previous
`exclusive_local_midnight` contract remains held, and previously reviewed
operation bodies fail reconstruction rather than being silently rewritten.
Tests cover single and multiple days, both DST transitions and adjacent-day
eligibility; they do not establish notification acceptance.
Setting policy flags only advances a candidate to `preview`; it never authorizes
or performs a write. A future executor needs its own review/authorization,
fresh scope/source/native checks, explicit notification handling and actual
readback. When a range moves, verify replacement coverage before removing old
owned coverage. Never blindly POST again after a timeout, and do not use this
outbox as evidence that live availability sync is active.

## API contract and verification

The fixtures use synthetic IDs and JSON:API shapes from official Services
2018-11-01 documentation:

- [Blockout](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/blockout)
- [Generated BlockoutDate](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/blockout_date)
- [PersonTeamPositionAssignment](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/person_team_position_assignment)
- [Native blockout behavior](https://help.planningcenter.com/en/142872-manage-blockout-dates.html)
- [Native leader notification behavior](https://www.planningcenter.com/blog/blockouts-bonanza)

```sh
python -m pytest tests/test_planning_center_availability.py \
  tests/test_planning_center.py tests/test_planning_center_staffing.py \
  tests/test_event_role_availability.py
```

Tests cover strict GET-only reads, local and native changes, owned/unowned diffs,
exact role scope, duplicate mappings, immutable source provenance, stale policy,
DST, unsupported hours/group/global limits, replay, supersession, uncertain
outcomes and rollback. Synthetic preview success establishes no real delivery
or live remote mutation.
