# Recipient selection and reply timing

Clyde's supplied Python module is preserved verbatim in
`app/core/clyde_algorithm.py`. `app/core/algorithm_outreach.py` adapts its pure
planner to the existing volunteer, outreach, fill and notification records.
No database migration or new dependency is required.

## Selection

The existing hard filters run first: consent and STOP, completed onboarding,
active status, role qualifications, availability, overlapping assignments,
serving limits, personal-care holds, outreach cooldown and monthly ask budget.
An active invitation or uncertain transport excludes that person. Anyone already
reserved/contacted for the event is excluded across its fill requests.

The algorithm ranks the remaining people using:

`S = acceptance_rate * elapsed_minutes / average_response_minutes`

`score = 10 * S / (S + k)`

Ties use the supplied module's string volunteer-ID ordering. `k` stays at 100,
as Jacob requested. Any positive `k` preserves this ranking order; changing it
changes the displayed scores, not their order.

Each application `Shift` represents one vacancy, so its `FillRequest` passes
`remaining_spots=1`. The shortest ranked prefix is selected until summed
acceptance rates reach the module's urgency-adjusted target. The unchanged
module can also plan larger pools with an explicit spot count. An application
batch may contain more people than vacancies. Expected acceptances are estimates,
not guaranteed bookings. Insufficient pools retain `target_met=False` in the
receipt rather than claiming coverage.

Selection reserves exact IDs under event/slot/person locks before asking Gloo to
compose. A durable `algorithm-batch:<fill>:<tranche>` receipt records selected
IDs, measured signals, target, expected acceptances, configuration, shift source
snapshot and any decline being replaced. Gloo cannot substitute recipients.
The send gate and native offer preflight only permit shared-vacancy offers for
members of code-reserved batches belonging to the same fill. Different fills
cannot share a vacancy or send overlapping invitations to the same person.

## History and enrollment

Only successful dispatch records (`sent`, `submitted`, `delivered`) contribute
real request history. A queue, review, blocked send or uncertain submission does
not become a request or reset the last-request timestamp. Use the immutable
`dispatched_at` timestamp where available, with send creation time for successful
legacy synchronous records. Submission is not proof of device delivery. Cooldowns and monthly budgets are
rechecked using actual outreach dispatch time, so a queue created last month
cannot hide a recent request from today's budget.

Acceptance statistics include explicit outcomes and expired requests. Positive
observed response durations supply the average response time. Pending requests
are not treated as declines before their deadline. A newcomer's estimates are
snapshotted at enrollment using equal-weight peer averages; empty pools retain
the supplied 100%, 60-minute, 1440-minute defaults. The synthetic scoring reference
is stored separately in `algorithm-enrollment:<id>` policy metadata and never
creates a message, outreach or delivery receipt. Existing imported profiles can
reconstruct a fixed baseline from enrollment-time history; later replies are
excluded from that reconstruction. Estimates age until a real successful ask.

## Timing and replies

Jacob asked to keep the supplied settings and defer a pending-probability model
until more data is available. No response distribution is assumed and no timed
probability-based expansion calls `plan_follow_up` today.

Existing `offer_response_window` settings own reply timing. With the defaults,
each successful dispatch starts a window of:

`min(120 minutes, max(2 minutes, time until event / 6))`

The reply deadline is capped at ten minutes before the event and rounded down to
an exact second. If fewer than two minutes remain in that window, the application
creates an internal coordinator task rather than another invitation. Native queue
and review time do not count as successful dispatch. Exact-reviewed wording and
hashes retain their existing fresh-review requirements when a deadline changes.

While invitations remain open, the app waits until the earliest active reply
deadline. Each unique decline may select one next uncontacted person immediately,
including while other dispatched invitations are still pending. The decline ID
is consumed in the durable batch receipt, preventing duplicate replacements.
Undispatched or held members finish their review/dispatch before opening another
tranche. Quiet hours defer replacement sends; expired offers are closed before
planning the next batch. UTC elapsed time and the existing church time zone keep
DST changes from altering elapsed minutes.

The first eligible acceptance obtains the slot under locks and closes sibling
invitations and reviews. Queued siblings are superseded. A later acceptance cannot
create a second assignment. A placement made externally also closes the batch on
the next timer scan, even before its reply deadline. Event/source changes and
uncertain delivery retain their existing holds and coordinator reconciliation.

## Activation and verification

`recipient_algorithm` contains `k=100`, `timescale_minutes=120`, and
`maximum_buffer=0.5`; malformed values hold the fill for internal review.
`algorithm_outreach_enabled` defaults to **false**, preserving current live holds.
This source integration does not change any runtime setting, scheduler, transport
allowlist, test-session expiry or Google Voice policy. Enabling this application
policy alone never establishes real-transport authorization. Gloo composition,
consent, exact review, quiet hours and transport gates still apply.

`tests/test_clyde_algorithm.py` uses fictional records, a fake clock, a scripted
Gloo and mock SMS. It covers measured scoring, prefixes and insufficient pools,
enrollment persistence, successful-send history, batch dispatch, decline dedupe,
expiration, first-winner arbitration (including concurrent SQLite workers),
eligibility, event dedupe, source changes, quiet hours, DST, filled slots and
invalid configuration. Other regression tests cover native delivery, exact
review, message style, signup and profile synchronization. These are synthetic
acceptance checks, not real Gloo or device-delivery evidence.
