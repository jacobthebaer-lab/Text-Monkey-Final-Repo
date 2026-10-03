# Validation evidence (2026-10-03)

Isolated branch: `codex/google-voice-browser-bridge`, base `15a65b0`.
Environment: local macOS, Python 3.13, official Playwright 1.58.0,
paired headed Chromium 145.0.7632.6 (Playwright revision 1208).

- Full repository suite: **492 passed in 52.58s**, including **25 new bridge
  tests**. Synthetic browser test pages block all HTTP/HTTPS requests.
- Real repository FastAPI TestClient integration passed: natural bound-thread
  input, internal provenance, exact signed-in approval/hash, durable claim,
  preflight, one fixture browser click, observed bubble, submitted receipt,
  and STOP suppression. Gloo is replaced by deterministic test parsing.
- Read-only CLI run twice against local fixture with the same persistent
  profile and journal: both exit 0, `ready` health, outbound disabled.
- `pip check`: no broken requirements. Python compilation, shell syntax and
  Git whitespace checks passed.
- Initial test attempts encountered browser-download timing and sandbox
  loopback permissions; final runs completed with synthetic browser/socket
  access allowed. No security bypass, real Google access or real messaging.

Open checks: local Docker daemon unavailable; image build/Linux packages,
Xvfb/systemd run, VM memory needs, account login/session durability, actual
Voice DOM ID/time mapping, remote admin/backend reachability, real carrier
receipt and laptop-off proof are **not verified**.

No paid provisioning, new OAuth/host credentials, browser-session extraction,
personal messages, live sends, cloud deployment or parent/main-worker edits
were performed by this branch. The dependency lock and deployment examples
are deliverables for owner review, not evidence of remote execution.

## Routine-mode follow-up

Branch `codex/google-voice-routine`, based on integrated commit `277917c`.
Full Python suite: **522 passed in 64.54s**, including **29 new routine-mode
checks**. Headed HTML fixtures block network requests; backend tests use
fabricated records and local FastAPI TestClient only.

Routine evidence covers natural booking replies without per-text approvals,
internal session/history provenance, saved reminder/booking-change sources,
latest offer deadline refresh before click, immutable active deadlines,
exact/routine mode separation, raw or changed pending-row rejection, opt-out,
STOP before click, care holds, quiet hours, schedule and newer-input changes,
proof expiry across church timezones, budget/cooldown, restricted-role approvals,
automation disablement and uncertain-delivery holds. The refreshed-body browser
fixture observes one click; its dropped-send variant remains unknown and never
sends later recipient output. Existing competition review/transport tests pass.

Routine mode allows only outreach/reminder/booking_status/confirmation/
cancellation_ack/filled_thanks. Other purposes require the existing review path
or separate authorized workflow; this is not a generic arbitrary-send feature.
Default settings still select mock transport and leave the bridge disabled.
No deployed preview, shared runtime, account, real message, cloud host or
credential was activated or modified. Prior cloud/DOM/Linux blockers remain.
