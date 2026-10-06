# Reviewed partial-slot coverage

This source extension supports local, reviewed split coverage for explicitly
selected roles. It does not enable scheduling, transport or live database
changes. Planning Center imported slots and child staffing writeback remain held
until an exact external partial-time contract has been verified.

## Coordinator workflow

1. In Shifts, load Reviewed split coverage and explicitly select an allowed role.
   This saves `split_role:<role_id>` with `{"value":true}`. Existing role clearance
   and fill policy stay authoritative; this setting grants no qualification.
2. Select an actual partial reply to an exact delivered whole-slot offer. Gloo
   interprets the dated boundaries. Ambiguous boundaries or unavailable Gloo hold
   the action. The source read transaction ends before this model request.
3. Review the complete gap-free interval partition. Approving creates two or
   three real child Shifts sharing the original Event. It creates no Assignment
   and sends no text. The parent cannot be independently staffed afterward.
4. Explicitly ask eligible helpers for these intervals. This queues the existing
   fill worker, with its existing contact authorization, Gloo, consent, care,
   quiet-hour, review and native delivery guards. No review endpoint sends.
   Gloo must include both exact dated boundaries and UTC offsets in child offers.
5. Actual timely YES replies to fresh delivered child offers are held. Review
   all interval/helper pairings together. Final approval revalidates every source
   and current eligibility and atomically creates one ordinary Assignment per
   child, or creates none. The parent receives no fake full-slot Assignment.
6. Cancellation reopens only that same child's interval. Existing eligible
   replacement offers and acceptance can fill it once the first atomic booking
   review has applied. Sibling bookings remain intact.

Every child interval must have current approved or confirmed coverage before the
parent counts as fully staffed. Overlapping/duplicate partitions, partial unions,
changed role/parent scope, duplicate helpers in the first booking review, stale
inputs, STOP, revoked consent, care holds and missing verified qualifications
cannot bypass the final review. Review hashes and actual receipt/body hashes are
retained. An ongoing native session may have a null expiry; a changed or inactive
session cannot authorize an old receipt. Original replies are timed against their
actual offers, without an arbitrary two-hour receipt-age cutoff. Human reviews
and fresh child offers retain their own bounded expiry.

Expired initial child acceptance sources expire their pending final review and
resume the same child's existing guarded fill worker. They never reserve or
book a helper after expiry. Explicit partition and booking review rejection
creates no records or sends beyond the retained internal decision audit.

## Representation and compatibility

`Shift.parent_shift_id`, `interval_starts_at`, `interval_ends_at` and
`coverage_review_id` are nullable for historical whole slots. Child intervals
share the existing Event; no synthetic Event, event-count inflation or duplicate
three-hour admin summary is introduced. Shift `starts_at` and `ends_at` provide
one effective interval in Python and joined SQL queries. Eligibility, overlap,
monthly caps, recurring windows, planning, offer deadlines, cancellation,
reminders, display and delivery preflight use these effective boundaries.
Whole-slot source snapshots retain their original shape. The existing unique
active-Assignment index still enforces one person per Shift.

## Activation prerequisites

Do not restart an old connected store against the new model until the selected
store has been backed up, workers stopped, its source migration reviewed and the
migration applied by its operator. Application startup does not perform this
migration or enable any role. For SQLite, `tools/migrate_split_coverage.py
--database PATH` inspects read-only; adding `--apply` explicitly applies the
transactional offline migration. For PostgreSQL/Supabase, the proposed migration
is `supabase/migrations/20261007010000_reviewed_child_shift_intervals.sql` in the
private `texty` schema. Neither has been executed against a live store here.

Integrate the Mac and shared session timing hooks against the latest ongoing
session implementation before connected testing. Preserve nullable expiry and
session fingerprints rather than replacing those source files wholesale.
Then review the application/router/UI hooks together, run the retained suite,
and verify actual connected offer delivery and receipt separately. Synthetic
source tests prove record and guard behavior; they do not prove Gloo availability,
native delivery, registered production transport or external staffing writeback.
