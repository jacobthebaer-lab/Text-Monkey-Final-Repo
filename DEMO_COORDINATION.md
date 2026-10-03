# Text Monkey demo handoff

Checked October 3, 2026, 10:35 AM America/Denver. Demo only; no production or customer outreach work is authorized here.

## Verified connected texting

Gloo interprets/composes; the application's SendGate queues approved-purpose output; its Mac connector transmits automatically. The valid evidence uses real `GlooClient` requests, not ScriptedAI, mocked responses, or agent UI texting.

The isolated SQLite test started with zero volunteers and retains Noah's existing profile elsewhere. It uses ordinary replies (`input_mode: natural`), one selected Noah conversation, the 908 sender line, iMessage, a fresh checkpoint, and an expiring test session. Personal history is skipped. Session IDs travel internally; Noah types no markers or codes.

| App evidence | Gloo audit | Native delivery proof |
| --- | --- | --- |
| Outbound message 2, correction allowing normal replies | Run 2, `signup_reply`, `reply_composed`, 1,157 input / 1,060 output tokens | Messages row 296796, exact body matches Gloo output; sent=1, delivered=1, error=0; sender line verified |
| Ordinary inbound message 3, received at 10:34:46 AM | Run 3, `signup`, `not_signup`, 1,625 input / 423 output tokens | Actual inbound received through connector; no fabricated volunteer response |
| Automatic outbound message 4 at 10:34:56 AM | Run 4, `signup_reply`, `reply_composed`, 1,162 input / 620 output tokens | Messages row 296798, exact body match; sent=1, delivered=1, error=0; sender line verified |

All runs above used `gloo-openai-gpt-5-mini`. Profile count remains zero: normal ingress, Gloo interpretation/composition, and automatic delivery are verified; name/consent/preferences completion is still pending.

The first welcome was manually sent through agent UI and is **not** valid app-test evidence. A subsequent real Gloo welcome requested markers; that obsolete test instruction was corrected. Do not use either to claim a normal-user signup demonstration.

Evidence: [DEMO_EVIDENCE.json](</Users/jacob/Documents/ChatGPT/Text Monkey/DEMO_EVIDENCE.json>). Detailed initial receipt: [latest-gloo-signup-test.json](</Users/jacob/Documents/ChatGPT/Text Monkey/latest-gloo-signup-test.json>). Private credentials/configuration and delivery claim tokens must not be copied into handoffs or public demos.

## Current session

- Expires October 3 at **10:51:41 AM America/Denver**. Connector wrapper stops automatically at expiry; no renewal is authorized by this handoff.
- At the check, backend PID 40332 and bounded connector PIDs 44580/44581 were running. Backend is loopback-only at port 50335; its test session rejects later ordinary input after expiry.
- Backend scheduling is off. Only the separately authorized Noah signup demonstration is active. This coordination request caused no additional texts.

## Demonstrate normal signup

Noah replies with his first and last name. Gloo then interprets it and composes the consent question. He replies **YES**, then a serving interest such as **greeter**, then ordinary availability such as **Sundays at 9am, twice a month**. Follow the questions actually returned; do not fabricate his consent, replies, bookings, or completed profile.

The isolated church has Greeter, Usher, and Production roles. Observe `agent_runs`, app message statuses, connector acknowledgments, and saved profile stage/preferences in the isolated database referenced by the receipt. A submitted acknowledgment alone does not prove delivery; use matched native delivery metadata when making that claim.

The natural-reader change and focused connector, session-privacy, and signup-responder tests passed. Local source changes are uncommitted; preserve other workers' unrelated edits. Source: `/Users/jacob/Documents/Codex/2026-10-02/church-text-integration`.

For a reliable presentation, use synthetic dashboard flows and clearly label them simulated. Connected texting currently depends on this Mac, Messages, the selected line, and local processes. Verified transport here is **iMessage**, not carrier SMS or laptop-independent cloud delivery. No new hosting, production rollout, or extra outreach is needed for the demo.

## Portal polish and Cloudflare publication

Owner: Find form details chat (`01a10296-229e-7ef3-bbc2-e68b8dcc08ce`).
Target: https://text-monkey-demo.pages.dev/ (Cloudflare Pages project `text-monkey-demo`, production branch `demo`).
Source: `/Users/jacob/Documents/Codex/2026-10-02/church-text-integration/web/texty/public`.

Portal polish is implemented: context header and explicit preview status, roster and shift summary counts, calmer table/form layout, compact sample-booking disclosure, church/settings cards, responsive navigation and setup spacing. The earlier Email/Password placeholder fix remains included. All 26 frontend tests, module syntax checks and whitespace checks pass.

Publication pending the Improve admin console clarity handoff for shared admin mobile/status changes. Existing backend and shared app edits are preserved. The Pages upload uses `tools/build_cloudflare_demo.py`; it contains only public assets and browser-local sample rules, without connected accounts, real AI or message delivery. Deployment proof and live-browser checks will be recorded here after publication.

## Master coordinator scope and Git handoff

Jacob explicitly appointed Coordinate Text Monkey demo as master coordinator and requested a demo rather than production customer readiness. Find form details owns final Cloudflare publication; Improve admin console clarity hands off mobile-number/status changes before publication. Build and push to GitHub owns integration of all completed work and the remote push for Clyde, with repository URL, branch and commit verification. Preserve all other handoff entries. Keep private credentials, real phone records, raw test conversations, local databases and logs out of Git; use sanitized evidence and reproducible synthetic fixtures.

Planning Center setup is requested for Cedar Hills Community Church. Its $0 signup page is open in Chrome; mobile verification and Terms acceptance precede account details. User handoff is pending for verification and any new password. The coordinator has not yet verified an account, API token, live sync or webhook. Preserve the logged-in session once signup completes; credentials belong only in ignored private configuration. Do not open duplicate signup sessions.
