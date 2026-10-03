# Personalized signup recovery

Base: edc40f29053dbe0c31b0728338b9f7ff15ab1f44. Branch:
codex/adaptive-signup, worktree
/Users/jacob/.codex/worktrees/onboarding-message-settings/Text Monkey.

## Source integration

Integrate the availability owner's commits first:
3a9a76d7bf4857c74168c356ba41c9b7af43519f and
80c661f21a601e2cd5b7a3707f5594002cceaaac. This worktree contains equivalent dependency commits 707a5b1 and
a8b0d8a for combined testing. Do not duplicate those commits if already present.
The conversation-editor release commit is recorded in this chat's final receipt.
Also integrate f6a83b2, the global send/native punctuation boundary release. It
supplies app/core/message_style.py, reused by this writer and the decoded JSON
recovery validator. Equivalent local dependency commit here is 1ba4f4b.

Owned files: app/core/signup.py, app/core/onboarding.py,
app/core/signup_responder.py, new app/core/signup_recovery.py,
prompts/signup.md, prompts/onboarding.md, prompts/signup_reply.md,
tests/test_adaptive_signup.py, tests/test_exact_signup_copy.py,
PROMPTS_CHANGELOG.md and this document. No UI or shared checkout files changed.

## Conversation behavior

The four original approved messages remain literal on the normal path. The
deleted clarification is not reintroduced as a routine fifth step. Jacob's new
authorization permits one personalized recovery message only when an actual
reply is off-topic, ambiguous or incomplete. Gloo receives the actual reply,
stage and this person's saved answers. It returns a short acknowledgment and
the exact current missing question in checked JSON. There is no template
fallback, automatic YES/STOP/HELP addition, extra question or roster advancement
from that output. Invalid Gloo output or an outage holds for review.

An unknown sender who replies off-topic after a scoped app invitation gets a
gentle name redirect. Unrelated two-word phrases do not become legal names:
Gloo must identify an actual identity reply and code must match the actual text.
A genuine first-name-only reply is stored against its actual Message ID and
session; only the last name is then requested. A later genuine last-name reply
can complete the name while preserving invitation receipt, same sender,
session, age and STOP checks. No fake JOIN, YES or inbound row is synthesized.

Preferences recovery asks only for missing roles or availability facts. A
frequency-only answer is retained and only days/dates are requested. Ordinary
unrestricted weekday/all-day replies still complete without inventing frequency,
keeping the original four-message normal flow. Role/time/group-specific answers
preserve validated recurring_windows in the partial draft and final preferences.
Complex windows require actual frequency and known hours before completion;
unknown Wednesday hours never become all-day availability. The recovery asks
only for missing hours/frequency together, then only the still-missing detail.
Already validated Sunday hours or role interests are not re-requested.

Gloo receives WINDOW_SCHEMA_INSTRUCTIONS plus role/type catalogues, selected
roles, explicit any-role state and saved windows. It must use recurring_windows
for ranges/group context, not invalid invented preferred_services enums. A
newly stated Coffee interest is added to existing interests without granting
qualifications, coordinator approval or a booking. A frequency-only followup
preserves the Sunday time window and the separate Wednesday role/group window.
Unresolved event-type mappings remain ineligible under the availability module.
Sunday 08:00-10:00 excludes a 10:00-11:00 event. Do not bypass this rule to make a
demo cancellation possible.

After two consecutive redirects without valid progress, the flow holds for
coordinator review. Valid progress resets the retry counter. Sensitive replies
keep their care escalation, privacy requests hold for review without a signup
redirect, and STOP remains authoritative. The reply writer and recovery
validator reject U+2014 em dashes before enqueue, never altering an already
approved message. The separate global send/native validator owner's work is
still required to cover transports outside this writer.

## Live owner

This release did not send texts, call real Gloo, restart a reader, reset a
session/checkpoint, modify a runtime database, deploy or push. The sole live
owner integrates both dependency fixes and this commit, then reprocesses the
preserved held actual inbound once using its original audit/Message ID. Re-run
Gloo interpretation with the new window schema, preserving earlier name,
consent, interests and reader checkpoint. Do not use an old invalid enum result
as guessed availability, insert a new fake message, replay delivered messages,
send another opening or fabricate the missing frequency/hours. Further real
replies must come from the person.

Concrete recovery entry point is `onboarding.handle`, not `handle_inbound`.
Within the live owner's existing transaction, load the original received
Message through `conversation.scope(..., active_session)` and its volunteer;
verify phone, original receipt/fingerprint, current availability stage and that
no linked reply/approval/delivery was already produced. Do not insert an inbound
Message or clear the native receipt/checkpoint. Acquire the owner's runtime lock
before this one-off operation. Use the existing actual provider/Gloo instances:

```python
gate = SendGate(session, clock, provider, reply_to_message_id=incoming.id)
gate.gloo = gloo
session.info.update(
    mac_test_session=active_session,
    sender_phone=incoming.phone,
    confirmation_now=clock.now(),
    conversation_origin="mac_messages",
    sender_record_permissions={},
    sender_profile_instruction=True,
)
outcome = onboarding.handle(session, clock, gate, volunteer, incoming.body, gloo)
session.flush()  # keep the real sender's field authorization active through flush
```

The root owns invocation, not this source editor. Preserve the original receipt
and record a separate recovery audit, outcome and resulting outgoing Message
IDs atomically with the preferences. Use a unique recovery key per original
Message ID and refuse a duplicate completed recovery. If Gloo holds without
producing a reply, do not claim delivery or fabricate answers. Send only through
the normal existing native queue, then verify actual delivery and literal bytes.
Restore transient session.info authorization after commit/rollback. Existing
name, consent and interests remain; the handler starts at availability.

Tests use fictional sender/body data, mock transport and Gloo stubs. Coverage
includes the exact four-message normal path, off-topic replies at every stage,
name parts and two-word non-names, partial frequency, role/time/group windows,
new role interest, complete-event exclusion, no factual/consent/clearance
invention, unavailable/invalid Gloo, privacy/sensitive/STOP and em-dash rejection.

The focused signup/window/eligibility selection passed 172 cases, including the
final decline/identity-draft cleanup and canonical-window precedence additions.
The full run collected 864 cases: 845 passed, 1 historical quiet-hours expected
failure, and 18 failed because old cancellation/clarification templates still
contain U+2014 and this writer now correctly rejects them. These are a real
combined-release dependency on the global punctuation/send-validator owner's
patch, not a reason to relax the prohibition or silently rewrite approved
messages. The full suite must be rerun after that patch is integrated.
The global patch is now present. Formerly failing-case verification reduced the
18 failures to two old scripted-offer fixture failures: their synthetic Gloo
offer in tests/test_fill_agent.py:60 still contains an em dash, so the send
boundary correctly blocks it. The fixture should use a comma under the new
human requirement; production protections must remain. This editor did not
alter that separately owned fixture, any eval case, eligibility rule or template.
Transactional composition inside SendGate retains its final BLOCKED_STYLE
outcome; signup composition done before SendGate rejects invalid typography
immediately. Both use the shared message_style.py rule without rewriting copy.
Final signup/exact-copy/global-style selection passed all 97 cases after sharing
the global style helper. The two remaining broader failures are the unchanged
scripted-offer fixture noted above and must be repaired by its integration owner.
