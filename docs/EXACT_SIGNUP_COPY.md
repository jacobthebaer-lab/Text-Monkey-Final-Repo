# Original signup copy correction

This supersedes the visible wording in CONCISE_SIGNUP.md for explicitly enabled
demo recipients. Source: original user widget submission at
2026-10-03T18:16:22.976Z, visualization-a10b0c41f52e8279 / outgoing_signup_texts.
The submitted consent and clarification messages were deleted. Four outgoing
messages remain; only recipient first-name substitution is allowed.

```text
Welcome to Text Monkey 🐵 Text us your FIRST and LAST name to sign up and receive scheduling texts. Message/data rates may apply🐒

Thanks [First Name]! What would you like to help with? 1: Greeter, 2: Usher, 3: Production, 4: Coffee, 5: Child Care. Reply with names or numbers, or "Anything". Some roles need coordinator clearance.

When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. You can also say "Flexible". 🐒 You can also tell me if you would like certain roles on certain dates or times. Just text me like you'd text a person 🐵

You're all set, [First Name]! We've saved your preferences. When a shift matches, we'll text you the details and ask if you can take it 🐵 Thanks for being willing to help out!
```

## Root runtime contract

Enable only the explicitly user-authorized recipient using the existing Policy
store: key `signup_exact_copy:<phone>`, value `{"value": true}`. This release does
not enable any recipients or mutate a live database. Keep the active named
Messages test session and existing reader provenance/permissions. Root is the
sole sender. Compose the opening through
`app.core.signup_copy.compose_welcome(session, clock, gloo, phone)` and the normal
SendGate with purpose `signup_reply`. Do not insert a fake inbound JOIN or YES.
WELCOME_REQUIRED is empty for the exact original opening;
LEGACY_WELCOME_REQUIRED retains the old path's requirements.

An actual incoming full-name reply advances immediately after a recorded exact
app invitation to the same phone, sent/submitted within 24 hours and the active
session. Code requires the real incoming Message ID supplied by inbound routing,
matching body/phone/status, newer than the invitation. Wrong-session, expired,
future, queued/uncertain and wrong-phone evidence is rejected. A intervening STOP
or phone opt-out also rejects activation. Native `submitted` is a native send
receipt, not proof of remote delivery; root verifies actual bytes/delivery
separately. The real name reply is recorded as
`consent_source=sms_name_reply_to_exact_invitation`, never synthetic YES consent.

A name received before the app invitation stays inactive and receives the exact
opening; a later matching real name reply can advance. Pending YES or role text
does not create name-reply consent and receives no deleted consent message.
Existing backend STOP remains authoritative; no visible STOP/HELP footer is
added to the four signup messages. Ambiguous preferences go to internal review
without the deleted clarification or an inaccurate completion message.

Gloo receives exact_copy=true and the full approved message; changed words,
punctuation or emojis fail validation without sending a substitute. Exact mode
overrides emoji spacing and administrator draft paraphrasing. All actual copy
still requires Gloo even when the optional general reply setting is disabled.

The five numbered roles use IDs 1–5 in the submitted order. Existing matching
roles and their clearance are preserved; conflicting IDs fail explicitly. Only
missing catalogue entries are created. New Production requires sound_training;
new Child Care requires background_check and child_safety_training plus
coordinator approval. No volunteer qualifications or shifts are granted.

Legacy recipients retain the previous name-plus-YES path. No services were
started, credentials read, real Gloo calls made, actual texts sent, remote pushes
or deployment performed here. This is a local source release for integration.

Focused tests: tests/test_exact_signup_copy.py and tests/test_concise_signup.py,
57 cases covering literal bodies, four-message routing, genuine name-only
replies, receipt/session failures, absent/changed Gloo, ambiguity, roles and STOP.
The combined signup, history, Messages, onboarding-copy and MVP regression
selection passed 157 cases. No runtime was activated for these checks.
