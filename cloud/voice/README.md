# Cloud Google Voice connector

Experimental, private sidecar for the isolated cloud Text Monkey test build.
The browser runs on the cloud host and does not need the coordinator's Mac or
an open admin page. This implementation has synthetic tests; it has **not**
passed Google account activation, live Google UI, or carrier delivery tests.

## Runtime

- Node 22 and Debian Chromium, with Playwright controlling an isolated persistent
  profile. The Dockerfile supports Debian's amd64 and arm64 Chromium packages.
- Run **one replica** per persistent `/data` volume. Chromium's profile lock
  prevents a second process from opening that same profile. Do not use shared
  network filesystems or multiple independent volumes for the same account.
- Keep port 8765 private to the application backend. All endpoints, including
  health, require `Authorization: Bearer <VOICE_API_TOKEN>`. No CORS or public
  browser-debugging endpoint is provided.
- Mount `/data` durably. It contains Google session credentials, incoming test
  message content, deduplication records and send reservations. It must remain
  private to the service account and outside the repository and public backups.
- Chromium manages session-cookie rotation in its persistent profile. Session
  expiry, failed identity checks or changed selectors hold processing.

| Variable | Meaning |
| --- | --- |
| `VOICE_API_TOKEN` | Private shared API secret, at least 32 characters |
| `VOICE_EXPECTED_EMAIL` | Exact selected Google account |
| `VOICE_EXPECTED_NUMBER` | Exact claimed Google Voice number in +1 E164 form |
| `GOOGLE_VOICE_ALLOWED_PHONES` | Comma-separated exact test recipients, max 20; empty means none |
| `VOICE_DATA_DIR` | Persistent state directory, default `/data` |
| `VOICE_BROWSER_PATH` | Chromium executable; Docker sets `/usr/bin/chromium` |
| `VOICE_POLL_SECONDS` | Poll interval, default 30, permitted 15–3600 |
| `PORT` | Private HTTP port, default 8765 |

The backend independently enforces test-session duration, confirmation, consent,
Gloo composition, quiet hours, scheduling and recipient eligibility. The
connector's allowlist is an additional restriction, not a replacement for them.

## API contract

`GET /health` returns:

```json
{
  "ready": false,
  "state": "reconnect_required",
  "reason_code": "session_not_verified",
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

When verified, account email and number are masked. `identity_fingerprint` is
SHA-256 of the **observed** lowercase email, a newline and the observed E164
Voice number. Readiness requires those observed values to match configuration;
the backend also compares this fingerprint against its own configuration.

`POST /session` accepts `{ "cookies": [...] }`. The array uses Playwright cookie
objects with `name`, `value`, `domain`, `path: "/"`, and optional `expires`,
`httpOnly`, `sameSite`. Only Google account/Voice domains are accepted; cookies
are forced secure. Use a dedicated single-account Google session, exported
only after the user completes Google verification. Import through the
authenticated superadmin backend over HTTPS, never a committed file or shell
argument. The endpoint verifies observed identity and clears an incorrect
session. It returns `initializing` until an inbound baseline completes. Cookie
export/transfer and live account connection have not been performed by this
build.

`POST /send` accepts:

```json
{"idempotency_key":"test-session:message-001","to":"+12025550102","body":"Synthetic example","not_after":"2026-10-03T18:00:30Z"}
```

The status is `submitted`, `uncertain` or `rejected`, with `reason_code` where
available. `submitted` means the outgoing text appeared in Google Voice's UI
and its composer cleared. **It does not prove receipt by the carrier or phone.**
The connector durably reserves the idempotency key before the one send click.
Duplicate keys return the recorded result; changed content with the same key
returns HTTP 409. A crash across the send boundary becomes `uncertain` after
restart. An uncertain send must be reviewed, never retried under a fresh key.
The required timezone-aware `not_after` authorization deadline is bound into
the idempotency digest and checked before browser preparation and immediately
before the send click. The backend chooses the earliest approval, test-session,
queue-age or quiet-hours deadline. An expired authorization is rejected without
a click and cannot be extended by changing the same idempotency key.

`GET /inbound?cursor=0` returns up to 100 messages and the next decimal-string
cursor. Each message has `id`, `phone`, `body` and an aware ISO `received_at`.
The first successful scan establishes an activation baseline without emitting
history. Absolute timestamps discard late-rendered older history. Durable IDs
prevent replay after restarts. Only allowlisted one-to-one thread bodies are
read; opening those test conversations may mark them read in Google Voice.
No group/MMS ingestion is implemented.

Errors are `{ "error": "reason_code" }`. HTTP errors and logs never include
underlying browser exception text, request bodies, cookie values or message
content. The API is intentionally inaccessible without the private token.

## Validation and known compatibility gate

Run `npm ci --ignore-scripts && npm test` for synthetic tests. They cover
idempotency across concurrent requests/restarts, crash ambiguity, historical
baseline suppression, allowlist enforcement, identity mismatch, timestamp
requirements, cookie validation, authorization and redaction. These tests do
not open Google or use a real browser profile.

The Google DOM adapter is isolated in `browser.mjs`. Selector facts were checked
against the public [googlevoice-mcp selector definitions](https://github.com/zhuqf/googlevoice-mcp/blob/main/selectors.ts),
which report live verification in April 2026. No implementation from the AGPL
mautrix bridge was copied. Google's documented UI flow is available in its
[text messaging instructions](https://support.google.com/voice/answer/115116).

Before live use, validate the selected account's visible email, settings Voice
number section, one-to-one thread IDs, incoming/outgoing DOM direction and
absolute message timestamp attributes. Relative-only timestamps and unknown
DOM shapes fail closed with explicit health reasons. Google may reject a
headless login/session; successful cloud authentication is an acceptance gate.
The app must show pending verification until a consenting test recipient
confirms actual receipt and a reply completes the round trip.

Clearing the state volume destroys the deduplication ledger. Never do that to
retry an uncertain send. Routine restarts preserve the same volume and baseline.
