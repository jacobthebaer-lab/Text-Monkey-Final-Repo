# Google Voice church demo candidate

October 5, 2026 source candidate. Normal/production Google Voice remains held.
`GOOGLE_VOICE_DEMO_MODE=true` is a separate default-off mode using the existing
browser adapter, private connector and backend. It is not the disconnected public
`DEMO_MODE` preview. Demo authorization does not establish provider permission,
competition certification, cloud readiness or device delivery. This source build
used no real Google session, Gloo request, phone conversation or text submission.

## Church workflow

One dedicated configured account/Google Voice number represents the church. A
verified allowlisted superadmin can enter a new participant's exact mobile number
without startup edits or redeployment. Empty startup recipient scope is valid.
Registration saves a pending signup and a two-hour session, reconciles the same
durable scope with the connector, and sends nothing. An optional display label is
not the participant's identity or consent. Scope mismatch holds all delivery.

Compose the first invitation with Gloo, review its exact recipient/body, then
approve it. The existing name invitation asks first and last name and includes
exactly `Text STOP to stop.` before review. Only one initial invitation is allowed.
Actual first-and-last-name replies following the submitted invitation establish
name-reply opt-in provenance. A first-name-only reply prompts tailored recovery;
it does not grant consent. Subsequent Gloo texts omit the first-message STOP line.
No compulsory YES, verbal checkbox, imported opt-in or supplied name establishes
consent. Unavailable Gloo creates a hold; there is no fallback composition.

Ordinary participant replies need no session markers. Exact sender, recipient,
session time, origin and deduplication checks still apply. STOP immediately records
suppression without Gloo or automatic acknowledgement. Re-registration cannot
clear suppression, repeat the first invitation or discard unseen withdrawals.
Renewal checks only that previously registered participant's thread before
changing its session; reconnect keeps its durable activation cutoff. Withdrawals
that arrive after session/window expiry remain eligible at the next explicit
check, renewal check or enabled window. Ordinary expired replies are held. Existing
explicit START and original consent-provenance checks remain intact.

## Operator controls

Startup launches a private browser only with both dedicated demo mode and transport
enablement. It opens no Google page, checks no inbox and starts no timer. An
operator can prepare the separate private manual cloud sign-in surface, stopping
the connector before opening its persistent `/data/profile`. The human signs in
there, closes that browser, and restarts the connector. **Verify cloud sign-in**
then explicitly checks the existing profile's actual account email/Voice number,
without importing cookies or reading an inbox. It pauses outgoing work, clears
freshness and stops any window. A durable `manual-login.active` marker blocks
connector profile access until the operator explicitly stops the login service.
There is no automatic login or window resume. Personal-phone forwarding remains
off; use only the dedicated account. See the [manual cloud login runbook](CLOUD_BROWSER_LOGIN.md)
for the private sign-in surface and its SSH-only access.

Manual diagnostic steps are the default. **Refresh saved status** checks process
health only. **Check inbox once** reads only registered participant threads and
processes at most 100 new items. Historical content before each registration's
activation is skipped; an initial static test scope uses an explicit first-check
baseline. New STOP applies before retrying held non-control work. **Send this
reviewed text** binds the selected message and exact hash internally and submits
only that message. A verified inbox check must be less than 90 seconds old.

For a natural conversation, explicitly enable a **church demo window**, choosing
1–30 minutes and a visible submission budget of 1–1000 (default 100). Its separate
backend timer checks registered replies every 15 seconds and submits at most one
exact-approved queued text per tick. Every Gloo response still requires the
operator's individual exact-text approval. Approval then authorizes automatic
submission during that window; another inbox/send click is unnecessary. An admin
tab can be closed after approvals, but unapproved responses remain pending.

The window stops on pause, reconnect, expiry, restart, uncertainty, connection
failure or exhaustion of its chosen budget. Reservation of that budget and the
exact message claim are one durable transaction before browser preparation/click.
There is no sidecar timer, normal scheduling, fill, broad outreach, ranking,
Planning Center synchronization or automatic reconnect resume. Use one backend
worker and one connector replica. Intake, registration, session, pause and send
mutations serialize; concurrent changes return conflict.

All existing eligibility, consent, quiet-hour, freshness, exact approval, Gloo
composition receipt, no-em-dash and deduplication gates run before claim and again
after browser preparation. Final sender verification uses a second private page,
preserving the reviewed composer. Expiring deadlines reach the single browser
click. Reviewed bodies/hashes are never silently rewritten. Durable uncertainty
blocks repeat submissions, including a fresh message key for that participant.

`submitted` means a Google Voice UI acknowledgement, never delivered. Uncertain
submissions require manual investigation. Verify actual participant-device receipt
separately before claiming working live delivery.

## Isolated cloud setup after integration review

Follow [the persistent cloud deployment runbook](CLOUD_DEMO_DEPLOYMENT.md). The
existing Cloudflare frontend needs a persistent backend/connector host; no such
host has been verified or provisioned. A successful configuration check cannot
establish hosting access, account consent, real Gloo availability or delivery.

Use distinct Compose project volumes and private actual inputs outside Git, mode
0600. Never copy the Mac database, Messages credentials or an existing church's
browser profile. Required inputs include the dedicated account/number, Gloo,
verified Supabase configuration and an actual administrator in both admin and
superadmin allowlists. Generate and validate the explicit demo setup:

```sh
python3 tools/cloud_deploy.py init --demo --inputs-file /private/path/inputs.json --env-file /private/path/google-demo.env
python3 tools/cloud_deploy.py check --demo --require-config --env-file /private/path/google-demo.env
```

These commands do not start services, access a Google account or send. Without
`--demo`, the tools deliberately retain disconnected settings and reject enabled
demo flags. The generated setup enables `GOOGLE_VOICE_DEMO_MODE`,
`GOOGLE_VOICE_ENABLED`, `LIVE_SMS` and `ALLOW_TEXT_SIGNUP`, requires exact review,
and keeps generic automation, Mac bridge, profile sync and Planning Center off.
Compose passes transport enablement as `VOICE_ENABLED`. Start with empty recipient
scope, then register each pending participant in the authenticated UI. Preserve
private database and connector volumes on restart; never delete their receipts.

Sign in to the intended admin account, verify the manually authenticated cloud profile,
register a participant, compose/review the first invitation, and choose either
the separate manual steps or an explicit church window. For a natural signup,
the participant replies with first and last name; review each resulting Gloo
response. Reconnection/restart requires a new explicit window. Connector/browser
debugging ports, session cookies and credentials must remain private.

## Offline checks and limitations

Focused synthetic tests cover default no-browser holds, verified authentication,
dynamic scope partial-commit recovery/restart, actual name-reply consent and name
recovery, exact single dispatch, delivery gates, final account change, concurrent
pause/reconnect, expired-session STOP renewal, Gloo holds, durable budget and
uncertainty, bounded HTTP streaming, UI approval/registration and window controls.

Live Google DOM selectors, actual sender observability, login/session lifetime,
Gloo output, carrier receipt, hosting/tunnel routing and hard browser/OS crash
recovery remain unverified. Ambiguous identity, route, timestamp or composer state
holds. More than 100 visible elements in a thread or 100 inbound items in a scan
holds rather than consuming partial unknown history. Synthetic fixtures prove
code boundaries only. The retained normal/production hold is unchanged.
