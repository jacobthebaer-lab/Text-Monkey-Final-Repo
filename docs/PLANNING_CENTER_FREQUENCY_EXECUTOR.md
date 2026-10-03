# Reviewed role-frequency executor, future release

`planning_center_frequency_executor` is a separately imported, disabled-by-default
executor for one existing `PersonTeamPositionAssignment.schedule_preference`
PATCH from the immutable availability preview outbox. This batch supplies no
runtime hook, scheduler, migration, configuration switch, activation or live
execution. It does not change the consent/cancellation release. All verification
uses synthetic data and a mock HTTP transport.

## Exact scope

Only a `preview` operation with no holds, explicit verified ownership, a matching
current native baseline and a supported role limit of one, two or three times
per month may proceed. The existing organization/person/role/service/team/
position/membership mapping, JSON:API type, endpoint and body are rebuilt and
checked. The sole body attribute is `schedule_preference`, using the official
monthly preference label. It remains a preference, not an enforced serving cap.

Blockout operations are rejected unconditionally, including a preview whose
synthetic policy flags claim silence. Native leader notifications and boundary
acceptance remain unresolved. Role hours, group/event-follow context, global
limits and other unsupported facts remain held by the preview/local scheduling
code. No POST or DELETE, native event/plan changes, membership creation,
permissions, qualification fields, emails, invitations, SMS, or outgoing
algorithm changes are supported.

## Release preconditions

An integrating owner must complete independent review and explicitly approve
this future release before wiring or enabling it. That includes:

- Authenticate a durable review receipt for the exact intent, preview, source,
  remote snapshot, operation and organization/person. `FrequencyReview` stores
  their hashes and a receipt hash; constructing it is not authorization or proof
  of a real receipt. The caller must verify the receipt's origin and legitimacy.
- Establish current native membership notification behavior and bind its
  evidence hash to the reviewed remote snapshot. The official API offers no
  documented notification suppression flag for this PATCH. A boolean alone
  cannot establish that a live operation is silent. Without independently
  verified evidence, leave the preview held and executor disabled.
- Confirm actual person/role/member mappings and ownership from verified
  readbacks. Recapture the current corrected committed source, rather than
  passing a cached snapshot or stale receipt revision. Earlier private profile
  or synthetic proofs do not satisfy this precondition.
- Explicitly migrate the three new PCOBase tables only after review. Importing
  the module registers model metadata but does not create tables.
- Supply a dedicated session factory with no unrelated pending work or send
  observers, a read-only `source_reader(session)` that validates committed
  receipt provenance in the same transaction, and a current aware clock. A
  production source-reader must lock its receipt rows as well as the profile
  rows locked by the executor; external receipt stores need an equivalent
  coordination contract. Do not mutate local preferences during capture.
- Serialize native edits during an authorized application attempt. No documented
  atomic compare-and-set primitive is used for this API. Fresh preflight and
  readback detect observed conflicts but cannot eliminate another native editor
  changing the preference between GET and PATCH. The same limitation applies
  to local changes in the short gap after the final source recheck. Keep live
  activation held until the integrating owner accepts and coordinates these
  boundaries; do not claim transactional two-way consistency.

Reviews must expire within 15 minutes. Execution checks current time during
preflight, under the claim lock and immediately before the request. An expired
review cannot initiate a write. Exact original review identity still permits
read-only reconciliation or returning an already verified historical result
after expiry.

## Durable claim and reconciliation

`execute_frequency_intent(..., enabled=False)` returns `disabled` without any
database or API activity. When explicitly enabled, it reads the complete native
snapshot using GET, verifies the immutable preview/intent, and recaptures local
source before claiming. Database locks serialize a person's claims; a unique
person mutex also rejects concurrent first claims. No database lock spans HTTP.

Before PATCH, one immutable attempt receipt and `unknown` intent/person claim
are committed. Each intent can issue at most one PATCH through this executor.
Process death, failed HTTP responses, malformed responses, timeout, expiry after
claim or failed readback retain the unknown barrier. It can represent zero or
one actual request. Another intent for that person cannot bypass it, including
an unrelated blockout unknown outcome in the existing outbox.

Calling again on an unknown intent performs GET-only reconciliation. The
`reconcile_only=True` option also refuses to start a fresh write. A native value
still at baseline does not prove that no request occurred and never authorizes
a retry. Changed source, identity, another membership or unowned fields remain
unknown. No automatic cancellation, timeout expiry or new approval clears a
claim. Independent manual outcome investigation is required when readback
cannot converge; this module does not implement a force-reset path.

Successful readback must match the complete preflight snapshot with only the
target frequency and its `updated_at` changed. Other memberships, preferred
weeks, time preferences, identities and blockouts must remain equal. The current
local source must still match. The executor then atomically records the native
readback, a verified ownership baseline and `verified` intent, and releases the
person claim. A later independently reviewed preview may consume that ownership
baseline. Replay verifies the stored readback/ownership and does not send another
PATCH. A verified result describes the saved observation, not perpetual live
convergence or an edit to local preferences.

## Contract and checks

The API fields and labels are documented in the official
[Services membership API](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/person_team_position_assignment).
See also the existing [availability preview contract](PLANNING_CENTER_AVAILABILITY_PREVIEW.md).

```sh
python -m pytest tests/test_planning_center_frequency_executor.py \
  tests/test_planning_center_availability.py tests/test_planning_center.py \
  tests/test_planning_center_staffing.py tests/test_event_role_availability.py
```

Fakes verify the committed pre-HTTP unknown claim, strict PATCH body/identity,
untouched other roles, absent message/assignment effects, disabled behavior,
exact/expired reviews, stale native/local state, blockout exclusion, ambiguous
outcomes, readback drift/failure, GET-only reconciliation, replay, nested workers,
and persisted receipt/readback/ownership integrity. Passing these checks does
not establish actual native execution, notification silence or live two-way
completion.
