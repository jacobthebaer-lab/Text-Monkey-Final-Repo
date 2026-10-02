# Texty coordinator demo

Live synthetic preview: https://texty-volunteer-demo.jacobthebaer.workers.dev

The website is published on Cloudflare Workers. The preview uses explicitly
labelled sample rules and synthetic data saved in the visitor's browser. It
never invokes Gloo or sends SMS. Actual model calls use the existing Python
Gloo Responses client; no OpenAI account or key is needed.

## Current implementation

- Responsive coordinator dashboard, roster, add/edit form, schedule, approvals,
  personal-concern review and text lab.
- Supabase email/password login with server verification of confirmed email
  and an explicit coordinator allowlist. Tokens remain in browser memory.
- Python JSON endpoints adapt the existing scheduling core. The text lab and
  approvals made through Texty always use MockSMSProvider, even if Twilio is
  enabled elsewhere. All simulated outbound messages still pass through SendGate.
- `ALLOW_TEXT_SIGNUP=true` lets an unknown sender request signup. Gloo extracts
  a complete first/last name; the coordinator approves an inactive, opted-out
  profile. Consent and individual qualifications must be verified afterward.
- Optional Supabase Postgres storage in the private `texty` schema, using the
  existing SQLAlchemy models. Existing SQLite seed/demo workflows still work.

## Connect the isolated Supabase project

The Texty Hackathon organization was created under `jacobthebaer-lab` via GitHub.
Project creation still requires the owner to set a database password.

1. Create the project in that organization. Keep automatic RLS enabled and
   automatic table exposure disabled.
2. Apply `supabase/migrations/20261002022222_texty_store.sql` in that project's
   SQL editor or through a connector authenticated to **that exact account**.
   Do not use the separate Baer Ventures connector/project by mistake.
3. Privately set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and
   `ADMIN_EMAIL_ALLOWLIST=jacobthebaer@gmail.com` in the backend environment.
4. In the new project's Authentication users page, create the confirmed app
   admin with that email. The owner chooses the app password. The Supabase
   platform GitHub login and the app's Supabase Auth login are separate.
5. To put the scheduling store in Supabase as well, set `DATABASE_URL` to the
   project's **session pooler** Postgres connection string, using the owner's
   database password and TLS (`sslmode=require`). `postgresql+psycopg://` is
   supported. The engine maps all app tables to the private `texty` schema.
6. Populate only the new, isolated synthetic demo database. The existing seed
   command drops/recreates app tables; never run it against unrelated data.
   Reapply the migration's RLS/revoke statements after any reset.

Supabase auth and Postgres integration cannot be claimed live until verified
against the completed project. The backend does not expose a service-role key
or database password to the website.

## Local development

Use Python 3.11+; this checkout was verified on Python 3.13.

```sh
.venv/bin/pytest
.venv/bin/python -m app.db.seed
.venv/bin/uvicorn app.main:app --port 8001
```

Open http://127.0.0.1:8001/texty for the dashboard. The original coordinator
pages remain protected by `ADMIN_PASSWORD` when set.

For the Cloudflare frontend, in a second terminal:

```sh
cd web/texty
npm ci
npm run dev
```

Copy `.dev.vars.example` to `.dev.vars` to connect that development frontend
(port 8000) to FastAPI (port 8001). Set the same private `BACKEND_BRIDGE_KEY` in
both environments. Never print or commit either secret file.

## Cloudflare hosting and phone testing

The Python process needs a reachable URL; Workers hosts the frontend and proxies
`/api/*`, while FastAPI runs the scheduling engine. For a temporary demo,
`ngrok http 8001` supplies a backend URL. Set Cloudflare's `BACKEND_URL` to that
URL and `BACKEND_BRIDGE_KEY` to the backend secret. A temporary tunnel stops
working when its process or computer stops. A hosted Python service is the
next deployment step for an always-on app.

Twilio targets `<backend URL>/sms/inbound`, with the exact same origin in
`PUBLIC_BASE_URL`. It does not target the static website's SMS route. Keep
`LIVE_SMS=false` until verified demo phones and human-approved live tests are
ready. Only the existing signed webhook and original send gate may send live.

## Verification and remaining work

133 backend tests pass, including signup review, missing names, duplicate
requests, sensitive unknown senders, authorization, and forced mock delivery.
The Cloudflare bundle passed a deployment check. Browser checks covered add
volunteer, signup review, STOP and human-care review.

Both pinned Gloo models passed live preflight. The 43-text sample check reached
36/43 intent matches (84%) and 42/43 sensitive-flag matches (98%). A separate
live signup created a coordinator approval without SMS. The seven intent
mismatches remain documented in `GLOO_VERIFICATION.md`; no eval criteria or
cases were changed.

Not yet live: the new Supabase project, its app admin login, the backend tunnel,
and real Twilio delivery. No Gloo result or complete cloud database integration
is implied by the synthetic preview.
