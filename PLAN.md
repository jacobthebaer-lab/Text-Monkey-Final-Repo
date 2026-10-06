# PLAN.md: Text-First Volunteer Scheduling Agent

Build plan for the Gloo AI Hackathon 2026, Agents Track. Claude Code: read this whole file before writing code, then work one phase at a time (see "Build Phases").

**Recipient implementation update, October 5:** Clyde's supplied algorithm now owns
replacement ranking and batch size; Gloo composes the exact selected batch.
Existing reply windows own timing, each unique decline replaces one person, and
pending-probability expansion remains deferred until a model is supplied. Live
outreach remains held. [Current implementation](docs/RECIPIENT_ALGORITHM.md)
supersedes historical selection and tranche timing below.

**Text Monkey demo update, October 1:** Gloo now chooses replacement batches from the
full eligible pool and records its reason. Fixed ranking remains a history
signal, not the selection decision. Code still enforces eligibility, consent,
batch limits and affirmative acceptance. Volunteer signup is JOIN → name → YES
entirely by text, without a coordinator signup approval. The Cloudflare admin
website uses Supabase account creation, email confirmation and password recovery.
See `docs/TEXTY.md` for the connected demo and its remaining deployment gaps.

---

## 1. What we're building

A text-message agent that keeps a church's volunteer schedule full. Volunteers only ever text. The volunteer coordinator only approves. No app, no login for volunteers.

**User:** a part-time volunteer coordinator at a ~300-person church with ~60 active volunteers.

**Four jobs:**
1. **Plan the month:** collect availability by text, build a draft schedule, send it to the coordinator for approval.
2. **Remind:** day-before reminder texts; replies like "can't make it" start the fill process early.
3. **Fill gaps:** when someone cancels, find qualified replacements, text them in tranches, confirm the first yes, update the roster.
4. **Look ahead:** flag risks (single points of failure, burnout, drop-off, expiring checks, chronic gaps) and opportunities (untapped volunteers, unused skills, growing needs), with evidence and a suggested next step.

**Demo-critical path (build first, polish most):** inbound cancellation text → agent fills the shift via tranches → roster updates → sensitive message gets escalated instead of handled.

---

## 2. Core design principle: deterministic core, AI at the edges

The model makes judgment calls. Plain code enforces rules. The model can never bypass a rule because the rules live inside the tools, not in the prompt.

| Plain code (deterministic) | Model (Gloo AI) |
|---|---|
| Eligibility checks (qualifications, expirations, double-booking, availability) | Interpreting messy inbound texts |
| Candidate ranking | Deciding whether/how urgently to fill a shift |
| Tranche timing and wait windows | Handling partial offers, unclear replies |
| Scheduling solver (greedy assignment) | Reviewing a draft schedule and explaining problems |
| Send gate: policy, approvals, quiet hours, message budget, opt-out | Writing short personal outreach messages |
| Templated messages (reminders, confirmations, "it's filled, thanks") | Writing capacity/risk flag summaries |
| Qualification verification (admin only) | Understanding admin requests in natural language |
| All database writes | Asking the admin about unknown event types |

Hard rule: **no one is ever assigned to a role they don't hold current, admin-verified qualifications for.** Enforced in code in `eligibility.py`, checked again at assignment time.

---

## 3. Stack

- **Python 3.11+**
- **FastAPI**: Twilio webhook, admin web pages, phone simulator page
- **SQLite** via SQLAlchemy (swap to Postgres later if needed)
- **Jinja2 + plain HTML/CSS** for admin pages (no frontend framework)
- **APScheduler** for background jobs (tranche timers, reminders, monthly planning, capacity scans)
- **OpenAI Python SDK pointed at Gloo AI** (see section 4)
- **Twilio Python SDK** for SMS
- **Google Calendar API** (read-only) via google-api-python-client
- **pytest** for tests
- **python-dotenv** for config

Keep dependencies minimal. Ask before adding anything not listed.

---

## 4. Gloo AI integration (read carefully)

- Docs index: https://docs.gloo.com/llms.txt. Tool use guide: https://docs.gloo.com/api-guides/tool-use.md
- Auth: a single API key in env var `GLOO_API_KEY`, sent as a bearer token. The OpenAI SDK handles this.
- Client:
  ```python
  client = OpenAI(api_key=os.environ["GLOO_API_KEY"],
                  base_url="https://platform.ai.gloo.com/ai/v2/guarded")
  ```
