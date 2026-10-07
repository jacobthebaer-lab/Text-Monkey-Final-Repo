# Text Monkey: Agent Build Document

Prepared October 6, 2026. Source checkpoint: `a9bc76d89b3fa3cf2fb3e68f1ac970cd0eae5efe`. The accompanying [verbatim prompts and audit appendix](submission/PROMPTS_AND_AUDIT.md) is part of this document.

## The user and the burden

Text Monkey serves pastors, event organizers and church volunteer coordinators staffing childcare, youth ministry, hospitality and other roles. Volunteers communicate solely through ordinary texts, with no app or login. Coordinators use a cloud admin portal to review availability, staffing gaps, proposed changes and messages.

We spoke with several volunteer coordinators at Cornerstone Church in Boulder, Colorado. They described replacement coordination as a substantial part of their week: someone cancels, often at the last minute, and the coordinator must remember who is available, qualified and recently asked, decide whom to contact, and track replies. Information gets lost across conversations. Their goal was to make scheduling efficient while preserving their personal relationships with volunteers.

The workflow covers both advance staffing and replacement after cancellation. Group requests create notifications for people who cannot fill the opening and leave the organizer tracking individual replies. Text Monkey prepares event-specific requests for a selected subset of eligible volunteers. Reducing unnecessary notifications is a design goal, not a measured outcome.

Those conversations validate the problem qualitatively. We have not measured weekly hours, financial losses, missed shifts or time saved. The next pilot measures coordinator minutes per cancellation, time to confirmed coverage, unresolved gaps and notifications per filled role. Public evaluation uses fictional adult volunteers and churches, not scraped congregational, donor, counseling or minor records.

## Architecture

Text Monkey is an event-driven pipeline with specialized agents and a bounded tool loop. Its work continues through replies and scheduled jobs rather than ending after a drafted answer.

```text
An event needs volunteers, or someone cancels
  → Check texting consent and hold sensitive messages for a person
  → Gloo interprets the message; the app identifies the event and role
  → Filter for availability, qualifications and serving limits
  → Our specialised algorithm selects a group of eligible volunteers
  → Gloo drafts invitations for coordinator approval
  → Send approved invitations and track delivery and replies
  → Recheck eligibility, then prepare the assignment for approval
  → Update coverage, or return an unresolved gap to the coordinator
```

Signup and availability agents collect missing facts and preserve role-specific restrictions. Unresolved preferences remain drafts. Monthly planning starts with a constrained schedule, then an agent inspects gaps, proposes repairs or swaps, and inspects again. The coordinator approves publication. Coordinator-command tools prepare changes against actual record IDs and cannot approve their own proposals.

Planning Center integration imports selected service times and open staffing needs into local events and shifts. Signed, deduplicated webhooks refresh the allowed scope. This bridge does not establish consent, automatically start outreach or mirror the full remote roster; remote staffing writes remain a separately reviewed boundary.

### Our specialised selection algorithm

Our specialised algorithm ranks only volunteers who pass the eligibility checks. For each person, it combines acceptance rate, response speed and time since their last successful request:

$$
S = \frac{a \times t}{r}
$$

$$
\mathrm{score} = \frac{10 \times S}{S + 100}
$$

Here, **a** is the fraction of resolved requests answered YES, including declined and expired requests in the denominator; **r** is average positive response time in minutes; and **t** is minutes since the last successful request. Pending requests are not counted as declines. Higher acceptance rates, faster replies and a longer interval since the last ask increase priority. The score is a ranking value, not a probability; 100 controls its scale rather than the ranking order.

To decide how many people to ask, the algorithm takes the smallest group from the top of that ranking whose acceptance rates sum to at least this target:

$$
T = R \times \left(1 + \frac{0.5 \times 120}{u + 120}\right)
$$

**R** is the number of unfilled places and **u** is minutes until the filling deadline. The target rises as the deadline approaches, allowing a larger invitation group when time is short. Each application fill represents one vacancy, so R is currently 1. Expected acceptances are estimates, not guaranteed coverage; an insufficient pool is flagged.

New volunteers start from saved peer averages, or explicit defaults when no history exists. Drafts and queued or uncertain sends do not create request history. Gloo must compose for the selected IDs without replacing them. Reservations and locks prevent duplicate asks and double fills. The first eligible acceptance wins; later replies cannot create a second assignment. Faster coverage and fewer notifications still need pilot validation; probability-based follow-ups remain deferred.

Code makes permission, eligibility, timing and assignment decisions. Gloo handles language interpretation, composition and bounded schedule repair. Uncertain sends and incomplete fills return to the coordinator.

## Prompts, verbatim

The appendix includes all eight current prompt files, runtime instruction additions, tool instructions and schemas, and the earlier parser prompt. There is no hidden universal system prompt: each workflow supplies its versioned instructions, and the Gloo client appends the documented punctuation rule.

Parser version 1 failed on a sensitive cancellation when a guarded non-JSON response lost the logistical action. The later prompt and a strict cancellation backstop preserve an unambiguous cancellation while holding sensitive communication for a person. Fill version 6 now composes invitations for the algorithm's selected group; the earlier version asked the model to choose recipients itself. Prompt text and source provenance are supplied for reproduction.

## Platform and stack

