# ServFrictionless

A text-first volunteer scheduling agent for churches, built for the Gloo AI
Hackathon 2026 (Agents Track).

Volunteers only ever text. The coordinator only approves. No app, no login.

**Four jobs:**

1. **Plan the month** — collect availability by text, build a draft schedule,
   send it to the coordinator for approval.
2. **Remind** — day-before reminder texts; "can't make it" starts the fill
   process early.
3. **Fill gaps** — when someone cancels, find qualified replacements, text
   them in tranches, confirm the first yes, update the roster.
4. **Look ahead** — flag risks (single points of failure, burnout, expiring
   background checks) and opportunities, with evidence and a suggested next
   step.

Design principle: **deterministic core, AI at the edges.** Eligibility rules,
tranche timing, quiet hours, and the send gate live in plain code. The model
interprets messy texts and writes warm outreach — it can never bypass a rule,
because the rules live inside the tools.

All data in this repo is synthetic. The fictional church is "Cedar Hills
Community Church". No real names, phone numbers, or church data are used.

## Setup

Requires Python 3.11+.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in GLOO_API_KEY etc.
```

Run the tests:

```bash
pytest
```

Seed the database with synthetic data (also the reset command — it drops and
recreates everything):

```bash
python -m app.db.seed
```

Run the app:

```bash
uvicorn app.main:app --reload
```

## Configuration

All configuration comes from environment variables (see `.env.example`).
Real SMS is double-gated: messages only leave the building when
`SMS_PROVIDER=twilio` **and** `LIVE_SMS=true`. Tests, evals, and the phone
simulator always use the mock provider.

## Project layout

See `PLAN.md` for the full build plan and `CLAUDE.md` for working rules.

- `app/` — FastAPI app, deterministic core, agents, Gloo AI integration
- `prompts/` — versioned system prompts (changes logged in `PROMPTS_CHANGELOG.md`)
- `data/` — synthetic seed data
- `evals/` — eval cases and runner
- `logs/` — auditable agent session logs (JSONL, gitignored)
- `tests/` — pytest suite

## Going live with real SMS (Twilio)

Real texts are double-gated: nothing leaves the building unless
`SMS_PROVIDER=twilio` **and** `LIVE_SMS=true`, both set by a human in `.env`.

1. In the Twilio Console: buy an SMS-capable number, note the Account SID,
   Auth Token, and number, and (on a trial account) verify every phone that
   should receive texts under Verified Caller IDs.
2. Fill in `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
   `TWILIO_FROM_NUMBER`, `SMS_PROVIDER=twilio`.
3. Map demo volunteers to real phones in `demo_phones.json` (gitignored):
   `{"Jen Hartley": "+1970...", "Maria Delgado": "+1..."}` — the seed script
   overlays these onto the synthetic roster. Re-run `python -m app.db.seed`
   after editing it.
4. Expose the webhook: `ngrok http 8000`, put the https URL in `.env` as
   `PUBLIC_BASE_URL`, and set the Twilio number's "A message comes in"
   webhook to `<that URL>/sms/inbound` (POST). Free ngrok URLs change on
   every restart — re-paste both places each session. Inbound requests are
   verified against the X-Twilio-Signature header; `PUBLIC_BASE_URL` must
   match exactly or validation fails.
5. Flip `LIVE_SMS=true`, start the app (`uvicorn app.main:app`), and text a
   cancellation (e.g. "can't make it Sunday") from a mapped phone to the
   Twilio number. Keep `DEMO_MODE=true` so the clock stays pinned to the
   seed anchor and the demo bar's fast-forward still drives tranches.

To go back to safe mode, set `LIVE_SMS=false` — everything else keeps
working against the mock provider and the phone simulator.

## Known gaps

- Admin pages use a single shared `ADMIN_PASSWORD` (hackathon scope).
- To be expanded as phases complete.

## Status

Phases 0–6 complete. Run `python -m app.db.seed`, then
`uvicorn app.main:app` and open http://127.0.0.1:8000 — dashboard,
approvals, schedule, needs map, volunteers, flags, session log viewer,
phone simulator, and demo controls (fast-forward / reset). Live Gloo
verified: 84% intent / 98% sensitive-flag accuracy on the sample texts
(`python -m app.llm.classify_samples`). The live end-to-end Twilio text
awaits ngrok + webhook configuration (see "Going live"). See `PLAN.md`
section 20 for the phase list.

## Texty dashboard

A Cloudflare coordinator demo and Supabase login/storage adapter are now in
`web/texty/` and `app/web/texty.py`. See [Texty setup](docs/TEXTY.md) for the live
synthetic preview, verified behavior, and the remaining account connections.
