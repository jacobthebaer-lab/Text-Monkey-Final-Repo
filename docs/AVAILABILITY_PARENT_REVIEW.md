# Signed-in availability collection approval

The workflow requires an owner-bound parent review for an exact month and frozen
recipient scope, then a separate exact recipient-and-text review. Approving scope
does not deliver texts or activate scheduling. Isolated tests use fictional
identities, fake Gloo, and mock or Mac queues with synthetic bridge acknowledgments.
They do not prove actual native delivery or live model behavior.

## Integration

The router is registered in `create_app`; Schedule uses `planning-workflows.js`.
Generic review deliberately refuses `confirm_collection`; only the dedicated
endpoint can approve it after verified allowlisted identity and owner checks.

## API

Base: `/api/planning/availability-collections`. Uses existing Supabase bearer
authentication/allowlist and bridge restrictions. Both global application modes
support this workflow. Its short-lived authenticated transaction uses exact
confirmation mode for collection preparation; global settings and other sessions
retain their configured mode.
Owners come exclusively from verified user IDs, never request bodies.

| Request | Body | Behavior |
| --- | --- | --- |
| GET base | None | Owner-filtered `{collections: [...]}` |
| POST base | `{month, request_id?}` | Preview exact current month/scope, pending parent |
| GET `/{id}` | None | Owner-filtered collection envelope |
| POST `/{id}/approve` | `{content_hash}` | Approve parent, create bound child; zero model calls |
| POST `/{id}/reject` | `{content_hash}` | Reject parent; zero model calls |
| POST `/{id}/retry` | `{content_hash}` | Explicitly prepare at most one recipient text through Gloo |

Except for the list, envelopes contain `collection`, `text_review_ids`, `sent:0`,
`delivery_enabled:false` and `scheduler_activated:false`. Collection fields:
`id`, `month`, `status`, `content_hash`, `expires_at`, `authorization_expires_at`,
`decided_at`, `scope`, `collection_id`, `composition_status`, `text_review_ids`,
`remaining_recipient_count`, `retry_at`, `hold_reason`.

Scope contains `scheduling_scope:existing_single_church`, the existing scheduling
policy timezone, `recipient_count`, exact `{volunteer_id,name,phone}` recipients,
and aggregate `excluded_counts`. It excludes inactive/non-serving participants,
missing or globally revoked consent, existing month availability, care holds and
current monthly ask limits. Staged imports do not enter this scheduling scope.
It is the existing single-church store, not a new multi-tenant roster system.

Pending review expires after two hours. Approved month/scope authorization lasts
through target month end, including its permitted later reminder; each text still
needs a current, separate exact approval. Preparation never auto-retries or sends.
Review IDs represent only valid pending exact text approvals, not delivery receipts.
Composition states are `not_started`, `reviews_pending`, `held`, `blocked_policy`,
`no_remaining_recipients`; zero remaining can mean valid dynamic skips.

## Conflicts and source checks

Foreign parent IDs return 404. Client owner/scope/recipient fields return 422.
Wrong hashes, stale scope/month, wrong decision state and preparation without
approval return 409. Repeated identical decisions do not create a child or invoke
Gloo. Optional UUID requests replay the original review; reuse for another month
returns 409. Request receipt keys fit the existing PostgreSQL VARCHAR(80).

Fresh requests can re-scope changed data and expire the older authorization.
An approved scope never silently adds newcomers or changes a reviewed phone/name.
Changed identity explicitly holds; revoked consent, care and supplied availability
are safe dynamic skips. Existing native/text source preflight checks the parent.

`approved_collection_problem(session, child, now)` exports the integration guard:
prove owner, valid approved parent/hash, exact bound recipient IDs/scope, child
linkage, timezone and live month authorization. `contact_binding` additionally
binds the current source and exact recipient to conversation policy. Approval,
queue claim and native preflight recheck that binding, consent, care, ask budget,
and the original validated Gloo composition receipt. Direct or legacy availability
asks without these reviews cannot stage or deliver in either global mode.
Do not activate scheduler batch collection
or broad operations controls; the explicit API preparation action is separate.

SQLite demo requests commit inside a process lock; Postgres also locks the parent
row. Concurrent explicit preparation cannot duplicate a recipient's model call or
review. Gloo failures retain the existing bounded receipt/backoff/hold behavior;
seed text is never a fallback.

Conversation keys permit one initial ask and one reminder per recipient and target
month, even if a collection is requested again. A reminder requires the original
approved initial text, unchanged source and composition proof, sent, submitted or
delivered status, and at least three days since that ask. It still needs its
own Gloo composition and exact text review. Uncertain or queued initial asks do not
authorize reminders. Quiet hours continue to hold both preparation and delivery.

## Validation

Focused coverage includes authenticated scope/prepare/exact-hash approval in both
global modes, owner isolation, forbidden scope fields, stale identity and month,
rejection/idempotency, one-recipient preparation, concurrency, Gloo outage and
invalid typography, reminder provenance, care/STOP/budget/quiet holds, and legacy
or uncomposed asks. Isolated Mac HTTP tests exercise approval, queue claim and
native preflight, including revocation between each boundary. Exact results and
commands are recorded in the PR. Runtime and transport configuration are unchanged.