The coordinator frontend is hosted on Cloudflare Pages. The persistent application uses Python, FastAPI, SQLAlchemy and APScheduler; SQLite supports isolated reproduction. The repository also contains Supabase migrations and Planning Center adapters. A Node/Playwright/Chromium Google Voice adapter implements the cloud prototype transport interface; the retained Mac connector is a separate transport. Hosting a frontend alone does not establish backend or delivery readiness.

All language calls use Gloo's Responses API through the OpenAI Python SDK. Defaults are `gloo-openai-gpt-5-mini` for structured interpretation and `gloo-anthropic-claude-sonnet-4.6` for tool use and schedule review. This allocates the smaller model to routine extraction and the larger model to constrained multistep work. The audit records the actual model. Failure holds the action; no provider or canned-message fallback silently replaces Gloo.

Memory is relational: consent provenance, availability, qualifications, events, offers, approvals, messages and run/step logs. Retrieval uses scoped database queries, not a vector database. Only authorized, relevant conversation history enters model context. Secrets and transport sessions remain outside Git.

For a rough inference budget, the historical 25-case live evaluation averaged 18,390 input and 802 output tokens per case. Applying published underlying-model rates gives approximately **$0.006–$0.067 per comparable case**, or **$0.62–$6.72 per 100 cases**. This is a pricing proxy, not a measured Gloo bill or a typical single-text cost; hosting, retries and transport are additional. Rate references: [GPT-5 mini](https://developers.openai.com/api/docs/models/gpt-5-mini) and [Claude Sonnet 4.6](https://platform.claude.com/docs/en/models/sonnet-4-6/overview).

Production transport is planned through Twilio with a dedicated number per church, pending A2P 10DLC brand/campaign registration and approval. Provisioning and production delivery are outside this submission's verified evidence.

## Tools and permissions

| Tool or data source | Permitted work | Explicit boundary |
|---|---|---|
| Gloo and scoped records | Interpret logistics, compose requests, explain supplied facts | No counseling, diagnoses, pastoral judgments or invented facts |
| Replacement tools | Inspect eligibility, confirm reserved IDs, propose asks, schedule policy timers | No arbitrary recipients, qualification changes or shortened deadlines |
| Send gate and transport | Submit the approved recipient/body within authorized scope | No opt-out bypass, changed approved copy, blind retry or unapproved contact |
| Schedule and coordinator tools | Inspect, repair drafts and propose record changes | No self-approval, irreversible publication or record-of-record write without review |
| Calendar and Planning Center | Authorized reads/imports; prepare mapped proposals | No unrestricted remote writes or treating directory access as consent |
| Capacity and care workflow | Surface evidence and create internal handoffs | No diagnosis, automatic crisis contact or inferred spiritual authority |

## Evaluation

The evaluation set contains **25 hand-built workflow cases**, not a standardized benchmark. Pass criteria inspect routing, consent state, selected recipients, approval state, assignment counts and messages. Cases include cancellation, decline, expiry, two simultaneous acceptances, unavailable candidates, restricted roles, partial availability, STOP/START, sensitive input, quiet hours and Gloo failure. Mock delivery cannot count as native delivery.

The original live Gloo run passed 23/25. Restricted-role outreach could be reported as progressing without a completed ask; sensitive guarded output could lose a clear cancellation. Completion checks and the cancellation backstop addressed those failures. Separate targeted real-Gloo retests passed each repaired case; they are not a fresh 25/25 run.

Fresh October 6 evidence is recorded in the appendix and package status. The backend passed 2,879 tests with one documented expected failure; the frontend passed 129 tests. The frozen standalone replay passed 13/25 against the integrated source: several older fixtures and expectations conflict with newer consent, selection and reply behavior. We retain that result. Current adapted contract checks and the algorithm's synthetic tests are distinct evidence. The coherent fictional rehearsal passed with scripted Gloo and mock delivery; it exercises exact approval, stale hashes, deduplication and review expiry, but intentionally does not exercise replacement ranking. Connector failures and dependency findings remain visible. No claim of a completed new cloud end-to-end acceptance is made from these tests.

## Guardrails and human handoff

Consent provenance, STOP suppression, clearance, availability, serving limits, quiet hours, care holds and review gates run outside prompts. Recognized sensitive input is held before a model request; uncertain interpretation cannot authorize contact. A human handles care and crisis situations. No financial or pastoral tools exist. Scripture is unnecessary to this workflow; it must not invent quotations or theology.

Competition operation requires exact human confirmation for communications and changes to official records. Confirmation binds the recipient, body or proposed change and current source state. Changed content, expired review or stale facts require fresh review. This mode is configurable and defaults off in source, so deployment must explicitly enable and verify it. A queued or submitted message is not a delivery receipt.

## Reproduction

Obtain authorized access to [the private repository](https://github.com/jacobthebaer-lab/text-monkey), use `codex/complete-text-monkey`, and pin the checkpoint above. Install Python 3.11+ requirements and the locked Node dependencies. Copy `.env.example` privately. Run backend/frontend tests with `SMS_PROVIDER=mock`, `LIVE_SMS=false` and `AUTOMATION_ENABLED=false`; use a new SQLite database for synthetic seeding. The README contains exact commands.

Offline tests need no church credentials. Real model checks require a private Gloo key. Connected operation additionally requires admin authentication, authorized database and Planning Center credentials where used, transport credentials, explicit confirmation configuration and an independently verified deployment. Never commit private conversations, phones, cookies, databases or native receipts.

Remaining gaps include measured impact, frozen-replay alignment, connector failures, deployed approval and delivery checks, and production registration.
