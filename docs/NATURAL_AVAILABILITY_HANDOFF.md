# Natural availability signup handoff

Normal answers such as “Sundays and Wednesdays all day” now save both days and
the all-day preference. When frequency is missing, Gloo composes a short
frequency question instead of repeating the entire availability question.
Later answers and corrections retain the sender's validated facts.

## Integration

- Worktree: `/Users/jacob/.codex/worktrees/natural-availability-signup/Text Monkey`
- Base: `9ed9d71203ed86989455e81a6fca2717a8017682`
- Branch: `codex/natural-availability-signup`
- Repository: `https://github.com/clementsnc/planning-center-but-better`
- Commit: recorded in the final handoff receipt; local only, no push.
- Integration/push remains with the coordinator's GitHub integration owner.

## Changed files

- `app/core/onboarding.py`: sender-scoped partial snapshot, schema validation,
  targeted clarification, correction-aware date persistence, required Gloo
  composition, and internal handling of interpreter outages. FLEXIBLE/SKIP
  retains explicit unavailable dates.
- `prompts/onboarding.md`: merged facts, partial answers, weekday/all-day
  interpretation, missing frequency, and correction rules.
- `tests/test_natural_availability.py`: multi-turn state, corrections, supplied
  frequency, malformed extraction, outage, consent, clearance and booking guards.
- `tests/test_mvp_flows.py`, `tests/test_setup_regressions.py`: synthetic Gloo
  fixtures now distinguish reply composition from interpretation.
- `tools/verify_natural_availability.py`: repeatable real Gloo verification with
  fictional identities and in-memory simulated SMS transport.
- `docs/evidence/natural-availability-real-gloo.json`: fictional conversations,
  Gloo call/token audits and assertions.
- `docs/NATURAL_AVAILABILITY_HANDOFF.md`: this receipt.

## Verification

Full Python regression suite: **569 passed, 1 expected failure**, 14.74 seconds.
Command: `python -m pytest -o addopts='' -q --tb=short`.
`git diff --check` passed.

Real Gloo model `gloo-openai-gpt-5-mini`, 30 calls across three passing scenarios:

1. Sunday/Wednesday all day, Thursday replacing Wednesday, frequency followup.
2. Multiple weekdays/all day/frequency supplied together.
3. Frequency first, then multiple weekdays/all day.

The retained weekday, all-day and frequency values were checked after signup.
YES consent was required. No qualifications, staff authority or assignments
were created. Every transport receipt was MOCK; **zero real messages sent**.
Credentials were read from an existing private environment file and were not
copied into the worktree or evidence.

## Limits

This verifies actual Gloo interpretation/composition and the application's
signup routing/state with simulated delivery. It does not verify live device
delivery, renew Noah's session, contact Noah, introduce a provider or deploy
changes. Configurable copy/UI/account mapping and the baseline occasional,
varied monkey emoji behavior were preserved. The final FLEXIBLE/SKIP retention
guard was covered by the full synthetic suite after the real Gloo run; the
three real scenarios do not use those commands.
