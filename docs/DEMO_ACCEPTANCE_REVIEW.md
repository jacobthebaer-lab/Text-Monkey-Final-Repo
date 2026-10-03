# Independent demo acceptance review

Baseline: `9ed9d71203ed86989455e81a6fca2717a8017682` on `codex/complete-text-monkey`.
Review branch: `codex/demo-acceptance-review`. Implementation files were not edited by the reviewer; released owner commits were applied locally for acceptance.

## Resolved findings in released owner commits

The baseline findings below are now resolved when owner commits `d6f62bfb23d44e62cff385cfd610ea0b42506f74` (same-ID recovery/readiness) and `3092c1aee2c04e70c26e78329d801c9363590d71` (mandatory notification composition) are applied. The independent review module passes **9/9 with `--runxfail`**. Both new strict-xfail decorators were removed; no historical expectation was changed. Final account-editor and integrated-publication acceptance are recorded separately once released.

Private one-shot receipt independently checked without extracting recipient, phone, message body, native row ID or credentials: source baseline `9ed9d71`, one real Gloo call, `gloo-openai-gpt-5-mini`, app status submitted, native body exact-match and selected sender-line match, iMessage sent=1/delivered=1/error=0. Scheduling and inbound reading were off. This is proof of that designated test, not continuing transport/scheduler uptime. Sanitized receipt: `docs/evidence/demo-acceptance-review/designated-one-shot.json`.

## Verified baseline

Focused command: `python -m pytest tests/test_demo_acceptance_review.py tests/test_admin_text_settings.py tests/test_mac_messages.py -ra`.
Result: **62 passed, 2 strict expected failures**. These expected failures are new and specific to the issues below; they are separate from the existing full-suite quiet-hours expected failure. Running the new review module with `--runxfail` gives **7 passed, 2 failed**, reproducing the blocker.

New regression checks use fictional 202-555 contacts, fixture Gloo responses, an isolated SQLite database and the in-process durable Mac queue. They never run a connector or call Messages/Gloo externally.

- One-shot connection checks require composition even with optional signup composition disabled; queued status is distinct from a native delivery claim.
- Pausing enrollment, changing the saved recipient, opting out, or expiring its session after queueing prevents connector claims.
- STOP is accepted after the bounded session expires, and neither re-enrollment nor a check can override it.
- Gloo outage queues no fallback message. A fresh request after recovery can queue exactly one composed message.
- Existing focused tests verify account/recipient-bound retries, missing sessions, paused/changed recipients, opt-outs and connector delivery gates.

Public acceptance: the exact production alias https://text-monkey-demo.pages.dev/ matches all six baseline core assets through the independent HTTPS verifier. Backend writes are unavailable (404), configuration is disconnected with real delivery off, and the browser shows no incoming simulator, sample admin-send, or fake texting switch. Offline approval is disabled. Cached fictional history was retained and does not establish fresh composition or delivery. Evidence: `docs/evidence/demo-acceptance-review/public-baseline.json` and `public-settings-baseline.jpg`.

## Baseline finding: same-ID recovery with scheduling paused

`test_same_request_recovers_after_gloo_outage_without_scheduler` reproduces:

1. Enrolled consenting fictional admin, active recipient session and connector heartbeat; automation is disabled.
2. A connection check encounters Gloo failure and stores a pending notice without a message.
3. Gloo recovers and the stored two-minute retry time passes.
4. Retrying the same UUID still returns pending; no second Gloo call or message occurs.

`notifications.deliver` returns an existing pending row without dispatch unless an inbound reply is attached. The frontend keeps its UUID on a failed check, so this one-shot workflow cannot recover through its ordinary retry when the background scheduler is off. Starting a new UUID works but leaves the first pending notice, which could send an extra check if scheduling later resumes.

At baseline this regression was marked **strict xfail**. The integrated implementation fixes the recovery path; the marker is now removed and the regression passes. This does not establish ongoing runtime readiness.

## Baseline finding: coverage digest template fallback

`test_coverage_digest_requires_gloo_when_signup_composition_is_disabled` uses an unavailable fixture Gloo client, optional signup composition disabled, and a due staffing digest. The baseline sent the factual template through mock delivery with zero Gloo calls. The integrated dispatcher now requires Gloo for every notification; the temporary marker is removed and the regression passes. No actual text was sent by this regression.

## Runtime distinction

A read-only loopback snapshot during this review found: port 50335 reported Gloo configured (`aiReady=true`), a configured Mac transport but no recent connector heartbeat, and scheduling disabled. Port 58122 reported Gloo disconnected, no recent connector heartbeat, and scheduling disabled. Port 58125 returned `service=planning-center-webhook`, `status=ok`; it is not a texting service. A configured credential is not a fresh Gloo API success, and these transient signals do not prove native delivery. The separate authorized test owner handles the designated one-shot; this reviewer performs no duplicate send.

## Limits

This review performed no real send, changed no recipient/consent/runtime, renewed no session and inspected no private conversation. It establishes queue/gate behavior and public preview identity, not current laptop delivery or scheduler uptime. Actual connected acceptance requires separately authorized current Gloo audit and matched native delivery metadata. Final incoming implementation branches and publication need acceptance against their integrated commit; this report describes the baseline only.
