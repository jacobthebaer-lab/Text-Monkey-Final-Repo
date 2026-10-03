# Concise signup changes, October 3, 2026

Jacob edited the messages to consolidate signup, delete the separate consent
message and repeated availability question, thank volunteers at completion,
and use monkey and occasional other friendly emoji sparingly.

The normal path is four outgoing texts:

1. One welcome asks for FIRST and LAST name plus YES, with frequency/rate and
   STOP/HELP information. JOIN is optional when answering that introduction.
2. Name and affirmative consent together move directly to the personalized
   roles question using the real saved catalogue and the Anything alias.
3. One availability question accepts a natural answer and extra preferences.
4. One confirmation saves preferences and thanks the volunteer, without booking
   a shift or asking for an RSVP.

Actual sender text must exactly match a full name plus YES/Y, either at the
start or end. Gloo extracts identity but does not decide consent. A name alone,
incidental/quoted YES, model claims of consent and STOP cannot activate texting.
A name alone still receives a targeted YES request because consent is missing;
an already-disclosed frequency/rate notice is not repeated. Existing YES, STOP,
START and HELP behavior remains. All signup replies, including pending consent
help and legacy completion, now require Gloo, with no template fallback.

Newly started onboarding marks `signup_minimal_texts=true`. Once days/dates or
explicit flexibility are understood, frequency can remain unknown; the app does
not ask an extra question or invent `max_per_month`. Code records
`availability_frequency_known=false` and leaves the existing scheduler cap in
effect. An explicit frequency is still validated and saved. Legacy in-progress
profiles without the concise marker retain the earlier targeted follow-ups.
Ambiguous or unsafe input still receives only a necessary targeted question or
human review. The removed repeated generic question is blank in default editor
copy; blank clarification wording is allowed and never sent as a message.

Gloo can choose at most one permitted emoji per light signup reply. Any emoji
in either of the previous two outgoing texts suppresses decoration; the next
eligible reply excludes the previous emoji. Welcome and completion can use
light monkey or simple friendly emoji; targeted consent and clarification
follow-ups are plain. Cancellations, care and other transactional replies keep
their existing plain style. No literal suffix is automatically appended.

Extra role/date/time preferences remain in the existing raw availability note.
Structured weekday, Sunday service hour, date and frequency constraints retain
their code validation. This change does not invent a new role catalogue or claim
new automatic support for separate time windows per role; detailed conditional
preferences still require coordinator interpretation. The roles list is dynamic,
so Coffee or Child Care appear only when configured in that church catalogue.

No real Gloo calls, real texts, activation of services, credentials, database
modifications, remote push or deployment was performed here. The private
canonical repository is `jacobthebaer-lab/text-monkey`; Git integration and any
user-authorized fresh Messages signup are owned by the Git handoff chat.

Worktree: `/Users/jacob/.codex/worktrees/onboarding-message-settings/Text Monkey`

Branch: `codex/concise-signup-flow`

Base: `e14cc0d` (completed integration and verified release).

Changed files:

```text
PROMPTS_CHANGELOG.md
app/core/inbound.py
app/core/onboarding.py
app/core/onboarding_copy.py
app/core/signup.py
app/core/signup_copy.py
app/core/signup_responder.py
docs/CONCISE_SIGNUP.md
prompts/onboarding.md
prompts/signup.md
prompts/signup_reply.md
tests/test_concise_signup.py
tests/test_copy_history.py
tests/test_mac_messages.py
tests/test_mvp_flows.py
tests/test_onboarding_copy.py
tests/test_texty.py
web/texty/public/onboarding-copy-defaults.json
web/texty/public/onboarding-copy.js
```

Meaningful checks: `tests/test_concise_signup.py` covers the four-message path,
name/YES combinations, unsupplied frequency, date exclusions, absence of
assignment/clearance changes, incidental YES, STOP, Gloo outages on all signup
branches, friendly emoji limits and spacing. Updated historical fixtures now
handle composition JSON independently from parser JSON. Full Python suite:
679 passed, 1 existing expected failure (the immutable quiet-hours eval).
Full Node suite: 37 passed. The new focused concise-flow suite has 27 cases.
The exact release commit is recorded in the chat handoff.
