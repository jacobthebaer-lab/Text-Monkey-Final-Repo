# Clyde handoff

Use the existing private repository `clementsnc/planning-center-but-better`, branch `codex/complete-text-monkey`. The branch preserves source history and the resolved merge `64b6ff1`; no default-branch merge or repository replacement was performed. Jacob deferred the repository rename; it is not a current delivery blocker. Keep the same repository identity and private visibility.

## What is included

- Account-scoped church onboarding, confirmed-admin access, staged contact imports, branded portal and mobile setup.
- Gloo interpretation/composition, text signup, bounded private Messages sessions, exact review, opt-out/eligibility checks, offers, cancellation/replacement and care handoff.
- Admin mobile enrollment/pause, three-hour pre-event status and coverage digests, explicit connection blockers and saved-recipient connection-check endpoint with Gloo and recipient-session checks.
- Latest admin UI removal of the incoming simulator, sample-send/preview controls and fake pause/resume toggles. Static pages show disconnected status; real settings require the connected app.
- Synthetic monthly planning, availability, reminders, capacity flags, coordinator proposals, read-only calendar import, fixed eval cases and Agent Build Document.
- Scoped Planning Center Services open-needs importer, idempotent local links, signed/deduplicated webhook, explicit synthetic setup CLI and tests. It does not import people, manufacture consent or send texts.
- Cloudflare static packaging, publication verifier and sanitized public-portal evidence. The current receipt is `docs/evidence/portal-polish/current-release.json`; it verifies all six core files from source checkpoint `e009ff2` against the production alias. Older screenshots and receipts preserve prior UI history.
- Editable account-scoped onboarding copy, partial multiday/all-day availability, keyboard/mobile improvements, safe local preview/preflight, fictional staged-import package and reproducible three-hour status replay.
- Messages connector recovery for transient backend failures, durable native-attempt receipts and a local-only diagnostic.

## Start and validate

Follow the README for Python 3.11+ and isolated SQLite setup. Run the credential-free static preview with `python3 tools/texty_local_demo.py --port 58123` and open `/texty`. It has no real account, Gloo, backend writes or delivery. Use `pytest -ra` for backend checks and `npm test` under `web/texty` for frontend checks.

`tools/check_synthetic_gloo_signup.py` is optional: it uses your private Gloo configuration with a new in-memory database and mock delivery. It forcibly disables Messages and scheduling; its result is written under ignored generated reports. The committed `docs/evidence/synthetic-signup.json` records an earlier real-Gloo fictional signup and explicitly zero real texts.

Current integrated UI/backend validation: **635 backend tests passed, 1 explicit expected failure; 37 frontend tests passed**. Whitespace checks passed. The local-preview regression verifies all new editor/readiness/accessibility assets and rejected private routes/writes. Public deployment of this updated UI is being verified separately; the existing public receipt above describes the earlier release. The earlier 25-case real-Gloo run passed 23/25. Sensitive cancellation and restricted-role approval each passed a subsequent targeted 1/1 retest. These do not equal a fresh 25/25 live-model run. The immutable quiet-hours expectation remains a documented expected failure because the newer product permits immediate sender-initiated acknowledgment while proactive outreach holds.

The subsequent [acceptance review](DEMO_ACCEPTANCE_REVIEW.md) found same-request recovery after a Gloo outage and coverage digests bypassing required composition. Both implementation fixes are integrated; all nine acceptance checks pass and their temporary expected-failure markers are removed. The historical quiet-hours exception remains separate.

## Connected work still required

Jacob confirmed the admin destination separately. Real scheduled admin texting still needs verified account enrollment, consent, active private Mac recipient session, selected receiving line, Gloo credential, Messages connector and backend. A separately authorized one-shot test used real Gloo and the application Messages gate; the exact new native outgoing message was confirmed sent and delivered with no device error. Its private receipt stays outside Git. This test did not enable scheduling or change account enrollment, and does not establish ongoing uptime or carrier SMS delivery.

Connection-check request IDs are already bound to the original saved recipient. If the saved mobile changes, retrying the old UUID returns 409 without a new Gloo call; a fresh UUID can queue for the new saved consenting recipient. This is covered by the committed backend regression and client request-ID clearing.

The new legacy `/operations` controls, monthly availability collection and legacy reminders are **held for connected delivery** until their Gloo composition and exact-review integration is complete. Their mock workflows remain testable. Reviewed fill and three-hour event notifications retain their existing runtime gates.

Planning Center's dedicated receiver, multi-key signature verification and real API/webhook evidence are integrated. The owner verified a real update, duplicate replay and restored plan title through the tunnel: two imported events, ten open positions and zero people, assignments or messages. Created/destroyed subscriptions are configured but were not exercised. The Mac receiver and tunnel must remain running; this is not always-on hosting. Private credentials and runtime state remain outside Git. The importer needs reviewed schema migration before PostgreSQL use. Do not run its write CLI against an unapproved organization. Complete live-model regression and hackathon video/submission remain separate work.

## Privacy and history

Current source excludes private environment files, tokens, real phone records, databases, logs, device receipts and personal conversations. Detailed local originals were preserved outside Git; the repository has portable notes and synthetic fixtures.

Already-pushed historical commit `96e2894` contains developer filesystem paths, a personal test identity and native receipt metadata in **DEMO_COORDINATION.md**. That historical blob contains no E.164 phone values or quoted message bodies found during review. A test file also previously used an apparent real-phone-shaped overlay fixture; the current `tests/test_webhook.py` uses a fictional 202-555 number. This task preserves history and does not force-rewrite previously pushed commits. Keep the repository private; a history rewrite requires Jacob's authorization.
