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
Production), and two private Sunday 9 AM–10 AM America/Denver plans. It disables
plan reminders and schedules no people. It verifies saved objects with fresh
GET requests. Use only the synthetic demo organization; seed is a real API write.

Save the returned service type ID in `PCO_SERVICE_TYPE_IDS`, and matching org ID
in `PCO_ORGANIZATION_ID`. Then explicitly select an isolated local database:

```sh
python tools/planning_center_demo.py sync --env-file .env --expected-org ORG_ID --database sqlite:///./planning-center-demo.db
```

The importer never uses a default production database from `.env` through this
CLI. Empty teams do not manufacture staffing requirements. Add explicit open
positions to the synthetic plans in Planning Center to demonstrate imported
gaps. The application retains local staffing history when a remote need shrinks.
Unoccupied removed remote needs are pruned; vanished remote service times are
cancelled only if no local assignment history exists. These local cancellations
never cancel Planning Center data.

## Webhook

The backend includes `POST /integrations/planning-center/webhook`. Set
`PCO_WEBHOOK_SECRET` to the subscription's authenticity secret in ignored `.env`.
It validates Planning Center's hex HMAC-SHA256 `X-PCO-Webhooks-Authenticity` header
against the raw request body, enforces the selected organization, deduplicates
EventDelivery IDs transactionally, and refreshes only the allowlisted service
types. Successful relevant events return 200; API/DB failures return 503 so
Planning Center retries. No text or background outreach runs from this route.
Unrelated event types are acknowledged without reading their payload or people.

For a live subscription, use the **existing verified HTTPS ngrok backend origin**
plus the route above. Subscribe to Services plan/plan-time/needed-position changes.
Do not register against a stale or unavailable tunnel: inspect ngrok's local
`http://127.0.0.1:4040/api/tunnels`, verify the public `/healthz`, verify that the
running backend has this route and the secret, then register. The webhook secret
and selected scope must be configured before any delivery test. Verify an actual
Planning Center delivery ID, 200 server response, durable receipt and imported
change separately from signed synthetic requests.

As checked October 3, 2026, the local ngrok API was unavailable and the identified
private configuration's `PUBLIC_BASE_URL` was empty. No subscription or live
webhook delivery has been verified by this task.

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
