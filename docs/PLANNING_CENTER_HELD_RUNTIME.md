# Authenticated held Planning Center preview

This runtime batch exposes an explicit coordinator action, not an executor. It
uses committed same-store Mac/profile receipts and two fresh GET-only Planning
Center snapshots, then recaptures the source under the final local transaction
before committing a deterministic preview and intents. It never calls Gloo,
changes a profile, creates ownership, sends a message, modifies Planning Center,
extends a text session or starts a worker. Gloo's existing interpreted receipt
is validated rather than reinterpreting the original conversation.

## Owner configuration

`PCO_REVIEW_ENABLED` defaults to false. Enable only after the sole runtime owner
has reviewed/applied the six-table migration to the explicitly selected private
SQLite target. Every action checks complete compatible source/feature schema
through the read-only preflight; missing tables/indexes hold the action. Startup
creates only the nine pre-existing PCO tables, even if optional feature models
were imported by another app instance. No feature migration runs at startup.
The PostgreSQL path remains held for separate runtime integration review.

Supply two absolute paths, without committing their private contents:

- `PCO_REVIEW_BINDINGS_PATH`: backend-user-owned regular mode600 JSON file for
  exactly one approved volunteer/person and organization. The PCO mapping owner
  must verify the real Services identity and every local-role/service/team/
  position/membership association. These are explicit operator-selected mappings;
  matching names alone are insufficient. Local role identities and fresh native
  relationships are checked on each capture. Wrong scope, public/unreadable files,
  ambiguous bindings or a file changed during capture hold the action.
- `PCO_REVIEW_SIGNING_KEY_PATH`: backend-user-owned regular mode600 raw key file,
  at least 32 bytes, provisioned/protected by the runtime owner and stable across
  restarts. It is loaded into private app state only; no key generation, logging,
  publication or fallback occurs. Missing/invalid keys permit the held preview
  but refuse review acknowledgement. Restart after an authorized key rotation;
  old signatures then fail verification.

The existing PCO credential and explicit organization/service scope are reused.
The current profile and Mac recipient allowlists must both approve the volunteer.
Leave general automation, staffing writes/polling and all transport holds as
currently configured; the review flag does not enable them.

Synthetic binding-file shape (replace every mapping with verified owner facts):

```json
{
  "schema": 1,
  "organization_id": "10",
  "volunteer_id": 1,
  "person_id": "70",
  "memberships": [{
    "role_id": 1, "role_name": "Greeter", "service_type_id": "20",
    "team_id": "30", "position_id": "40", "position_name": "Greeter",
    "membership_id": "80"
  }]
}
```

Configuration does not manufacture write ownership or silence evidence. This
batch supplies no ownership to the preview and uses default held policy. An
already equal native frequency is a no-op; a different unowned preference stays
held. Unsupported role hours/global limits/blockouts remain explicit holds. A
missing role binding may produce a preview with top-level holds and no frequency
intent; it cannot be acknowledged as a fabricated mapped operation.

## Console/API contract

All routes use existing Supabase bearer authentication, confirmed-email
coordinator allowlisting and the additional nonlocal bridge check. The console
uses its existing authenticated session. An unauthenticated or foreign actor
cannot read/capture/acknowledge a proposal. No principal comes from request JSON.

1. `POST /api/planning-center/held-previews` accepts only
   `{"volunteer_id":1}`. The server selects the latest same-source profile row;
   callers cannot choose receipts, revisions, native payloads, ownership or flags.
   A changed/stale/foreign source or unmatched actual receipt holds before capture.
   Success returns `preview_id`, organization/person, source/remote hashes,
   `native_snapshot_saved_at`, operations with deterministic `intent_key`,
   top-level unsupported-fact `holds`, mandatory `release_holds`, and
   `execution_enabled:false`. Operation states remain held/conflict/noop.
2. `GET /api/planning-center/frequency-reviews/{intent_key}` returns the exact
   four review hashes, membership binding, operation and
   `native_snapshot:{schedule_preference,saved_at}`. The value is the **saved GET
   snapshot**, not a fresh read on this GET or perpetual live convergence. For a
   PATCH, the desired value is `operation.body.data.attributes.schedule_preference`.
   For a no-op, body is null and the saved native value is already equal.
3. `POST` to that same review URL accepts only `preview_hash`, `source_hash`,
   `remote_hash`, `operation_hash`. A valid server key signs an exact ten-minute
   durable `reviewed_held` receipt. The response supplies receipt ID/hash, expiry
   and all mandatory holds, with `execution_enabled:false`.

No execute endpoint exists. The durable execution authority still refuses every
release because notification silence, native-edit coordination and fresh native
execution preflight are unverified. Mounting these routes or signing a review
cannot bypass those holds. Remote edits after the final GET are not an atomic
compare-and-set guarantee; this preview reports a saved observation only.

The UI module `planning-center-review.js` is allowed through the existing public
static serving paths; the separate UI owner supplies its contents. Static asset
availability alone grants no API or native authority. Missing/incompatible schema
returns503; source/configuration/freshness failures return sanitized409 reasons.
No private phone, conversation, signing key or credential is returned.

```sh
python -m pytest tests/test_planning_center_held_runtime.py
```

Checks use temporary synthetic stores, mocked Supabase and fake native transport.
They prove the mounted auth/capture/review path, GET-only/no-message behavior,
no-op/held values, stale source/native/config rejection, missing schema/key holds,
and legacy startup preservation. They establish no actual mapping, delivery,
notification silence, native write or runtime activation.
