# Texty backend MVP

Volunteers text the church number; they do not need an account, website,
or link. Gloo interprets text and selects replacements. Application code
validates every change, writes the private Supabase store, and serves the
same data to Texty's roster and calendar.

Regular SMS is the intended live transport. The first-party Mac connector
can use the church iPhone's forwarded SMS with a Google Voice volunteer test
number. Both service and recipient are explicitly restricted; iMessage is
optional compatibility support. See [SMS test setup](MAC_MESSAGES.md).
Real-device SMS verification remains pending until the tester number and
iPhone forwarding are configured.

## Signup

1. JOIN and a first/last name create an inactive, unconsented profile.
2. Explicit YES records scheduling-text consent. STOP always takes priority.
3. Gloo collects catalogue role interests; ANY keeps options flexible.
4. Gloo collects weekdays/service times, specific date exceptions, and a
   monthly serving limit (default two, allowed one through eight).
5. The completed profile becomes eligible for matching. A name/consent-only
   signup still finishing setup is excluded. Returning messages match the
   normalized phone number. SETUP or PROFILE restarts preference collection.

Qualifications, pastor/coordinator privileges, and assignments never come
from a volunteer's signup claims. Existing profiles retain their settings.
A role requiring clearance remains unavailable until its credentials are
verified. One clarification is attempted before unfinished setup needs review.

`full_text_onboarding` is enabled in the demo seed and the connected MVP store;
its unseeded default stays off for compatibility with older installations.
`GLOO_SIGNUP_REPLIES=true` enables the Gloo reply writer for signup and factual
transactional updates. Prompts are versioned in `prompts/` and supplied with
API requests; no special platform-side skill installation is needed.

The signed-in roster has **Start text setup** (or **Restart text setup**) for
active, opted-in enabled test phones. It calls the Gloo reply writer, queues
through SendGate, and starts interest collection without faking an inbound
message. Setup already in progress rejects a second start, preventing repeated
invites. Delivery pause, opted-out phones, sensitive holds, quiet hours and Gloo
failures leave the profile unchanged. Volunteers answer entirely by text.

## Cancellation and replacement

A cancellation immediately removes the original assignment. If multiple
upcoming shifts match, Texty asks which one before changing anything. Gloo
chooses from the full eligible pool and writes individual invitations. Code
checks opt-in, active status, completed setup, stated availability, verified
qualifications, overlapping shifts, serving frequency, and contact limits.

| Time remaining | First batch | Subsequent batches | Waits | Escalation deadline |
| --- | --- | --- | --- | --- |
| More than 48 hours | 3 | up to 5 | 4h, 4h, then 6h | 24h before start |
| 12–48 hours | 3 | up to 5 | 1h, 1h, then 2h | 6h before start |
| 2–12 hours | 3 | up to 5 | 20m, 20m, then 30m | 90m before start |
| Under 2 hours | up to 5 | up to 5 | 10m | at start |

Wait tier can become more urgent as time passes. The escalation deadline is
fixed when the cancellation starts; it does not drift toward the event.
Batches continue through previously unasked eligible people until the pool
is exhausted or the deadline arrives. There is no “send to everyone” batch.
If everyone declines, the next batch can start early. No reminder nudges are
sent for an unanswered invitation.

Each person receives at most one invitation per request, at most one ask per
24 hours, and at most four asks per month by default. Ordinary outbound asks
pause 9pm–7am; urgent same-day asks pause 9:30pm–6:30am, church local time.
A quiet-hour search persists its wakeup; it does not incorrectly escalate
because nobody was texted overnight.

## RSVP and staffing

YES or NO works directly for a single delivered offer. When multiple offers
are outstanding, Texty asks for YES R[number] / NO R[number] using the code
already present on each invitation. An unsent/held invitation cannot book a
volunteer. Eligibility and monthly serving limits are checked again at YES.

