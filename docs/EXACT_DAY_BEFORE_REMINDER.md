# Literal day-before reminder

The human's submitted wording is preserved exactly:

> Hey Clyde, Text Monkey here. You're signed up to greet tomorrow at 10am. If we don't hear from you, we'll assume you're good to go. If you can't make it, just let me know.

Only the actual saved recipient, serving role and local shift time are substituted.
Greeter/greeting roles render as `greet`; other saved roles render as `serve in the
{role} role`. Times use the scheduling policy timezone, with `10am`, `12pm` or
`10:15am` formatting. `tomorrow` is used only on the local day before that shift.
The formatter alone does not authorize contact or establish eligibility.

`reminders.process` sends the desired copy through a dedicated Gloo call. The raw
model output must equal the approved literal string, character for character.
Paraphrases, quotes, whitespace, emojis, YES/STOP/HELP or added confirmation
instructions fail closed. There is no cleanup, template fallback or alternate AI.
On the day-before date the duplicate initial planner-confirmation preview is
suppressed. Earlier pending or approved but unqueued confirmation reviews expire;
approval and native source preflight also reject them before the next job tick.
Signup is untouched. Silence keeps the existing assignment; only an
actual cancellation changes it and routes the existing replacement workflow.

The existing receipt/dedupe, consent, eligibility, care, recipient-session and
quiet-hour checks remain. Exact human review still precedes queue submission.
Approval and native preflight recheck source and the current literal role/time/name
copy; policy timezone changes cannot reuse an old time. Saved source timestamps
are canonical UTC so equivalent local/UTC timestamps do not cause false holds.
Old nonliteral reminder reviews require fresh review and cannot dispatch.
Saved facts containing an em dash require correction before composition or review;
the application never silently rewrites a reviewed body.

## Runner hook and timing

`app.main.create_app` lifespan starts `BackgroundScheduler` only when the initial
configuration has `AUTOMATION_ENABLED=true` and `demo_mode=false`. `fill_tick`
calls `app.jobs.process_jobs` every **30 seconds**, which runs `reminders.process`
for connected providers in exact-confirmation mode. Merely changing settings flags
on an already paused app does not start that scheduler.

There is no morning cutoff: an eligible approved/confirmed assignment created later
on the day-before date is picked on the next tick. It stages a source-bound literal
review, not an automatic delivered text. Quiet hours/session/Gloo failure hold it.
The current literal `tomorrow` copy is never released on the event day after a hold.
Runtime start/restart, recipient configuration, human review and delivery evidence
belong to the integration/live-send owner; this patch activates none of them.

With connected transport and `confirmations.MODE_KEY` false, reminder preparation
holds. A live owner with explicit approval for the particular real message can use
`session.info[confirmations.MODE_KEY] = True` for that transaction, prepare the review
and call `confirmations.decide(session, gate, approval, approve=True,
actor="explicit human chat approval", expected=displayed_content_hash,
now=clock.now(), ctx=ctx)`. This core helper requires no forged browser JWT.
Retain the actual human approval evidence privately and verify eligibility first.
The native claim/verify owner must honor existing exact-review proof per message
even when its global confirmation flag is false. Transaction-local preparation
alone cannot establish that protection in the current native worker. This patch
does not change transport or activate automatic delivery.

The synthetic timing test is October 3, 6PM Denver with an October 4, 10–11AM
fictional greeter assignment. It is not evidence that any real volunteer is eligible
or assigned to that event. Actual availability and the saved shift must be verified
before a live owner prepares the reminder; never create a mismatched assignment to
make the requested text true.

## Validation

Full isolated backend: **784 passed, one unchanged quiet-hours expected failure**.
Focused cases check literal equality and forbidden edits, missing/outage Gloo,
role/time substitutions, changed source/timezone at preflight, dedupe, silence and
explicit cancellation/replacement, late-created assignments without duplicate
confirmation, superseded but unexpired confirmation reviews, forbidden punctuation
in saved facts, and equivalent timezone timestamps. No eval criteria or outreach
algorithm were changed.

[Sanitized actual-Gloo evidence](evidence/exact-day-before-gloo.json): one real model
call accepted the literal copy for a fictional record using mock transport. It
staged one reminder review and no duplicate confirmation; **zero messages queued**
before human review and **zero real messages sent**. Usage: 881 input / 249 output
tokens. It does not prove active runtime, actual recipient eligibility or delivery.
