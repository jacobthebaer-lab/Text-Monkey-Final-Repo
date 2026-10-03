# Planning Center synthetic demo integration

The Services importer reads selected service types, plans, **service** PlanTimes,
teams and NeededPositions. Each service time becomes an event, with UTC-aware
start/end values; each unfilled NeededPosition becomes an open shift. It does
not import people or existing scheduled team members, so it is an **open-needs
schedule bridge**, not a complete Planning Center roster/coverage mirror.
It does not infer SMS consent, send messages, create fill requests, enable
scheduling, or change the Gloo/Mac Messages path.

## Private setup

Create a personal access token in the verified demo organization at
https://api.planningcenteronline.com/personal_access_tokens. Token creation
requires action-time approval under the browser confirmation policy. Store
its Application ID and Secret only in the backend's ignored `.env` (mode 600),
never in Git, screenshots, logs, handoffs, frontend code or Cloudflare assets.

```dotenv
PCO_APP_ID=
PCO_SECRET=
PCO_ORGANIZATION_ID=
PCO_SERVICE_TYPE_IDS=
PCO_WEBHOOK_SECRET=
PCO_WEBHOOK_SECRETS=
```

Services requests pin API version `2018-11-01`; webhook API version is
`2022-10-20`. The client uses HTTP Basic auth and follows only same-origin HTTPS
pagination links. Errors omit raw responses, URLs containing credentials and
authentication headers.

From the backend source directory, with its Python dependencies available:

```sh
python tools/planning_center_demo.py inspect --env-file .env --expected-org ORG_ID
python tools/planning_center_demo.py seed --env-file .env --expected-org ORG_ID --write-synthetic
```

`ORG_ID` is the numeric organization ID verified in Planning Center. Seed creates
or reuses `Text Monkey Synthetic Demo`, three empty teams (Greeters, Ushers,
Production), and two private Sunday plans intended for 9 AM–10 AM America/Denver.
The saved-time discrepancy was repaired as described below. It disables
plan reminders and schedules no people. It verifies saved objects with fresh
GET requests. If this is the first service type in a new account and the API
returns 500, create `Text Monkey Synthetic Demo` once through Services onboarding
and rerun seed. The seed reuses that empty onboarding plan and service time
instead of creating a duplicate. Use only the synthetic demo organization; seed is a real API write.

Save the returned service type ID in `PCO_SERVICE_TYPE_IDS`, and matching org ID
in `PCO_ORGANIZATION_ID`. Then explicitly select an isolated local database:

```sh
python tools/planning_center_demo.py sync --env-file .env --expected-org ORG_ID --database sqlite:///./planning-center-demo.db
```

The importer never uses a default production database from `.env` through this
CLI. On each empty demo team, use Services → Teams → Add position to create
Greeter, Usher, and Production Operator respectively. TeamPosition creation is
not exposed in the documented public API. Rerun seed: it creates explicit
synthetic open needs of 2 Greeters, 2 Ushers, and 1 Production Operator per plan,
using the verified `team_position_id`. Plan-wide teams forbid `time_id` when
creating needed positions. Seed returns any teams still needing this UI step;
it never creates people or sends invitations. The application retains local staffing history when a remote need shrinks.
Unoccupied removed remote needs are pruned; vanished remote service times are
cancelled only if no local assignment history exists. These local cancellations
never cancel Planning Center data.

## Webhook

The backend includes `POST /integrations/planning-center/webhook`. Set
`PCO_WEBHOOK_SECRET` to the subscription's authenticity secret in ignored `.env`.
Planning Center creates a separate subscription/signing key for each selected
event. Put all active keys in comma-separated `PCO_WEBHOOK_SECRETS`; the single
`PCO_WEBHOOK_SECRET` remains supported for existing single-event setups. It
validates Planning Center's hex HMAC-SHA256 `X-PCO-Webhooks-Authenticity` header
against the raw request body, enforces the selected organization, deduplicates
EventDelivery IDs transactionally, and refreshes only the allowlisted service
types. Successful relevant events return 200; API/DB failures return 503 so
Planning Center retries. No text or background outreach runs from this route.
Unrelated event types are acknowledged without reading their payload or people.

For a live subscription, use the **existing verified HTTPS ngrok backend origin**
plus the route above. The current Services webhook UI exposes Plan created/updated/destroyed events;
select only those three. PlanTime and NeededPosition event selectors are not
available in the current UI. Each accepted Plan event refreshes the scoped
service-time and open-need data from the API.
Do not register against a stale or unavailable tunnel: inspect ngrok's local
`http://127.0.0.1:4040/api/tunnels`, verify the public `/healthz`, verify that the
running backend has this route and the secret, then register. The webhook secret
and selected scope must be configured before any delivery test. Verify an actual
Planning Center delivery ID, 200 server response, durable receipt and imported
change separately from signed synthetic requests.

Jacob subsequently authorized starting ngrok and completed its sign-in. The
existing personal ngrok authtoken is stored in native private configuration;
no new ngrok credential was created. The dedicated receiver and authenticated
agent were started and their public HTTPS health verified. See the real
verification below; this supersedes the earlier missing-tunnel limitation.

## Verified real demo

Jacob approved using **Church of Clyde**, organization `545298`, and creating the
PAT. It is stored only in the integration backend's ignored `.env`, mode 600.
The account's timezone is America/Denver. Real API reads verified service type
`1826236`, three empty teams, two private plans (`92466235`, `92466244`) on
October 4 and October 11, 2026, with reminders disabled
and five explicit open positions. No people were scheduled.

