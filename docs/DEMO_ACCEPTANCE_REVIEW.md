# Independent demo acceptance review

Baseline: `9ed9d71203ed86989455e81a6fca2717a8017682` on `codex/complete-text-monkey`.
Review branch: `codex/demo-acceptance-review`. Implementation files were not edited.

## Verified

Focused command: `python -m pytest tests/test_demo_acceptance_review.py tests/test_admin_text_settings.py tests/test_mac_messages.py -ra`.
Result: **62 passed, 2 strict expected failures**. These expected failures are new and specific to the issues below; they are separate from the existing full-suite quiet-hours expected failure. Running the new review module with `--runxfail` gives **7 passed, 2 failed**, reproducing the blocker.

New regression checks use fictional 202-555 contacts, fixture Gloo responses, an isolated SQLite database and the in-process durable Mac queue. They never run a connector or call Messages/Gloo externally.

- One-shot connection checks require composition even with optional signup composition disabled; queued status is distinct from a native delivery claim.
- Pausing enrollment, changing the saved recipient, opting out, or expiring its session after queueing prevents connector claims.
- STOP is accepted after the bounded session expires, and neither re-enrollment nor a check can override it.
- Gloo outage queues no fallback message. A fresh request after recovery can queue exactly one composed message.
- Existing focused tests verify account/recipient-bound retries, missing sessions, paused/changed recipients, opt-outs and connector delivery gates.

Public acceptance: the exact production alias https://text-monkey-demo.pages.dev/ matches all six baseline core assets through the independent HTTPS verifier. Backend writes are unavailable (404), configuration is disconnected with real delivery off, and the browser shows no incoming simulator, sample admin-send, or fake texting switch. Offline approval is disabled. Cached fictional history was retained and does not establish fresh composition or delivery. Evidence: `docs/evidence/demo-acceptance-review/public-baseline.json` and `public-settings-baseline.jpg`.

## Concrete blocker: same-ID recovery with scheduling paused

`test_same_request_recovers_after_gloo_outage_without_scheduler` reproduces:

1. Enrolled consenting fictional admin, active recipient session and connector heartbeat; automation is disabled.
2. A connection check encounters Gloo failure and stores a pending notice without a message.
3. Gloo recovers and the stored two-minute retry time passes.
4. Retrying the same UUID still returns pending; no second Gloo call or message occurs.

`notifications.deliver` returns an existing pending row without dispatch unless an inbound reply is attached. The frontend keeps its UUID on a failed check, so this one-shot workflow cannot recover through its ordinary retry when the background scheduler is off. Starting a new UUID works but leaves the first pending notice, which could send an extra check if scheduling later resumes.

The implementation owner has the reproduction. The regression is marked **strict xfail** so the condition remains visible and an implementation fix causes XPASS until the marker is removed. Do not interpret the focused command as unconditional connected acceptance.

## Concrete blocker: coverage digest template fallback

`test_coverage_digest_requires_gloo_when_signup_composition_is_disabled` uses an unavailable fixture Gloo client, optional signup composition disabled, and a due staffing digest. The baseline sends the factual template through mock delivery with zero Gloo calls. The notification dispatcher requires Gloo for pre-event/admin-check keys but not coverage-change keys. The implementation owners received this reproduction; the new regression is strict xfail pending enforcement of Gloo for this path. No actual text was sent.

## Runtime distinction

A read-only loopback snapshot during this review found: port 50335 reported Gloo configured (`aiReady=true`), a configured Mac transport but no recent connector heartbeat, and scheduling disabled. Port 58122 reported Gloo disconnected, no recent connector heartbeat, and scheduling disabled. Port 58125 returned `service=planning-center-webhook`, `status=ok`; it is not a texting service. A configured credential is not a fresh Gloo API success, and these transient signals do not prove native delivery. The separate authorized test owner handles the designated one-shot; this reviewer performs no duplicate send.

## Limits

This review performed no real send, changed no recipient/consent/runtime, renewed no session and inspected no private conversation. It establishes queue/gate behavior and public preview identity, not current laptop delivery or scheduler uptime. Actual connected acceptance requires separately authorized current Gloo audit and matched native delivery metadata. Final incoming implementation branches and publication need acceptance against their integrated commit; this report describes the baseline only.
