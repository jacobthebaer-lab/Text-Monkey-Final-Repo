# Cloud texting, authorized October 3, 2026

Jacob requested real texting without keeping the Mac on. This optional transport
uses Twilio on an always-on Python server, with Gloo for interpretation and
composition. Mac Messages remains available as a separate transport. Never run
both transports against the same live conversations or start duplicate coordinators.

## Current checkpoint

Implementation and synthetic tests are prepared. No Twilio account, phone number,
paid service or real delivery has been activated by this change. The separate
Codex cloud coding environment is for development; it is not an always-on server.

`render.yaml` prepares one paid Python web service with a persistent SQLite disk,
one process, manual deploys, and texting/scheduling disabled initially. It requires
account access and approval of the actual hosting and SMS costs before creation.
Render's [Blueprint reference](https://render.com/docs/blueprint-spec) defines these
settings; [persistent disks](https://render.com/docs/disks) provide storage across
restarts. Do not use a sleeping free service or an ephemeral SQLite filesystem.
An existing always-on Python host can use the same start command and settings.

## Deployment and activation

1. Review and merge the cloud SMS PR into `codex/complete-text-monkey`. Use only
   `jacobthebaer-lab/text-monkey`. Select that branch when creating the Blueprint.
2. Deploy with `SMS_PROVIDER=mock`, `LIVE_SMS=false`, `AUTOMATION_ENABLED=false`,
   `DEMO_MODE=false`, `MAC_BRIDGE_ENABLED=false`. Keep a single service instance
   and a single Uvicorn worker. Supply private Gloo and existing Supabase login
   settings through the host's secret settings, never through Git or chat.
3. Connect the existing Cloudflare dashboard proxy to this backend using
   `BACKEND_URL` and a private `BACKEND_BRIDGE_KEY` shared with the backend.
   Direct remote `/api` requests deliberately require that bridge header plus
   the signed-in Supabase user. Do not expose or remove this requirement.
   Set `ADMIN_SITE_URL` to the intended dashboard so auth email redirects work.
4. Retrieve the saved church settings and recipients from the existing admin
   console. Arrange a reviewed import/migration to the new store; do not seed
   fictional data into production or silently reuse the demo database.
5. Use an SMS-capable Twilio number approved for the intended sending region.
   Complete any provider-required activation. Store `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` privately on the host. Follow the
   [official Twilio setup](https://www.twilio.com/docs/messaging/quickstart).
6. Set `PUBLIC_BASE_URL` to the Python server's HTTPS origin, without a path,
   credentials, query or fragment. Configure the number's incoming-message
   webhook as POST `PUBLIC_BASE_URL/sms/inbound`. Outgoing messages set their
   own `PUBLIC_BASE_URL/sms/status?message_id=...` callback. Point both routes
   directly at the Python backend, not the static preview or Cloudflare worker.
   [Twilio signature validation](https://www.twilio.com/docs/usage/webhooks/webhooks-security)
   checks the exact public URL, account, number and message identifiers.
7. After the specific account/number is authorized, set `SMS_PROVIDER=twilio`,
   `LIVE_SMS=true`, `GLOO_SIGNUP_REPLIES=true`, and redeploy manually. Startup
   refuses live mode without Gloo, HTTPS, a strong admin password and Mac mode off.
   Leave scheduling and text signup disabled until the saved settings and intended
   recipient scope are verified. `COMPETITION_CONFIRMATION_REQUIRED=true` holds
   each exact text and applicable record change for dashboard review if desired.
8. With an explicitly authorized consenting test recipient, verify incoming
   interpretation through real Gloo and one real outbound carrier delivery.
   Queue/submitted status and mock tests cannot establish delivery. Then verify
   the saved three-hour admin update on a controlled event before enabling
   `AUTOMATION_ENABLED=true` for ongoing coordination.

Keep secrets separate from Git. Maintain backups of the persistent store.
The portable Postgres alternative requires the existing reviewed `texty` and
admin setup migrations; this Blueprint does not migrate or alter Supabase.

## Delivery behavior and recovery

The send gate composes canonical/manual text through Gloo with exact-word
validation, or receives text already composed by the Gloo workflows. A failed or
changed Gloo response holds the text; it never triggers a substitute. The gate
commits an immutable outbox reservation with the policy transaction. The cloud
worker rechecks consent, care holds, quiet hours, expiry, exact reviews and offer
eligibility, then commits a dispatch claim before contacting Twilio.

Incoming signed webhooks are durably deduplicated by carrier SID and acknowledged
before interpretation; worker retries roll back partial records and reservations.
Signed delivery callbacks distinguish queued/submitted, sent, delivered and
failed. Older callbacks cannot regress a terminal result. Provider queue acceptance
is not delivery proof.

A timeout or interrupted dispatch becomes `uncertain` and is never automatically
resent. A failed or uncertain offer creates a coordinator reconciliation task.
Before any manual resend, compare the carrier log to the reservation's attempt
time, exact number and body. Preserve the original receipt; do not reset its state
to queued or guess that a timeout meant nothing was sent. There is no automatic
carrier resend or dashboard reconciliation button in this checkpoint.

Restart recovery assumes exactly one process owns the scheduler. Do not scale
this service to multiple instances/workers without implementing leader ownership.
Legacy bulk operation controls remain held for connected transports. This change
does not replace the separately reviewed reminders or Planning Center work.

## Checks

`tests/test_cloud_sms.py` uses only synthetic Gloo/carrier doubles. It covers
committed delivery, rollback, duplicate callbacks/ingress, mutable consent/content,
quiet hours, expiry, exact review, timeout/restart recovery and cloud worker startup.
Run the full Python and frontend suites with SMS mocked as documented in README.
