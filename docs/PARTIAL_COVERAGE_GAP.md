# Partial-response coverage: verified limitation

The `partial-response` feature remains **partial**. Recognizing a limited offer
does not implement reviewed split coverage. The current fill path records the
partial response against its scoped delivered offer and seeks a person who can
cover the full slot, subject to offer deadlines and current contact policy. It
does not book the partial helper, declare the slot filled, combine offers or
send an automatic thank-you when the essential-message policy holds it.

## Concrete representation and integration gaps

- `Shift` contains only `event_id`, `role_id` and `slot_index`. Its entire time
  interval comes from `Event.starts_at` and `Event.ends_at`. `Assignment` has no
  interval or parent-coverage reference. Its unique active-slot index enforces
  one person per slot.
- `ParsedMessage.partial_window` is optional descriptive free text. It is not
  validated into dated, timezone-bound start and end instants. Inbound routing
  passes the `partial` intent, not that interval, to the fill decision.
- `Role` has no administrator-controlled `allows_split` setting. `fill_policy`
  controls review, not permission to split a slot.
- Planning Center staffing currently requires one exact mapped service time and
  the whole local event interval. It holds partial PlanPerson acceptance,
  multiple service times, per-time needs and changed time mappings. This is the
  supported adapter contract, not a claim that Planning Center can never support
  partial staffing.
- Existing approvals review exact proposals but cannot manufacture interval
  ownership, parent coverage accounting or a verified external time mapping.
  Creating another full-event Shift would duplicate staffing demand; changing
  Event times would affect every role and invalidate existing offers.

## Required contract before implementation

A usable split path needs an explicit administrator role opt-in and durable
interval-bearing child slots linked to the original coverage demand. Each child
must retain one-slot-one-person. The parent must remain unfilled until its entire
required interval is covered without gaps or double-counting. A reviewed proposal
must bind each helper's actual inbound receipt, concrete delivered offer, exact
dated interval and current consent, qualification and availability evidence.
Review application must recheck and apply all children atomically under the same
slot/person locks; partial failure must leave the parent and bookings unchanged.

Cancellation, reminders, monthly counts, double-booking, placement notices,
schedule validation and native preflight must understand these child intervals
and invalidate stale reviewed facts. Imported Planning Center slots must remain
held until a separately verified partial-time write/readback and cancellation
contract exists. An internal review is not that external proof.

These prerequisites cross schema, eligibility, planning and external staffing
boundaries. Adding a second timing model during the connected demo would be an
incomplete extension, so this patch adds no production split path or live writes.
The original feature ID and requested split behavior remain in the feature
universe and PLAN, with the gap recorded rather than marked implemented.

## Focused proof

The retained `tests/test_evals.py::test_workflow_replay[partial_offer]` verifies
that one partial reply produces no full assignment. The new
`tests/test_partial_coverage_boundary.py` runs two complementary partial replies
through actual inbound/offer/fill handling for a synthetic imported Planning
Center slot with the staffing hook enabled. The historical invitations are
sequential, with the first expired at its real deadline before the second,
preserving the existing one-active-invitation policy. It verifies that the full slot
remains unfilled, no child slot or active assignment appears, the original times
and cancellation history remain intact, both actual reply bodies are retained,
and the synthetic staffing outbox performs no write. This establishes the safe
hold boundary, not split-coverage functionality, real Gloo quality or delivery.
