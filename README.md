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

## Known gaps

- Admin pages use a single shared `ADMIN_PASSWORD` (hackathon scope).
- To be expanded as phases complete.

## Status

Phase 0 (setup) complete. See `PLAN.md` section 20 for the phase list.
