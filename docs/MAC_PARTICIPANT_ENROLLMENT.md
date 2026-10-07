# Explicit additive Mac participant enrollment

The signed version 2 enrollment revision adds one named participant to an
already adopted natural Mac session. It preserves each existing session ID,
start time, original reply proof, connector cursor, claims and delivery receipts.
The original version 1 journal remains valid and is retained as signed lineage.
Enrollment creates no volunteer, changes no consent and sends no message.
Native delivery still requires the existing explicit live worker and application
consent, review, Gloo, quiet-hours, eligibility and exact-body guards.

An operator must separately approve the specific named phone. Stop the connector
and read its current checkpoint bytes and active-claim list. The original
approved reply must have been received. An unresolved claim, attempted/uncertain
delivery, changed route or stale checkpoint holds the revision.

The source helper is pure and returns a candidate configuration:

```python
from app.integrations.mac_ongoing import enroll, adopt

updated = enroll(
    current_config, exact_checkpoint_bytes,
    phone=explicitly_approved_phone, name=explicitly_approved_name,
    actor=operator_identity, operator_confirmed=True,
    active=actual_active_claim_list, now=actual_transition_time,
)
candidate_state, journal = adopt(
    updated, parsed_current_checkpoint, exact_checkpoint_bytes,
    actual_active_claim_list, actual_transition_time,
)
```

Both calls are safe dry runs: neither writes a file, reads Messages, contacts a
backend nor invokes native delivery. The operator applies the selected config
and matching backend Mac settings only through the separately authorized
deployment process. Never print the connector token or private journal.

Stamp `actual_transition_time` at the real stopped-connector apply operation,
with a timezone-aware clock, rather than during earlier preparation. A fresh
participant receives a fresh random session ID with `starts_at`, `enrolled_at`
and `ongoing_since` equal to that approval instant, null expiry and explicit
`until_stopped`. There is no invented original expiry or prior inbound GUID.
The worker can adopt the matching revision from the preserved checkpoint. Its
restart is idempotent and never resets the cursor. The newly enrolled phone's
natural reader admits only rows after that checkpoint and at or after enrollment,
excluding old history and late-imported old messages, including historical STOP.
New STOP remains effective. Existing participants retain their original intake
history and the original approved target exception on that target's phone only.

Version 2 binds the entire candidate connector configuration by hash, as well
as the exact prior checkpoint bytes. After enrollment, connector configuration
changes require a separately reviewed source-supported transition; editing its
route, phones, session IDs or decoder silently cannot adopt this authorization.
To add another participant, apply a new explicit additive revision against the
then-current adopted journal and settled checkpoint. Existing participants
cannot be removed, renamed by changing session authority, or renewed through
this helper. Until-stopped authority is not SMS consent and does not override
STOP, an inactive profile, care holds or the ordinary native send preflight.

Verification walks the signed lineage iteratively and checks every revision
against its verified predecessor. It does not impose a 32-enrollment counter.
A synthetic 100-participant regression checks unchanged existing sessions,
cursor and delivery ledger, plus rejection of a tampered inner signature.
This verifies source behavior only, not native delivery or a live roster import.

No Google Voice automation, paid provisioning, public-send defaults or live
activation is enabled by this source support. Synthetic tests establish source
and ledger behavior, not actual native delivery.
