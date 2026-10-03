# Durable frequency review receipts and committed source

This source-only batch adds a separately mounted review router, signed durable
review acknowledgements and a same-store committed Mac/Gloo availability reader.
It changes no existing routes, startup imports, authentication, profile pipeline,
executor or configuration. It does not apply a migration, register the new
router, activate any runtime work, call Planning Center or send messages.

## Authentication and authority

The optional `planning_center_reviews.router` reuses the actual Supabase bearer
`admin` dependency, current confirmed-email coordinator allowlist and nonlocal
bridge check. It has a local preview GET and review-acknowledgement POST. The
POST accepts only four exact proposal hashes; actor, membership, evidence,
approval flags and body attributes cannot be supplied by the client. Actor UUID
and email come from the authenticated principal, and the service rechecks the
coordinator allowlist. It never uses the legacy Basic/open-local route.

Receipts bind the exact organization, person, role/service/team/position/
membership, intent, preview, source/profile revision, remote snapshot, operation,
review action, actor and ten-minute expiry. Their canonical document has a
domain-separated HMAC-SHA256 signature from a private server key of at least 32
bytes. Row metadata is checked against the signature-covered document. Forged,
changed, revoked, expired or foreign-actor/source receipts are refused.

The future integrating owner must separately provision and protect
`app.state.pco_review_signing_key` as bytes. No key is generated, committed,
loaded by default or copied from another credential. Missing key holds the POST.
Key rotation invalidates old signatures. Database rollback removes an uncommitted
review; the API commits only its dedicated local receipt transaction.

Every receipt's decision is **reviewed_held**, with notification-silence,
native-edit coordination and fresh-native-preflight holds. A review is an
acknowledgement of the exact proposal, never permission to apply it. The
`authorize_frequency_execution` authority validates durable evidence and then
fails closed because no established native release evidence authority exists
yet. Constructing `FrequencyReview`, passing a hash, adding UI flags or editing
a signed document cannot authorize through this authority. No execution route
exists. The existing low-level executor still trusts its internal caller; future
production wiring must consume the durable authority, never expose that raw
primitive or synthesize its trusted dataclass from request JSON.

## Committed source reader

Use `committed_source_factory(engine)` for a dedicated transaction and supply
`CommittedAvailabilityReader(settings, config, volunteer_id=..., profile_key=...,
source_id=..., clock=...)` as the source callable. Generic sessions are held.
`CommittedSourceSession` remembers source writes after flush, so clearing
`Session.dirty` cannot make uncommitted mutations into evidence. Commit/rollback
resets that marker; no global listeners are installed. Outbox/claim/review writes
are distinct from source rows. The reader refuses pending work and never changes
preferences, creates source/receipt rows, replays Gloo or imports identity maps.

Every call locks and freshly loads the volunteer, scoped Services map, policy
source identity, ProfileOutbox, MacInboundReceipt, matching messages, availability
and role/event catalogues. PostgreSQL uses row locks; SQLite uses BEGIN IMMEDIATE
when its physical transaction has not started. An integrating caller must use
this dedicated session factory consistently and preserve lock order with other
source writers. Locks cannot resolve absent-row insertion races or another
writer acting after the final local recheck; native-edit coordination remains a
release hold rather than a transactional consistency claim.

The reader requires the current profile and Mac recipient allowlists, enabled
profile mirroring, Mac transport, an availability-bearing recorded route, one
exact sender/message fingerprint match, active recorded consent and the exact
current serialized profile/outbox digest. A newer or ambiguous profile revision,
foreign source ID, held profile, changed map/profile, missing receipt or unmatched
sender history holds the preview. It reuses the existing profile snapshot/schema
validators and availability capture; Gloo output is not reinterpreted from text.

This proves consistency with the app's trusted committed bridge/profile records,
not external cryptographic proof of a native message or independent Gloo
execution. The backend/bridge and source-write permissions remain trusted.
Copied cloud profiles without the same-store Mac receipt/message/source queue
are held. The current private actual profile must be independently verified;
synthetic fixtures do not establish that it is ready.

## Unapplied migration and remaining integration

[Proposed migration](migrations/20261003_pco_frequency_reviews.proposed.sql) lives
outside automatic migration discovery. It has not been applied or PostgreSQL
execution-tested. It covers the two availability tables, three executor tables
and signed-review table, including ORM indexes and backend-only RLS/grants.
UTC timestamps match the app's UTCDateTime storage convention. It requires the
existing same-store source/profile/identity tables and backend role, and fails
on existing target tables to prevent a partial schema from being silently adopted.
The owner must compare existing schema, review privileges/backups and explicit
target, then apply the transaction only after independent approval. Never run it
from app startup or assume that a copied cloud schema contains valid provenance.
Rollback before commit is atomic; after commit preserve receipts/unknown claims
and use a reviewed forward correction instead of destructive table deletion.

For the Mac SQLite store, this PostgreSQL artifact is not executable. The owner
must review a target-specific SQLite transaction for these same six tables and
indexes, or generate their DDL from the six named ORM tables for review first.
Inspect existing columns/nullability/keys before adoption; do not call broad
metadata.create_all or alter source tables. Keep the same backup, explicit-target
and manual-application requirements. Synthetic tests initialize isolated tables
only, and establish no live migration or PostgreSQL lock behavior.

Remaining work is independent review, verified actual source/mapping, target
migration approval/application, signing-key provisioning, optional protected
router registration, authoritative native notification and edit-coordination
evidence, and authenticated guarded executor wiring. Those dependencies are
explicitly unmet. There is no human decision blocking this engineering batch;
live release remains held for the listed evidence/integration requirements.

```sh
python -m pytest tests/test_planning_center_frequency_reviews.py \
  tests/test_profile_sync.py tests/test_planning_center_availability.py \
  tests/test_planning_center_frequency_executor.py
```
