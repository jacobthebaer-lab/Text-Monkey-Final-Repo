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