- Use the **Responses API** (`client.responses.create`). Models are pinned: always pass an exact model name.
- **Tool schema must be the nested format** even on the Responses API:
  `{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}`
- Tool calls come back as `output[]` items with `type == "function_call"` (fields: `call_id`, `name`, `arguments` JSON string).
- To return a result, append to `input`: the `function_call` item (type, call_id, name, arguments), then `{"type": "function_call_output", "call_id": ..., "output": <json string>}`.
- Loop until the response contains a `message` item and no `function_call` items, or until `MAX_AGENT_STEPS` is hit (then escalate to human).
- Models come from env vars so we can swap them:
  - `AGENT_MODEL` (default `gloo-anthropic-claude-sonnet-4.6`): agent loops, schedule review, flags
  - `PARSER_MODEL` (default `gloo-openai-gpt-5-mini`): inbound message classification
  - Verify both names against Gloo's Supported Models page before relying on them.
- Log `usage` (input/output tokens) from every response into `agent_runs`.
- Wrap all Gloo calls in `app/llm/gloo_client.py` with retries (exponential backoff on 429/5xx) and a timeout. If Gloo fails, fall back safely: escalate to the coordinator, never guess.
- Endpoint: start with `guarded`. Make the endpoint family configurable (`GLOO_ENDPOINT=guarded|direct`) so we can compare. Eval cases must confirm sensitive messages are escalated, not blocked or deflected, on whichever endpoint we choose.

---

## 5. Repo structure

```
/app
  main.py                 # FastAPI app, routes
  config.py               # env/config loading
  clock.py                # Clock abstraction (real + fake for tests/demo fast-forward)
  db/
    models.py             # SQLAlchemy models
    session.py
    seed.py               # loads /data synthetic data
  core/
    eligibility.py        # hard rules
    ranking.py            # candidate scoring
    scheduler.py          # monthly greedy solver + validator
    send_gate.py          # ALL outbound messages pass through here
    templates.py          # templated message text
    policies.py
  llm/
    gloo_client.py        # Gloo wrapper, retries, usage logging
    parser.py             # inbound message classifier (structured JSON)
    agent_loop.py         # generic tool-calling loop
    tools.py              # tool schemas + implementations
  agents/
    fill_agent.py         # cancellation → filled shift
    planning_agent.py     # monthly availability + draft schedule review
    capacity_agent.py     # risk/opportunity flags
    admin_agent.py        # admin natural-language requests
  sms/
    provider.py           # SMSProvider interface
    twilio_provider.py
    mock_provider.py      # used in tests, evals, and simulator
  integrations/
    gcal.py               # read-only Google Calendar sync
  jobs.py                 # APScheduler job definitions
  web/templates/          # Jinja2 pages
/prompts                  # system prompts as .md files (versioned)
/data                     # synthetic seed data (CSV/JSON)
/evals
  cases/                  # eval cases (YAML) - DO NOT EDIT without human approval
  run_evals.py
  reports/
/logs                     # session logs (JSONL)
/tests
PROMPTS_CHANGELOG.md
README.md
LICENSE                   # MIT
.env.example
```

---

## 6. Data model

