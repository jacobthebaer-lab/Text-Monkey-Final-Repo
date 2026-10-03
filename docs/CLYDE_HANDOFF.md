# Clyde handoff

Use the existing private repository `clementsnc/planning-center-but-better`, branch `codex/complete-text-monkey`. The branch preserves source history and the resolved merge `64b6ff1`; no default-branch merge or repository replacement was performed. Jacob requested the visible repository name `text-monkey`; its rename remains pending owner/admin access. Keep the same repository identity and private visibility.

## What is included

- Account-scoped church onboarding, confirmed-admin access, staged contact imports, branded portal and mobile setup.
- Gloo interpretation/composition, text signup, bounded private Messages sessions, exact review, opt-out/eligibility checks, offers, cancellation/replacement and care handoff.
- Admin mobile enrollment/pause, three-hour pre-event status and coverage digests, explicit connection blockers and saved-recipient connection-check endpoint with Gloo and recipient-session checks.
- Latest admin UI removal of the incoming simulator, sample-send/preview controls and fake pause/resume toggles. Static pages show disconnected status; real settings require the connected app.
- Synthetic monthly planning, availability, reminders, capacity flags, coordinator proposals, read-only calendar import, fixed eval cases and Agent Build Document.
- Scoped Planning Center Services open-needs importer, idempotent local links, signed/deduplicated webhook, explicit synthetic setup CLI and tests. It does not import people, manufacture consent or send texts.
- Cloudflare static packaging, publication verifier and sanitized public-portal evidence. Refer to the receipt date; it may precede the latest UI-removal publication.

## Start and validate

Follow the README for Python 3.11+ and isolated SQLite setup. Run the credential-free static preview with `python3 tools/texty_local_demo.py --port 58123` and open `/texty`. It has no real account, Gloo, backend writes or delivery. Use `pytest -ra` for backend checks and `npm test` under `web/texty` for frontend checks.

`tools/check_synthetic_gloo_signup.py` is optional: it uses your private Gloo configuration with a new in-memory database and mock delivery. It forcibly disables Messages and scheduling; its result is written under ignored generated reports. The committed `docs/evidence/synthetic-signup.json` records an earlier real-Gloo fictional signup and explicitly zero real texts.

Combined checkpoint validation: **556 backend tests passed, 1 explicit expected failure; 28 frontend tests passed**. JS syntax, whitespace and shareability checks are performed before push. The earlier 25-case real-Gloo run passed 23/25. Sensitive cancellation and restricted-role approval each passed a subsequent targeted 1/1 retest. These do not equal a fresh 25/25 live-model run. The immutable quiet-hours expectation remains a documented expected failure because the newer product permits immediate sender-initiated acknowledgment while proactive outreach holds.

## Connected work still required

Real admin texting needs the intended saved admin mobile number, explicit consent/enrollment, active private Mac recipient session, selected receiving line, Gloo credential, Messages connector and backend. No admin destination was guessed, no text sent, no scheduler enabled and no runtime restarted by this Git task. Past device receipts cannot establish current runtime or carrier SMS delivery.

A small follow-up is still being finalized by the admin owner: a connection-check request ID must stay bound to its original saved recipient if the mobile changes after an uncertain response. The current checkpoint deduplicates the request, but do not treat an earlier recipient’s receipt as proof of delivery to a new mobile.

The new legacy `/operations` controls, monthly availability collection and legacy reminders are **held for connected delivery** until their Gloo composition and exact-review integration is complete. Their mock workflows remain testable. Reviewed fill and three-hour event notifications retain their existing runtime gates.

Planning Center live account choice, private token, API-created synthetic data, public backend tunnel and registered webhook/delivery remain unverified. The importer needs reviewed schema migration before PostgreSQL use. Do not run its write CLI against an unapproved organization. Always-on backend hosting, complete live-model regression and hackathon video/submission remain separate work.

## Privacy and history

Current source excludes private environment files, tokens, real phone records, databases, logs, device receipts and personal conversations. Detailed local originals were preserved outside Git; the repository has portable notes and synthetic fixtures.

Already-pushed historical commit `96e2894` contains developer filesystem paths, a personal test identity and native receipt metadata in **DEMO_COORDINATION.md**. That historical blob contains no E.164 phone values or quoted message bodies found during review. A test file also previously used an apparent real-phone-shaped overlay fixture; the current `tests/test_webhook.py` uses a fictional 202-555 number. This task preserves history and does not force-rewrite previously pushed commits. Keep the repository private; a history rewrite requires Jacob's authorization.
