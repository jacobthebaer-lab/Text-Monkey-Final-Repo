# Operator-enabled roster enrollment and explicit welcomes

This source supports future website additions to an already adopted, natural
Mac Messages connection. It starts from either the original signed one-person
version 1 authorization or a settled version 2 enrollment. Existing session
IDs, original reply proof, route, cursor and delivery ledger are preserved.
Deleting a profile does not create a replacement profile or a welcome request.
Enrollment sends nothing and composes nothing.

The feature defaults off. The live-runtime owner must explicitly enable both:

- Database Policy `mac_roster_enrollment` with JSON
  `{"enabled":true,"actor":"actual authorized operator identity"}`.
- The existing sole connector's `--roster-enrollment` flag, alongside its
  existing delivery flags, and `--config` pointing to the canonical private
  connector configuration. The flag itself does not enable native sending.

Use one canonical read/write private config. A launcher must read that file
without rebuilding or overwriting it from an older seed config at startup. The
worker checks the actual canonical file, actual checkpoint bytes and active
claim list, rather than accepting a pre-generated enrollment package. Never
start another connector for enrollment. Private configuration, checkpoints and
pending enrollment files contain credentials or private identity data and stay
outside Git.

The current operator policy authorizes all eligible future roster additions:
active profile, recorded consent, valid phone and no STOP. Synthetic/fictional
markers, reserved NANP 555-0100 through 555-0199 phones, pending consent and
inactive profiles are excluded. Enrollment is additive; it never revokes an
existing signed session, resets conversations or changes existing consent.
Profile-sync authorization is independent and is not broadened here.

The connector-authenticated handshake offers one person, prepares a signed
revision against the exact accepted scope, atomically adopts canonical config
and checkpoint, then receives the backend's durable acknowledgment. Only that
last transaction installs the new backend scope and its session-bound
`conversational_signup:<phone>` policy. Reader scope changes after the
acknowledgment. The new session starts at actual adoption preparation time, so
old input history cannot become new enrollment authority. Native direct-chat
routing is checked separately at delivery.

Accepted scope lives in `mac_roster_accepted_scope`. Startup restores only a
verified descendant of configured authority. A canonical staged config cannot
admit its new participant before the backend commits it. A fsynced private
`.enrollment` transaction recovers lost responses or split-file crashes before
reading or sending. Consent, STOP, status or operator-policy changes cancel an
uncommitted enrollment and restore only its exact prior config/checkpoint.
Prepared scope freezes new claims. Unknown or unresolved native claims remain
held; ledger reconciliation requires an exact set of backend claim IDs, tokens
and submitted/blocked local outcomes, bound to the actual unchanged checkpoint.
An empty queue never proves a missing claim settled.

The roster shows Connecting until enrollment is accepted. Checkboxes and select
all use the current filtered eligible roster; selections survive readiness
polling and clear on session changes. Welcomes are a separate signed-in explicit
action. Each batch request composes at most one person through Gloo and the
existing consent, purpose, quiet-hours and exact-review gates. Its stable UUID
binds the administrator and selection, and durable progress survives lost
responses. A successful per-person receipt suppresses another welcome across
new batch IDs or individual clicks. Default stored stage `complete` is never
proof that a welcome was sent. Batch records are processing metadata, not due
notifications or scheduled outbound jobs.

Pending reviews and queued, dispatching, submitted or uncertain texts never
qualify for an automatic resend. A new explicit action can retry a rejected,
expired or cancelled unsent review, a queued guard's affirmative no-claim
receipt, or the separately reviewed native route hold's affirmative
`native_attempted:false` receipt. Missing claims, generic blocked labels and
unknown native outcomes do not establish that proof. The retry creates fresh
Gloo copy and fresh review when required, uses a separately bound conversation
key and archives the prior attempt. Prior bodies, hashes, reviews, reservations,
claims and receipts stay intact. Any actual inbound conversation after the
original attempt prevents restart. A completed old batch replays its original
result; a new UUID is required for a deliberate terminal retry.

Synthetic verification uses disposable SQLite databases, fabricated profiles,
fake Gloo and fake native readers/senders. It verifies source behavior, crash
recovery, 40 sequential enrollments and 40-person bounded welcome batches.
It does not establish live delivery, authorize runtime activation or provision
another transport. Runtime activation belongs to the existing sole operator.
