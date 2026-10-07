# Automatic finite Planning Center blockouts

This feature requires the saved-source caller integration and the blockout
executor together. It uses existing `Policy` rows, without a migration or
changes to old held previews/frequency reviews. New Settings are
`PCO_BLOCKOUT_WRITE_ENABLED` (default false), `PCO_BLOCKOUT_SIGNING_KEY_PATH`
and `PCO_BLOCKOUT_ACCEPTANCE_PATH`. Private keys/configuration are never stored
in Git. The independent scheduler runs every 60 seconds with one instance and
coalescing; local saves queue their revision in the same transaction and never
wait for Planning Center or a cloud profile publisher.

A root-reviewed, signed acceptance descriptor establishes the exact API/date,
notification and booking-conflict contract for the configured organization,
service allowlist and timezone. The root installation helper signs verified
private evidence after independent acceptance, not an arbitrary UI assertion.
The descriptor has an explicit expiry (default 90 days, at most 365 days).
Missing, changed, future or expired acceptance holds future effects.

The authenticated coordinator enables a standing **per-person** policy. The
server binds the actual local volunteer, mapped Services person, mapping
creation identity, phone hash and acceptance evidence. Every new write
rechecks that identity, current consent, committed source, queued revision,
policy, signed journals and fresh native state. A coordinator can disable even
when the mapping, signing key or acceptance is missing. Disable preserves the
native ownership and unresolved-attempt history.

## Supported effects and notifications

Only saved, authoritative, finite global unavailable dates become person-wide
blockouts. They start at local midnight and end at the intended final local
23:59:59, including DST conversion. Generated UTC intervals must cover exactly
that range in the configured timezone. Role frequency, role-specific hours,
partial days and recurring local windows never become global exclusions.

The effect applies to all of that person's Services teams. Notifications are
**provider managed**, according to existing Planning Center leader preferences;
the executor does not claim silence or alter those preferences. The
[Blockout API](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/blockout)
has no documented notification suppression field. It does not cancel/decline
bookings: native person booking identities/status/relationships are read before
and after a blockout mutation. Changes remain an unresolved side-effect hold;
the existing cancellation and replacement flow owns declines/refills.

## Ownership, retry and upgrade

POST establishes ownership only after its exact successful provider-returned
ID is durably recorded and fresh native/generated coverage verifies that ID
against the captured before-state. Identical dates/reason, a new collection ID
or a creation timestamp alone never establish ownership. PATCH and DELETE
require the journal-owned ID and full unchanged native baseline. Unrelated
coordinator exclusions may satisfy availability but are never adopted, changed
or deleted. New range coverage is created before deleting replaced owned ranges.

A signed immutable attempt and person mutex commit **before** mutation HTTP.
Any error once mutation request begins, or process death after the claim,
requires GET-only recovery. A POST with no durably captured provider-returned
ID remains unknown, even if an identical blockout appears; it is never adopted
or resent. A successful POST response followed by failed GET can recover using
its saved ID. PATCH/DELETE recover against their already-owned exact IDs. Recovery
can establish a verified owned outcome after source edits, opt-out or disable,
then stop new effects until current authorization/source validates. The claiming
stack may explicitly record an aborted-before-HTTP attempt when it knows it
never called the mutation API; its history is retained and a later fresh claim
may proceed. No SQLite/Postgres write transaction survives network calls.

Fresh reads reduce native edit races, but the API exposes no documented atomic
conditional-write contract. An unrelated native change or changed booking
snapshot prevents convergence. No unsupported atomicity guarantee is made.

Prior Text Monkey blockouts require the separate root-only
`bootstrap_owned_blockout` helper with an independently verified original-write
receipt adapter, exact scoped ID/body/full readback hash and fresh generated
coverage. A GET or UI-supplied native ID is insufficient. The HTTP API has no
adoption endpoint. Existing Noah records must remain untouched until that exact
bootstrap is independently reviewed and root executes it.

## Caller contract

`queue_blockout_sync(session, org, volunteer_id, revision=...)` is transaction
local, idempotent and network free. Source/queue keys are respectively
`pco_bs:`/`pco_bq:` plus SHA-256 of JSON `[org, volunteer_id]`.

`sync_person_blockouts(factory, client, config, volunteer_id, source_reader=...,
clock=..., enabled=False, signing_key=None, acceptance=None,
reconcile_only=False, limit=10)` uses a dedicated factory. The reader must
validate the committed saved-source receipt and return a fresh `FrozenSnapshot`
without mutations/network, using `CommittedSourceSession` via the caller
source factory. Mutating or already-flushed source work is rejected. The caller loads the installed acceptance each tick.
Existing unknown claims can reconcile when new effects are disabled.

The authenticated enable PUT invokes
`enqueue_current_availability(session, settings, config, volunteer_id=..., user=...,
clock=...)` before commit. It records honest new coordinator provenance for
existing saved facts; a source hold rolls back policy enable atomically.

## HTTP contract

GET `/api/planning-center/blockouts/{volunteer_id}` and PUT the same path plus
`/policy` require existing verified coordinator authentication. PUT accepts only
`{"enabled": true|false}`. Paths, native IDs and proof hashes are server resolved.
GET/PUT return:

- `organization_id`, `volunteer_id`, nullable `person_id`.
- `policy_enabled`: saved standing choice, separate from runtime readiness.
- `runtime_enabled`: backend setting; `state`: pending, verified, held, unknown,
  or disabled; `reason`: fixed code, never a provider body or private source.
- Nullable `desired_revision`, `verified_revision`, `owned_count` (null if history
  cannot be authenticated); boolean `unknown_attempt`.
- Nullable `notification_mode` and `conflict_strategy`; `effect_scope` is
  `all_services_teams`; `global_unavailable_dates` contains saved global dates.
- `acceptance.available`; when true, evidence hash, verified/expiry timestamps,
  notification/conflict/date contracts and timezone.
- `readiness`: signing, acceptance, mapping, consent, authority and journal
  readiness. A saved enabled choice with stale setup shows held, not Off.

Common holds include `blockout_signing_key_required`,
`blockout_scoped_policy_required`, `blockout_scoped_policy_disabled`,
`blockout_server_acceptance_required`, `blockout_acceptance_expired_or_future`,
`blockout_policy_acceptance_changed`, `blockout_policy_identity_changed`,
`blockout_current_consent_or_constraints_held`,
`blockout_owned_native_baseline_changed` and caller `blockout_source_*` codes.
Unknown readbacks use `post_identity_unknown`,
`post_returned_identity_not_verified`, `patch_not_verified`,
`delete_not_verified`, `native_bookings_changed`,
`unrelated_native_blockouts_changed`, `native_generated_dates_not_verified`.
A disabled state can retain an unknown attempt requiring later GET recovery.

Source tests are synthetic. Root owns actual native fixture acceptance,
installation, reviewed ownership upgrade and deployment; source tests alone
never establish those live results.