- **volunteers**: id, name, phone (E.164), sms_opt_in (bool), status (active/inactive), is_coordinator (bool), is_pastor (bool), preferences (JSON: interested_roles, preferred_services, max_per_month, serves_with_volunteer_id, notes), created_at
- **qualifications**: id, volunteer_id, type (background_check, child_safety_training, sound_training, drivers_license, first_aid, ...), status (pending/verified/expired), verified_by, verified_at, expires_on
- **roles**: id, name, ministry, required_qualifications (list), criticality (critical/standard/optional), fill_policy (auto/needs_approval)
- **event_types**: id, name, title_patterns (list of strings/regex used to match calendar titles)
- **role_recipes**: id, event_type_id, role_id, count
- **events**: id, gcal_event_id (nullable), title, event_type_id (nullable = unknown type), starts_at, ends_at, status
- **shifts**: id, event_id, role_id, slot_index
- **assignments**: id, shift_id, volunteer_id, status (proposed/approved/confirmed/cancelled/completed), source (planner/fill/admin), created_at, updated_at
- **availability**: id, volunteer_id, month (YYYY-MM), available_dates (JSON), unavailable_dates (JSON), raw_reply, parsed_at
- **fill_requests**: id, shift_id, cancelled_assignment_id, urgency (critical/high/normal/skip), state (open/waiting_approval/in_progress/filled/escalated/skipped), current_tranche, next_action_at, created_at, closed_at
- **outreach**: id, fill_request_id, volunteer_id, tranche, message_id, response (none/yes/no/partial/unclear), responded_at
- **approvals**: id, kind (send_outreach/publish_schedule/training_invite/...), payload (JSON), status (pending/approved/rejected/expired), requested_at, decided_at, decided_by, via (web/sms)
- **messages**: id, direction (in/out), volunteer_id (nullable), phone, body, kind (template/ai/admin), provider_sid, status, created_at
- **escalations**: id, category (sensitive/pastoral/unfillable/unclear/system_error/unknown_event), severity (normal/urgent), summary, related_ids (JSON), assigned_to, status (open/acknowledged/resolved), created_at
- **flags**: id, kind (concern/opportunity), type (single_point_of_failure/burnout/drop_off/expiring/chronic_gap/untapped/unused_skill/growing_need/rebalance), summary, evidence (JSON), suggested_action, status (open/accepted/dismissed), created_at
- **policies**: key/value settings: quiet_hours (21:00–07:00 local), monthly_ask_budget_per_volunteer (default 4), approval rules by role criticality, church timezone
- **agent_runs**: id, agent, trigger, started_at, ended_at, model, input_tokens, output_tokens, steps, outcome
- **agent_steps**: id, run_id, step_no, type (model_call/tool_call/tool_result/decision/escalation), tool_name, arguments (JSON), result (JSON), created_at

Every agent step is also appended to `/logs/session-YYYY-MM-DD.jsonl`. This is the auditable session log required by the challenge.

---

## 7. The send gate (most important safety component)

Every outbound SMS goes through `send_gate.send(...)`. Nothing else may call the SMS provider. The gate:

1. Checks `sms_opt_in`; handles STOP/START keywords (STOP → opt out, confirm once, never text again).
2. Checks the message kind against policy:
   - Pre-approved templates (reminders, assignment confirmations, "it's filled, thank you", availability asks within an approved plan): send.
   - Outreach for roles with `fill_policy = auto`: send.
   - Outreach for roles with `fill_policy = needs_approval` (anything involving children): create an `approvals` row and text the coordinator: "Sarah cancelled 9am nursery. I'd like to ask Jen, Mark, Priya. Reply YES to send." Hold until approved.
   - Anything to a person flagged in an open `sensitive` escalation: **blocked**. Only a human contacts them.