Postgres locks the event/slot/person/request for acceptance; a partial unique
index also prevents any other write path from creating two active assignments
in one slot. The first eligible YES confirms the assignment. Others receive
one closure; duplicate replies do not generate duplicate confirmations.
A late YES cannot double-book the slot. A pending quiet-hour closure can be
released immediately when its own recipient replies. Offers expire at event
start. Previously escalated, actually delivered offers can still recover a
slot before start if it is still vacant and the sender remains eligible.

Queued Mac invitations are suppressed if their request closes, the event
starts/closes, or the recipient opts out or needs personal-care review.
Restricted-role batches retain coordinator approval. YES A[number] identifies
an approval; a bare YES cannot approve an arbitrary batch when several wait.
Quiet-hour approved invitations are retried durably, without duplicate asks.
Unapproved batches expire at their deadline and enter the admin review queue.

The dashboard updates from the same Supabase records, polling every 10 seconds.
It shows setup stage, calendar coverage, replacement batch/replies, next action,
and human-review items. Proposed assignments do not count as staffed.

Coordinator staffing texts combine changes for five minutes and have at least
15 minutes between status updates, including updates across different events. Status is recomputed at send
time across all required role minima. Filling one slot never falsely declares
the whole event fully staffed. Unchanged status is not resent. Open gaps,
restricted approvals, and searches needing help appear in the summary.
Personal-care messages remain a human workflow.

## Durable notifications and delivery

The additive `notification_outbox` migration creates a private RLS-protected
outbox and the active-slot index. Backend-role CRUD was verified in a rollback
transaction; anon/authenticated access remains denied. Existing data and
sequences are preserved. Outbox keys deduplicate confirmation/closure texts;
quiet-hour deliveries retry when permitted and recheck opt-out/sensitive holds.
Gloo composition failures retry twice after two minutes, then require review;
they do not undo an already confirmed assignment.

A fresh matching inbound message permits its own transactional response during
quiet hours for up to ten minutes. It never permits outreach or another
person's replies. Mac delivery revalidates the matching inbound proof.
Native dispatch still uses durable claims and does not blindly resend an
uncertain Messages submission.

Production's scheduler ticks every 30 seconds when DEMO_MODE=false and
AUTOMATION_ENABLED=true. Set AUTOMATION_ENABLED=false to pause background work independently of delivery. The authenticated Texty text
lab and `/api/automation/tick` always use mock delivery; their database changes
still persist. They are admin testing controls, not an isolated scratch store.
Use the unit suite or the isolated Gloo smoke script for throwaway fixtures.

## Verification and current phone test

The isolated real Gloo test passed name/consent/interests/availability signup,
cancellation, model-selected replacement, YES confirmation, and a fully staffed
coordinator summary: 12 audited runs, 31,368 input and 3,758 output tokens,
eight mock messages. No real contacts or Messages application were used.
The versioned automated tests also cover late/duplicate replies, multiple
invitations, quiet-hour wakeups, STOP before deferred delivery, restricted
approvals, non-sliding deadlines, and the database uniqueness guard.

The October 2 authorized phone test enables only the previously approved
volunteer on the existing church-line conversation, with a fresh watermark
that skips prior messages. A real Gloo-generated interest question was saved
to Supabase, submitted by the native worker, and reported delivered by Messages
on the selected line. This conversation uses iMessage; real carrier SMS delivery
remains unverified. The full availability/cancellation phone journey still needs
the volunteer's actual replies; no test replies are impersonated.

Background timers remain paused during the single-person test. Immediate
inbound processing and replies are enabled only for that tester. A second
consenting, configured phone is required to verify real replacement acceptance
and another person's notifications; simulated multi-person tests remain the
proof for batching, timers and first-YES behavior. The latest suite passes
215 backend and five frontend tests.

The default Messages sender remains unchanged. Backend and tunnel availability
depend on the Mac. Pausing requires disabling the bridge and stopping the native
worker; `AUTOMATION_ENABLED=false` separately pauses background work. Reading
this document never authorizes delivery to new recipients.
