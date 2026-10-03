# Text Monkey

A Gloo AI hackathon demo for church volunteer scheduling. Volunteers communicate by text; coordinators review coverage, approvals and care follow-ups in the admin console. The repository includes fictional church and volunteer fixtures.

Text Monkey collects availability, drafts monthly schedules, reminds volunteers, fills cancellations and identifies capacity risks. Application code enforces consent, qualifications, quiet hours, approval requirements and assignment checks. Gloo interprets incoming messages and composes outgoing messages. Gloo failures must leave live messages held for review; do not replace Gloo with another provider or silently send canned live replies.

## Shared GitHub repository

Jacob has owner/admin access; Clyde (`Clyde-Kertzer`) and Noah (`clementsnc`) have active write access. Cloud can select this repository; connected texting still requires laptop Messages. Preserve uncommitted work and use separate feature branches/worktrees for new changes.

Use the private [Text Monkey repository](https://github.com/jacobthebaer-lab/text-monkey/tree/codex/complete-text-monkey), branch `codex/complete-text-monkey`. That branch is also the canonical fork's default branch. The earlier `clementsnc/planning-center-but-better` repository remains `upstream`; new collaboration and pushes use `origin` at `jacobthebaer-lab/text-monkey`.

For a new checkout with an account authorized for the private repository:

```sh
git clone --branch codex/complete-text-monkey https://github.com/jacobthebaer-lab/text-monkey.git
cd text-monkey
```

For an existing checkout, inspect its remotes, then point `origin` to the canonical fork:

```sh
git remote -v
git remote set-url origin https://github.com/jacobthebaer-lab/text-monkey.git
git fetch origin
```

Switch to the existing local branch with `git switch codex/complete-text-monkey`; if it has not been created locally, use `git switch --track origin/codex/complete-text-monkey` instead.

Retain `upstream` at `https://github.com/clementsnc/planning-center-but-better.git`. If that remote is absent, add it with `git remote add upstream https://github.com/clementsnc/planning-center-but-better.git`. Work on `codex/complete-text-monkey`; the GitHub handoff for Clyde owner coordinates shared integration and remote pushes. See [repository handoff](docs/BRAND_REPOSITORY_HANDOFF.md) for verified repository identities and historical boundaries.

## Run the synthetic preview

Requires Python 3.11+. This preview uses browser-local sample rules, fictional data and simulated conversations. It does not connect accounts, call Gloo or send texts.

```sh
python3 tools/texty_local_demo.py --port 58123
```

Open `http://127.0.0.1:58123/texty`. Review Home, coverage, the roster, onboarding and Settings. The admin console no longer includes incoming-text simulation, sample-send controls or fake delivery toggles. Real updates need a saved consenting admin mobile number and the connected app.

## Develop and verify the backend

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
DATABASE_URL=sqlite:// AUTOMATION_ENABLED=false SMS_PROVIDER=mock LIVE_SMS=false pytest -q
cd web/texty
npm ci
npm test
cd ../..
```

Fill in private configuration locally only when needed. Never commit credentials, real phones, conversations, databases or logs. Account access and saved church settings come from the admin setup; profile edits do not enable texting.

For an isolated synthetic backend, use a **new local SQLite file**. Seeding drops and recreates that selected database, so do not point it at shared or connected data.

```sh
DATABASE_URL=sqlite:///./local-synthetic-demo.db AUTOMATION_ENABLED=false SMS_PROVIDER=mock LIVE_SMS=false python -m app.db.seed
DATABASE_URL=sqlite:///./local-synthetic-demo.db AUTOMATION_ENABLED=false SMS_PROVIDER=mock LIVE_SMS=false uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `/texty` for the coordinator portal for account setup and coverage. Backend language workflows require your private `GLOO_API_KEY`; offline tests use explicit test doubles. Missing Gloo configuration does not establish a working live workflow. New legacy `/operations` controls, monthly collection, and legacy reminder jobs are deliberately held with connected delivery until Gloo composition and exact review are integrated. The reviewed fill and three-hour event-update paths remain separate.

## Evaluation

```sh
python -m evals.run_evals
# Optional real Gloo calls using your private local configuration; SMS stays mocked:
python -m evals.run_evals --live --env-file .env
```

Read [Clyde handoff](docs/CLYDE_HANDOFF.md) for the checkpoint's test counts and remaining work. Deterministic replays and live-model evaluations are reported separately. Generated raw logs and reports stay local unless sanitized for the repository.

Use the [portable demo runbook](docs/DEMO_RUNBOOK.md), [fictional import package](docs/IMPORT_DEMO.md), and [three-hour status replay](docs/demo_admin_status.md) for reproducible demonstrations. The [Planning Center guide](docs/PLANNING_CENTER.md) includes verified API and signed-webhook evidence.

## Connected texting and hosting

Use the first-party connector through Messages on the coordinator's Mac. Select the intended receiving line, exact consenting recipients and bounded test session in ignored private configuration. Read [Mac transport setup](docs/MAC_MESSAGES.md) before operating it. The Mac, backend and connector must run; paused scheduling or Messages cannot produce background updates. Native delivery evidence must be checked separately from a queue acknowledgment. The verified historical device test used iMessage; carrier SMS needs its own device verification.

The admin update workflow summarizes coverage three hours before an event, with deduplication, quiet hours, current blockers and a specific next action. Connected delivery requires enrollment, consent and active runtime connections. The static preview cannot send an admin update.

[Public preview build and publication](docs/CLOUDFLARE_DEMO.md) packages static assets only. The connected Worker and private backend are separate. This checkpoint does not authorize deployment, outreach or automatic customer operations.

## Repository guide

- `app/`: FastAPI, deterministic scheduling, agent workflows, Gloo and Mac integration.
- `web/texty/`: coordinator portal and frontend tests; existing directory and route names remain compatibility details.
- `tools/`: isolated preview and static demo packaging.
- `prompts/`: versioned Gloo prompts; changes recorded in `PROMPTS_CHANGELOG.md`.
- `data/`, `tests/`, `evals/`: synthetic fixtures, regression tests and workflow evaluations.
- `supabase/`: migrations for reviewed account and scheduling storage.
- `docs/`: setup, implementation boundaries and handoff.

See [build plan](PLAN.md), [administrator onboarding](docs/ADMIN_SETUP.md) and [demo coordination](DEMO_COORDINATION.md). Older transport references describe implementation history; the current authorized route is Gloo plus laptop Messages. Planning Center connectivity and production hosting are tracked as separate work and must not be inferred from this demo.
