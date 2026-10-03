# Null frequency correction

Base: 0c6cf8d. Branch: codex/nullable-signup-frequency. Worktree:
/Users/jacob/.codex/worktrees/onboarding-message-settings/Text Monkey.

Gloo can return frequency_known=true with max_per_month=null while supplying
valid independent role/day/time/context windows. That flag/value contradiction
now means unknown frequency. The valid windows remain in the partial draft and
new Coffee interest is preserved. If this person already has a validated known
frequency, its value is retained instead of erased or replaced. Non-null invalid
integers, floats, strings, booleans and invalid window data still reject.
Unknown Coffee hours remain unknown; no whole-day inference or booking occurs.
The follow-up asks only for Coffee hours and frequency, not already supplied days.

Changed files: app/core/onboarding.py, prompts/onboarding.md,
tests/test_nullable_signup_frequency.py, PROMPTS_CHANGELOG.md and this document.
Checks: all 93 nullable-frequency/adaptive/exact-copy/natural-availability cases
passed, including 17 new focused cases. No full-suite repeat, real Gloo call,
text, runtime database edit, reader/session reset, deployment or push here.

## Sole live owner continuation

The existing validated_availability and recover_preferences helpers can consume
the privately verified logged Gloo extraction in the owner's prepared locked
continuation. Save the validated partial draft, add only its known role interests,
and invoke recover_preferences with the original actual body and original sender
evidence. Do not reparse, fabricate a new incoming message or rewrite old receipt,
fingerprint, input body, checkpoint, name, consent or approval hash.

Alternatively handle now accepts recorded_step_id, a Python-only argument with
no HTTP or LLM-tool route. After the owner verifies the native/Gloo audit binding,
load the original received Message and original AgentStep decision. Use the
AgentStep ID, not the AgentRun ID. Preserve the existing sender authorization,
active session and native gate checks described in ADAPTIVE_SIGNUP_HANDOFF.md:

```python
session.info['verified_onboarding_source'] = {
    'incoming_id': original_incoming.id,
    'step_id': original_decision_step.id,
}
gate.reply_to_message_id = original_incoming.id
outcome = onboarding.handle(
    session, clock, gate, volunteer, original_incoming.body, gloo,
    recorded_step_id=original_decision_step.id,
)
```

The hook loads the persisted onboarding decision, checks the same actual phone,
body, received Message and current stage against the verified operator binding,
then applies normal validation and makes only the fresh Gloo composition call.
It logs a continuation referencing both original IDs without altering the old
extraction or counting another parser call. Missing/wrong binding holds without
an outgoing message. The owner's completed failed-attempt audit must remain;
use a separately locked, deduplicated continuation entry and supersede only the
earlier undelivered incorrect response before a native worker can claim it.
This hook does not bypass expiry, quiet hours, blocked_context, opt-out, sensitive
holds, approvals or final typography checks. Never create new reply proof or
adjust an incoming timestamp to bypass them. The live owner remains responsible
for current exact authorization and separate actual delivery verification.