The real API sync into ignored `planning-center-demo.db` created two events and
ten open shifts. Repeating seed and sync created no duplicate plans, events or
shifts. The local database contains zero volunteers, assignments or messages.
The API's plan `sort_date` is not the authoritative service timestamp; the
importer correctly uses `PlanTime.starts_at` and `ends_at` with timezone offsets.
Sanitized evidence: `docs/evidence/planning-center/live-sync.json`. This proves
live API access and local import; it does not connect the public Pages dashboard,
activate texting/scheduling. Live webhook delivery was verified separately below.

Local reset detection rejects stale import links before overwriting a local
event; use a fresh isolated import database after a schedule reset.

New additive tables: `pco_event_links`, `pco_shift_links`, `pco_deliveries`.
SQLite creates these via the existing app factory. PostgreSQL needs an owner-
reviewed schema migration before running this integration; no production schema
migration was performed for the demo.

## Checks and limits

`python -m pytest -q tests/test_planning_center.py` verifies two service times,
idempotent import, pagination, rejected credential-forwarding links, organization
scope, preserving assignments during shrink, signature enforcement, durable
deduplication and retry rollback. Those tests use a synthetic HTTP transport;
they do not prove credentials, actual API access, public reachability, Messages
delivery, or an active scheduler.

Official references:
- https://github.com/planningcenter/developers (personal-token authentication)
- https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan_time
- https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/needed_position
- https://api.planningcenteronline.com/docs/overview/webhooks


## Dedicated ngrok receiver

For the authorized live webhook demo, use the dedicated receiver rather than
exposing the full admin application. It shares the existing isolated imported
schedule database and only provides `/healthz` and the signed webhook route;
admin, text/simulator, SMS ingress, docs and OpenAPI routes return 404.

```sh
python tools/planning_center_webhook_server.py --env-file .env --database sqlite:///./planning-center-demo.db --port 58125
ngrok http 58125
```

Use the existing ngrok account authtoken through ngrok's private configuration;
do not put the token in command logs, Git, chat or shared setup notes. The agent
was installed from the official Homebrew cask for the requested tunnel setup.
A receiver may start while subscription signing keys are pending; health says
`webhook_configured: false` and webhook requests fail closed with 503. After
creating subscriptions, save all their keys privately and restart the receiver
before triggering a real test event. Verify public health, reject unsigned webhook posts, and match an
actual PCO EventDelivery receipt to a local import before claiming live success.
Texting and its scheduler remain separate. Tests in
`tests/test_planning_center_receiver.py` verify that no admin/texting paths are
exposed and that an existing isolated database/credentials are required; absent signing
keys fail closed.


## Real webhook verification, October 3, 2026

Actual Planning Center Plan update EventDelivery
`b1965f28-e57b-48b4-99bd-d2c9e09ca796` reached the dedicated receiver through
ngrok and returned HTTP 200. Its verified signature refreshed two events with
zero event/shift duplication. Planning Center's own redelivery returned HTTP
200 with `duplicate` and retained a single receipt for that event. A second
real event restored the original synthetic plan title and also returned 200.
Final database: two durable event receipts, two events, ten open shifts, zero
volunteers, assignments or messages. Native ngrok request/response evidence
is correlated to the exact durable EventDelivery IDs, and the PCO UI reported
200 for the original attempt.

Sanitized proof: `docs/evidence/planning-center/live-webhook.json`. It also
records current public health, rejected unsigned requests (401), and excluded
admin/texting/docs routes (404). Created/destroyed subscriptions are configured
and their distinct keys saved, but only Plan updated/replay/restoration were
actually exercised. The receiver performs no Gloo composition, texting,
background staffing or Messages activation.

The ngrok agent endpoint lasts while its process is running; this remains a
Mac-hosted demo, not an always-on deployment. Reuse the same account/domain,
verify the actual origin after restart, and update subscription URLs if it
changes. Do not set the public portal's BACKEND_URL to this dedicated receiver: it
intentionally excludes portal/admin endpoints. Start/stop only your own receiver
and tunnel processes; keep their state/logs under ignored `.planning-center-runtime/`.


## Saved service-time correction, October 3, 2026

The initial read-only audit superseded the earlier 9–10 AM
Denver claim. PlanTimes 229038886 (plan 92466235, event 1) and 229038904
(plan 92466244, event 2) were then saved at **09:00–10:00 UTC / 03:00–04:00 Denver**.
At that audit, the isolated database matched the source; this was not stale import data.
Plan sort_date is not the authoritative service time. The seed CLI sent
offset-bearing timestamps but did not assert saved timestamp equality; the
exact origin of the discrepancy is not proven by this read-only audit.

Intended 09:00 Denver is 15:00Z on both dates. Any subsequent correction should
use canonical UTC Z values, assert fresh API equality, then resync and verify
the local mapping. No source or database change was made during this audit.
Evidence: `docs/evidence/planning-center/current-service-times.json`.

Staffing writeback is a proposed extension, not implemented by this inbound
bridge. See `docs/PLANNING_CENTER_STAFFING_CONTRACT.md`.


## Authorized synthetic time repair, October 3 at 12:52 PM Denver

The two existing empty private plans were repaired through their exact PlanTime
IDs using canonical UTC Z timestamps. Fresh API reads verified **15:00–16:00Z /
09:00–10:00 America/Denver** on October 4 and 11. Explicit sync updated both
existing events (IDs 1/2) without creating events or shifts. All source/mapping
IDs remain unchanged, plans have no scheduled people, reminders remain disabled,
and local counts remain 2 events, 10 shifts, zero volunteers/assignments/messages.
No signup/texting runtime or subscription was changed.

`docs/evidence/planning-center/corrected-service-times.json` is the current
source-time receipt. The earlier `current-service-times.json` audit is retained
as history of the defect. The cloud owner should apply the narrow seed-tool
repair in `docs/PLANNING_CENTER_SEED_TIME_FIX.md`; no competing code change was
made by this live-verification task.