3. Enforces quiet hours (except same-day urgent fills, which still respect 06:30–21:30).
4. Enforces the monthly ask budget (confirmations/reminders don't count).
5. Logs to `messages`.

The model has a `request_send_text` tool, never direct SMS access.

---

## 8. Inbound message routing

Webhook `/sms/inbound` (Twilio) and the phone simulator both call `handle_inbound(phone, body)`:

1. Look up sender. Unknown number → template reply ("Hi! This number is for [Church] volunteers. Please contact the church office.") + log.
2. STOP/START handled by the send gate logic first.
3. If sender is the coordinator: check for a pending approval ("YES", "Y", "approve", "no") → resolve it; otherwise route to `admin_agent`.
4. **Sensitive check on every volunteer message**: parser returns `sensitive: true/false` + `severity`. Backstop keyword list in code (hospital, died, passed away, funeral, accident, emergency, surgery, cancer, hurt myself, suicide, etc.). If either fires → create escalation to pastor (urgent if self-harm or danger indicators), block automated replies to that person, and continue logistics (e.g., still fill their shift) without messaging them.
5. Classify intent with `PARSER_MODEL` into strict JSON:
   `{intent: cancel|accept|decline|partial|availability|question|confirm|other|unclear, shift_hint, dates, partial_window, sensitive, severity, confidence}`
   Validate against a schema; on invalid JSON retry once, then escalate as unclear.
6. Route: cancel → fill_agent; accept/decline/partial → match to open outreach → fill_agent; availability → planning; confirm → mark confirmed; question/unclear/low confidence (<0.7) → one clarifying template question, then escalate if still unclear.

---

## 9. Fill agent: last-minute cancellation (demo-critical)

Trigger: a cancel intent, or an admin marking someone out.

1. **Identify the shift.** If the volunteer has multiple upcoming assignments and the hint is ambiguous, ask which one (template with numbered options). Mark assignment cancelled. Send a kind acknowledgement (unless sensitive).
2. **Judge urgency** (tool `get_shift_context` returns role criticality, time until start, how many others are assigned, minimums). Code computes a default; the agent may adjust with a stated reason:
   - critical: role is critical and shift falls below minimum (e.g., a kids room needs 2 adults)
   - high: < 24h and standard role
   - normal: > 24h
   - skip: optional role with enough coverage → notify ministry leader only, close
3. **Build candidates** (tool `find_candidates`, deterministic): hard filters from `eligibility.py`, then scored by `ranking.py`:
   preferred role (+), already attending that service (+), fewer assignments in last 30 days (+), historical yes-rate (+), asked recently (−), over max_per_month (exclude), serves_with partner availability (+ if both can serve).
4. **Tranches** (timing in code, from `policies`):

   | Time until shift | Tranche 1 | Tranche 2 | Tranche 3 | Escalate if unfilled |
   |---|---|---|---|---|
   | > 48h | top 3, wait 4h | next 5, wait 4h | rest, wait 6h | 24h before |
   | 12–48h | top 3, wait 1h | next 5, wait 1h | rest, wait 2h | 6h before |
   | 2–12h | top 3, wait 20m | next 5, wait 20m | rest, wait 30m | 90m before |
   | < 2h | top 5, wait 10m | next 8, wait 10m | skip | immediately after T2 |

   The agent writes one short, warm, personal ask per tranche (no guilt, easy out). Each send goes through the send gate (approval if required). APScheduler schedules `next_action_at`.
5. **Handle replies:**
   - First eligible "yes" → assign (re-check eligibility at assignment time, with a DB lock to avoid double-assign) → confirm to them → "It's been filled, thank you so much for being willing!" to everyone else who was asked and hasn't replied → notify ministry leader.
   - Partial ("until 10:30") → agent decides: accept if role allows split coverage and look for the remainder, or thank and continue.
     - Current implementation limitation: partial replies never create a split assignment or fill the full slot. Shift/Assignment intervals, role split opt-in and verified Planning Center partial-time mapping are prerequisites for the requested reviewed split path. See [the verified partial-coverage gap](docs/PARTIAL_COVERAGE_GAP.md); this original requirement remains incomplete.
   - "Yes" from someone not eligible → warm thanks, no assignment.
   - Unclear → one clarifying question.
6. **Escalate** if unfilled at deadline: text + dashboard escalation to coordinator with who was asked, who declined, and options (combine rooms, move someone from an optional role with leader OK, call directly).
7. Log every step.

---

## 10. Planning agent: monthly schedule

Runs ~21 days before month start (and on demand from admin).

1. Sync events for the month (Google Calendar or seed data). Match titles to `event_types`. Unknown events → escalation of category `unknown_event` + text to coordinator: "I see 'Fall Festival' on Oct 25. What help will it need?" Admin reply goes to `admin_agent`, which proposes a new recipe for approval.
2. Generate shifts from recipes.
3. Send availability asks (template): "Hi Jen! Which [Month] Sundays can you serve? Reply with dates, 'same as usual', or 'not this month'." One reminder after 3 days to non-responders. "Same as usual" = infer from last 3 months pattern.
4. Parse replies into `availability`.
5. Run `scheduler.py` greedy solver: fill critical roles first, most-constrained shifts first, respect hard rules, preferences, max_per_month, fairness (spread load), serves_with pairs.
6. Run `validator` (code) for violations; then planning agent reviews the draft (tool results: gaps, conflicts, fairness stats) and fixes what it can by calling `propose_swap` tools; re-validate. Max 3 fix rounds.
7. Create approval `publish_schedule` with summary: "November schedule ready: 94% filled, 3 gaps, 1 conflict. Review at [link]." Admin approves on web or replies YES.
8. On approval: assignment texts (template) go out; reminders scheduled.

---

## 11. Reminders

Daily job at the church's configured time: day-before template for each confirmed/approved assignment:
"Hi Jen! Just a reminder: you're serving in the nursery tomorrow at 8:45am (9am service). Thank you for serving! Reply C to confirm or X if something came up."
X or a cancel intent → fill agent. Saturday 6pm: coordinator summary text ("Tomorrow: 14/14 filled. 1 cancellation today, covered by Priya.").

---

## 12. Capacity agent: flags

Weekly job. Code computes metrics; the agent turns them into flags with evidence and a suggested action (never contacts volunteers itself; training invites become approvals).

Rules (thresholds in `policies`):
- single_point_of_failure: ≤ 2 qualified people for a role, or one person covers > 50% of slots over 6 weeks
- burnout: served > max_per_month, or > 4 weeks straight
- drop_off: previously served ≥ 2x/month for 3+ months, now 0 in last 6 weeks or declined 3+ asks → suggest a human check-in (no automated message)
- expiring: qualification expires within 30 days
- chronic_gap: same shift unfilled or filled only via last-minute fill ≥ 3 of last 6 weeks
- untapped: opted in 30+ days, never scheduled
- unused_skill: qualification or stated skill matching an understaffed role
- growing_need: upcoming recipe demand > qualified supply in the next 8 weeks (e.g., Christmas)
- rebalance: ministry A over-supplied while ministry B has gaps

---

## 13. Admin agent

Coordinator texts or types (web chat box) things like:
- "We added a baptism service on the 15th, need 4 extra greeters"
- "Move Jen off nursery until her background check renews"
- "Who's serving Sunday?"
- "Fall Festival needs 10 general volunteers and 2 drivers"

Tools: read schedule, create events/shifts, propose recipe, mark volunteer unavailable, verify qualification (admin only), approve/reject pending items. Anything that changes records is echoed back as a confirmation before it's applied ("I'll add 4 greeter slots to the 15th. Reply YES to confirm.").

---

## 14. Agent tools (names, permissions)

| Tool | Allowed | Blocked |
|---|---|---|
| get_volunteer(id/phone) | read profile, prefs, quals | none |
| get_upcoming_assignments(volunteer_id) | read | none |
| get_shift_context(shift_id) | read | none |
| find_candidates(shift_id, exclude_ids) | read, deterministic ranking | cannot override eligibility |
| request_send_text(volunteer_id, body, purpose, fill_request_id?) | request via send gate | cannot bypass policy/approvals/opt-out/sensitive blocks |
| assign_volunteer(shift_id, volunteer_id) | write, with eligibility recheck | fails if not eligible |
| cancel_assignment(assignment_id, reason) | write | none |
| schedule_next_tranche(fill_request_id) | write timing | cannot shorten below policy minimums |
| create_escalation(category, severity, summary, related) | write | none |
| create_flag(...) | write | none |
| propose_recipe(event_type, roles) | creates approval | cannot apply directly |
| verify_qualification | admin agent only, admin-initiated | never from volunteer claims |
| read_calendar(range) | read-only Google Calendar | no writes to church calendar |

Explicitly blocked everywhere: deleting data, changing qualifications from volunteer self-reports, messaging anyone with an open sensitive escalation, sending money, contacting non-opted-in numbers, pastoral/spiritual advice.

---

## 15. Prompts

- Store each system prompt as a file in `/prompts` (`fill_agent.md`, `parser.md`, `planning_agent.md`, `capacity_agent.md`, `admin_agent.md`).
- Each prompt must include: role and goal; what it may and may not do; "you prepare, route, and schedule; you never counsel, advise spiritually, or make pastoral judgments"; escalation criteria; tone (warm, brief, no guilt, SMS-length ≤ 300 chars); output expectations.
- Every change: bump a version number at the top of the file and add an entry to `PROMPTS_CHANGELOG.md` (what changed, which eval failure or observation prompted it). Keep old versions in git history. The build doc needs at least one earlier version and what was wrong with it.

---

## 16. Web pages (admin)

Simple, clean, mobile-friendly Jinja2 pages. No auth needed for the hackathon beyond a single `ADMIN_PASSWORD` env var with a basic login (document this as a known gap).

- **Dashboard:** next 14 days, gaps highlighted, open fill requests with live tranche status, open escalations, open flags
- **Approvals:** queue with approve / edit / reject
- **Schedule:** month grid by event/role
- **Needs map:** roles, requirements, event types, recipes (editable)
- **Volunteers:** list, qualifications with expiry, preferences
- **Flags**
- **Session log viewer:** agent runs → steps → tool calls, tokens
- **Phone simulator** (`/simulator`): pick any volunteer or the coordinator, see their SMS thread, send messages as them. Uses the mock provider. This is the demo fallback and the eval visualizer.
- **Demo controls** (only when `DEMO_MODE=true`): fast-forward the fake clock (e.g., +20 min to trigger the next tranche), reset DB to seed.

---

## 17. Synthetic data (`/data`)

All synthetic. State this in README and build doc. Fictional church: "Cedar Hills Community Church", timezone America/Denver.

- **Events:** Sunday services 9:00 and 11:00; Wednesday kids night 6:30pm; a Food Drive Saturday; "Fall Festival" (intentionally no event type, to trigger the unknown-event flow); Christmas Eve services in the capacity horizon.
- **Roles:** greeter (optional/auto), usher (standard/auto), parking (standard/auto), coffee (optional/auto), nursery (critical/needs_approval; background_check + child_safety_training), kids teacher (critical/needs_approval; same), sound (critical/auto; sound_training), first aid (standard/auto; first_aid), food drive general (standard/auto), driver (standard/auto; drivers_license).
- **~45 volunteers** including deliberate cases: one person running sound nearly every week (single point of failure); one serving 3x their max (burnout); one long-time volunteer who stopped (drop-off); two opted-in but never scheduled (untapped); a nurse not on first aid (unused skill); three background checks expiring within 30 days; one pending (unverified) child_safety_training; a married couple who serve together; one opted-out number.
- **The coordinator** and **a pastor** as special volunteers.
- **~40 sample inbound texts** in `/data/sample_texts.json`, messy and realistic: "cant make it tmrw sorry!!", "which sunday?", "i can but only til 10:30", "Y", "yes!!", "who is this", "not this month", "2nd and 4th", "same as usual", "STOP", "my dad was just taken to the ER, can't come", "I'm not doing well and don't want to be around anyone", "I finished the safety training last week so put me in nursery", etc.

Phone numbers: use obviously fake numbers (e.g., +1555...) except for demo volunteers, whose numbers come from a local, gitignored `demo_phones.json`.

---

## 18. Evaluation (`/evals`)

- ≥ 20 hand-built cases in YAML. Each: `id`, `description`, `setup` (seed variant + fake clock time), `inbound` messages (with timestamps), `expected` (outcomes: assignments, escalations, messages sent/not sent, approvals created), `must_not` (e.g., no message to X, no unqualified assignment).
- Categories: straightforward cancel/fill (3), ambiguous shift (2), critical kids role with approval (2), partial offer (1), simultaneous yeses (1), ineligible yes (1), no one available → escalation (1), sensitive cancellation → pastor escalation + no auto-reply (3, including self-harm indicator), STOP (1), unknown number (1), self-reported qualification not trusted (1), availability parsing (3), unknown event type (1), quiet hours (1), Gloo API failure → safe escalation (1).
- `run_evals.py` runs all cases against the mock SMS provider and fake clock, outputs `evals/reports/<timestamp>.md` with pass/fail per case and totals, plus token usage.
- **Evals use real Gloo calls** (so they test the real model) but **never real SMS**.
- **Do not edit files in `/evals/cases` or pass criteria without explicit human approval.** Fix the code or prompts instead.

---

## 19. Config (`.env.example`)

```
GLOO_API_KEY=
GLOO_ENDPOINT=guarded
AGENT_MODEL=gloo-anthropic-claude-sonnet-4.6
PARSER_MODEL=gloo-openai-gpt-5-mini
MAX_AGENT_STEPS=15
SMS_PROVIDER=mock            # mock | twilio
LIVE_SMS=false               # must be true AND SMS_PROVIDER=twilio to send real texts
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
GOOGLE_CALENDAR_ID=
GOOGLE_SERVICE_ACCOUNT_JSON=
CHURCH_TIMEZONE=America/Denver
ADMIN_PASSWORD=
DEMO_MODE=true
PUBLIC_BASE_URL=             # for Twilio webhook (e.g., ngrok or deployed URL)
```

---

## 20. Build phases

Work one phase at a time. At the end of each phase: run tests, commit with a clear message, and summarize what was built, what's stubbed, and any decisions made. Wait for the human before starting the next phase.

**Phase 0: Setup.** Repo structure, MIT LICENSE, README skeleton, `.env.example`, `.gitignore` (.env, demo_phones.json, logs/*.jsonl optional), config loader, fake/real clock, pytest running.
*Done when:* `pytest` passes on an empty test; app boots.

**Phase 1: Data layer + seed.** SQLAlchemy models, synthetic data files per section 17, seed script, reset command.
*Done when:* seed loads; tests verify the deliberate cases exist.

**Phase 2: Deterministic core.** eligibility, ranking, send gate (with mock provider), templates, policies, message log.
*Done when:* unit tests cover every hard rule, quiet hours, budget, STOP, sensitive block, approval hold.

**Phase 3: Gloo client + parser.** Gloo wrapper with retries and usage logging; inbound classifier with JSON validation and keyword backstop; `handle_inbound` routing.
*Done when:* parser correctly classifies the sample texts (print a table); a live Gloo call works with the real key.

**Phase 4: Agent loop + fill agent (DEMO-CRITICAL).** Generic tool loop, tools from section 14, fill agent end to end with tranches on the fake clock, session logging.
*Done when:* a scripted scenario (cancel → T1 no reply → T2 yes → filled, others thanked) passes; sensitive cancellation escalates and blocks replies; kids role waits for coordinator YES.

**Phase 5: Web app + simulator.** Dashboard, approvals, schedule, session log viewer, phone simulator, demo controls.
*Done when:* the full demo scenario can be run in a browser with no real SMS.

**Phase 6: Twilio live.** Twilio provider, webhook with signature validation, `LIVE_SMS` guard, instructions for ngrok/deploy in README.
*Done when:* a real text from a verified phone triggers the fill flow and replies arrive on real phones.

**Phase 7: Reminders + monthly planning.** Reminder job, coordinator summary, availability collection, solver, validator, planning agent review loop, publish approval.
*Done when:* a month is planned from seed availability with ≥ 90% fill and zero hard-rule violations; unknown event triggers the admin question.

**Phase 8: Capacity flags + admin agent.** Metrics, flags with evidence, admin natural-language requests with confirm-before-apply.
*Done when:* every deliberate seed case produces the right flag; three sample admin requests work.

**Phase 9: Google Calendar (read-only).** Sync events from a test calendar; match to event types.
*Done when:* events from a real test calendar appear as shifts. (If short on time, keep seed-data events and document as a known gap.)

**Phase 10: Evals + hardening.** Build the eval set per section 18, run, fix failures via code/prompt changes (logged in PROMPTS_CHANGELOG.md), rerun.
*Done when:* report shows pass rate; all failures documented.

**Phase 11: Docs + cost.** README (setup, credentials, known gaps), cost report from logged tokens and message counts at a realistic monthly volume, export sample session log for the build doc.

**Priority if time runs short:** Phases 0–6 are required for the demo. Then 10 (evals), then 7, 8, 9.

---

## 21. Non-goals (for the hackathon)

- Volunteer-facing app or login
- Multi-church tenancy (design the schema so it could be added)
- Real member data of any kind
- Payment, donations, or anything financial
- External scheduling integration (document as the next step; the data model maps to it)
- Minors as volunteers (document as a known gap; future path requires parent involvement)

---

## 22. Definition of done (demo)

1. A judge's verified phone texts a cancellation for a greeter or nursery shift.
2. The dashboard shows the fill request, urgency, and ranked candidates.
3. (Nursery) the coordinator phone gets "Reply YES to send"; YES is sent.
4. Tranche 1 phones buzz; demo control fast-forwards; tranche 2 goes out; someone replies YES; others get "It's been filled, thank you!"; the roster updates.
5. A second cancellation containing a family emergency creates an urgent pastor escalation, with no automated reply to that person, while the shift fill still proceeds.
6. The session log viewer shows every step and tool call with token counts.
