# Google Voice connector: provider policy hold

Live Google Voice automation is permanently held in this build. Google's
[Voice Acceptable Use Policy](https://support.google.com/voice/answer/9230450)
prohibits sending messages through scripts and automatic messaging. The project
must follow provider rules, hackathon requirements and recipient opt-in. Google
Voice remains a manual option outside this automated transport.

Every shipped startup, including historical demo and signup configurations, serves authenticated health only. It never starts Chromium,
reads a Google account or profile, loads the message ledger, or schedules Google
polling. Completing identity verification, setting `VOICE_ENABLED=true`, importing
credentials, or changing backend flags cannot release the hold. There is no
configuration override.

Future automated delivery requires a separately approved, registered SMS
integration, such as the planned Twilio transport, with consent and the existing
Gloo, approval, eligibility, scheduling and quiet-hours protections. This connector
does not activate that future integration.

## Runtime configuration

Keep port 8765 private to the backend. Every endpoint requires
`Authorization: Bearer <VOICE_API_TOKEN>`. No public browser-debugging endpoint
or CORS access is provided.

| Variable | Current behavior |
| --- | --- |
| `VOICE_API_TOKEN` | Required private API secret, at least 32 characters |
| `PORT` | Private health-service port, default 8765 |
| `VOICE_ENABLED` | Legacy flag; strictly accepts `true` or `false`, defaults to `false`, and cannot enable automation |
| `GOOGLE_VOICE_DEMO_MODE`, `GOOGLE_VOICE_SIGNUP_ENABLED` | Historical flags; strict booleans that never release the provider hold |
| `GOOGLE_VOICE_TEST_SESSIONS` | Historical input, not parsed or activated by the held service |
| `VOICE_EXPECTED_EMAIL`, `VOICE_EXPECTED_NUMBER` | Legacy configuration only; no account lookup occurs |
| `GOOGLE_VOICE_ALLOWED_PHONES` | Legacy test allowlist validation only; no recipients are contacted |
| `VOICE_DATA_DIR`, `VOICE_BROWSER_PATH`, `VOICE_POLL_SECONDS` | Retained configuration; the shipped service does not open the profile/ledger, launch Chromium, or poll |

Do not export or import Google session cookies for this connector. Existing
private volumes remain untouched by any shipped startup. Never publish their
contents or place them in the repository.

## Shipped API

Authenticated `GET /health` returns HTTP 200:

```json
{
  "ready": false,
  "state": "policy_hold",
  "reason_code": "provider_policy_hold",
  "account_email": null,
  "number": null,
  "identity_verified": false,
  "expected_identity_match": false,
  "identity_fingerprint": null,
  "baseline_at": null,
  "inbound_cursor": "0",
  "delivery_verified": false
}
```

`GET /inbound` and `POST /session`, `/prepare`, `/send` return HTTP 503 with
`{"error":"provider_policy_hold"}` before request bodies are parsed. An absent
or incorrect API token returns HTTP 401. A healthy process does not indicate an
enabled provider or verified delivery.

## Offline verification only

Run `npm ci --ignore-scripts && npm test` for synthetic tests. Lower-level
`Connector`, `Store` and browser fixtures are retained for offline verification
of idempotency, uncertain outcomes, reservation recovery, recipient/body binding,
expiry, intake baselines and text-style guards. Production startup cannot reach
those fixtures. Their successful tests do not establish a compliant live Google
Voice transport.

The deployment image retains Node 22, Debian Chromium and Playwright for offline
container checks on Linux AMD64 and ARM64. `tools/cloud_voice_container_proof.sh`
uses synthetic `about:blank` content, no outbound network, and temporary private
volumes. Its restart fixture interrupts the connector after a durable reservation,
then verifies recovery in another container without retrying or clicking. It
closes Chromium cleanly first, so it does not prove hard browser-crash recovery.

No Google account, Google Voice message, Gloo round trip or carrier delivery is
verified by these offline checks. The retained DOM adapter has no approved live
activation path in this build.
