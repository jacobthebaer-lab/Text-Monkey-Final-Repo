# Feature universe reconciliation, October 6, 2026

Reviewed integration: `e205d8b06cbae7e0239e1a8c4b57a15de91e35ac`. All **183 feature IDs**, **15 systems**, **10 decision paths** and the **347 original input mappings** are preserved. The original inventory baseline remains intact so later source changes continue to require review.

This is an inventory and source-evidence reconciliation. “Source-backed code” means the recorded implementation and repository evidence exist in this snapshot. It does not mean every behavior was independently retested today, that a runtime was activated, or that a text reached a device. Existing historical regression and real-model evidence retain their original dates and limits. Active chat and private runtime progress are excluded from the public status feed.

| Classification | Records |
| --- | ---: |
| Source-backed code | 141 |
| Implementation gap | 13 |
| Runtime / acceptance gap | 18 |
| History / future scope | 10 |
| Human submission fact | 1 |

The complete record-by-record classification, source paths, owner groups and remaining requirements are in [reconciliation.json](reconciliation.json). The table below distinguishes current work from retained expansion directions; an inventory record is not automatically a current demo obligation.

## Priority gaps by owner

| Priority | Owner / category | Source-backed remaining work | Acceptance boundary |
| --- | --- | --- | --- |
| P0 | Algorithm integration | [PR38](https://github.com/jacobthebaer-lab/text-monkey/pull/38) is merged in this snapshot. Clyde’s supplied ranking, exact reserved batches, unique-decline replacement and first-winner arbitration are integrated; outreach remains disabled by default. | Source integration and synthetic ranking checks do not establish authorized runtime activation or native replacement delivery. |
| P0 | Gloo conversation and profile sync | Recheck validated partial facts and clarification behavior through the existing intake and profile-source paths after changes. [PR60](https://github.com/jacobthebaer-lab/text-monkey/pull/60) website invitations are already merged. | Intended recipient, consent, source binding, quiet hours, exact review and working Gloo remain authoritative. Do not rebuild the invitation path. |
| P0 | Planning and reminders | Existing monthly collection, three-day followup, three-hour admin digest, day-before reminder and source-bound planning helpers need their intended settings/events and authorized connected runtime. | Parent approval, per-text review, initial-dispatch proof, dedupe and current eligibility remain required. Submission is not device delivery. |
| P0 | Planning Center | Existing staffing and held preference review require exact Services identities/positions/times, schema, ownership and verified notification behavior. Frequency executor has no enabled runtime hook. | Held reviews do not authorize native writes. Verify C versus U coverage and real notification silence separately. |
| P1 | Coordinator agent | `natural-admin`, `new-event-recipe` and `capacity-ai`: current tools propose only add_slots, pause_role and mark_unavailable; capacity narration is deterministic and its model prompt is not invoked. | Model proposals cannot grant qualifications, mutate records without review or initiate texting. |
| P1 | Runtime and acceptance | `device-proof`, `imessage-sms`, `production-readiness`: distinguish active scheduler, intended backend/store, native submission and actual recipient receipt. | This audit performs no runtime activation, model call, delivery test, native write or cloud deployment. |
| P2 | Planning / ministry expansion | Broader ministry recipes, annual-rhythm learning, general seasonal forecasting, repeated-decline detection and true partial/split service coverage remain limited. Existing event recipes and six-week absence detector are already implemented in source. | Validate each required variant before widening demo claims. Do not replace working general configuration with duplicate engines. |
| Human | Submission owner | Source package is integrated in [PR15](https://github.com/jacobthebaer-lab/text-monkey/pull/15). Verify eligibility/acceptance, attendance, rights, final assets, current organizer instructions and actual entry receipt. | A draft description, storyboard or rendering tool cannot establish submission. |

## Scope and publishing corrections

- Automated Google Voice transport is permanently held by [project policy](../../AGENTS.md). Manual volunteer inbox use is separate from the Mac church sender. ID approval, cookies and superadmin login cannot release the automation hold.
- Twilio, multi-site pools, care logistics and broader split-team/per-time PCO support remain retained future scope. They are not current transport or activation tasks.
- The Google Voice record now reads historical/held, rather than a prototype waiting only for identity approval. Cloud controls and generic backend infrastructure do not imply independent texting.
- Submission is partial because sources are prepared; actual submission remains a human fact. Planning Center preference records now include the integrated held-preview and reviewed-executor evidence.
- Twenty-one formerly chat-only records now include concrete repository paths, allowing source changes and matching PRs to invalidate or update their status.
- Seventeen public reviews bind their summaries to the reviewed integration revision. Later relevant source changes invalidate those reviews automatically; reviewed timestamps are not refreshed by the scheduler.
- The latest observed integration/PR status-publisher runs succeeded. No workflow repair or additional background monitor was needed. This patch does not deploy or publish a new runtime. After integration, the existing status workflow can publish applicable review summaries; changed static descriptions require a separate authorized explorer deployment.

## All feature records

| ID | Feature | Classification | Demo scope | Source evidence |
| --- | --- | --- | --- | --- |
| `text-first` | No volunteer app or login | Source-backed code | Current | [README.md](../../README.md) |
| `four-jobs` | Plan, remind, replace, anticipate | Source-backed code | Current | [PLAN.md](../../PLAN.md) |
| `coordinator-control` | Coordinator remains in control | Source-backed code | Current | [docs/HUMAN_CONFIRMATION_MODE.txt](../../docs/HUMAN_CONFIRMATION_MODE.txt) |
| `single-church` | Shared single-church roster | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `church-brand` | Text Monkey brand system | Source-backed code | Current | [docs/TEXT_MONKEY_BRAND.md](../../docs/TEXT_MONKEY_BRAND.md) |
| `sunday-ministry` | Sunday ministry coverage | Implementation gap | Current | [data/event_types.json](../../data/event_types.json) |
| `midweek-ministry` | Midweek program coverage | Implementation gap | Current | [data/event_types.json](../../data/event_types.json) |
| `community-ministry` | Community outreach staffing | Implementation gap | Current | [data/event_types.json](../../data/event_types.json) |
| `seasonal-events` | Seasonal events and setup crews | Implementation gap | Current | [data/event_types.json](../../data/event_types.json) |
| `admin-auth` | Verified administrator access | Source-backed code | Current | [app/web/texty.py](../../app/web/texty.py) |
| `account-recovery` | Password recovery & sessions | Source-backed code | Current | [tests/test_editor_account_acceptance.py](../../tests/test_editor_account_acceptance.py) |
| `church-setup` | Three-step church setup | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `setup-resume` | Save, resume & checklist | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `saved-preferences` | Scheduling preferences | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `setup-isolation` | Owner-scoped setup storage | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `csv-import` | CSV contact import | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `xlsx-import` | Excel workbook import | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `phone-import` | Selected phone contact exports | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `import-preview` | Import review & duplicate safety | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `contact-staging` | Consent-safe staged contacts | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `field-mapping` | Import field mapping | Source-backed code | Current | [docs/ADMIN_SETUP.md](../../docs/ADMIN_SETUP.md) |
| `import-idempotency` | Safe repeat imports | Source-backed code | Current | [app/web/admin_setup.py](../../app/web/admin_setup.py) |
| `remove-staged-contact` | Remove staged contact | Source-backed code | Current | [app/web/admin_setup.py](../../app/web/admin_setup.py) |
| `import-templates` | Templates and fictional sample | Source-backed code | Current | [web/texty/public/setup.js](../../web/texty/public/setup.js) |
| `text-identity` | JOIN and real sender identity | Source-backed code | Current | [app/core/signup.py](../../app/core/signup.py) |
| `explicit-consent` | Explicit consent & opt-out | Source-backed code | Current | [app/core/signup.py](../../app/core/signup.py) |
| `role-interests` | Role interests & Anything | Source-backed code | Current | [docs/EXACT_SIGNUP_COPY.md](../../docs/EXACT_SIGNUP_COPY.md) |
| `start-setup` | Start / restart text setup | Source-backed code | Current | [docs/MVP.md](../../docs/MVP.md) |
| `adaptive-intake` | Personalized missing-fact recovery | Source-backed code | Current | [docs/ADAPTIVE_SIGNUP_HANDOFF.md](../../docs/ADAPTIVE_SIGNUP_HANDOFF.md) |
| `quiet-completion` | Silent preference completion | Source-backed code | Current | [docs/QUIET_SIGNUP_RECOVERY.md](../../docs/QUIET_SIGNUP_RECOVERY.md) |
| `essential-conversation` | Essential volunteer messages only | Source-backed code | Current | [app/core/outbound_conversation.py](../../app/core/outbound_conversation.py) |
| `booking-question` | Ask about my schedule | Source-backed code | Current | [app/core/booking_status.py](../../app/core/booking_status.py) |
| `serving-request` | Volunteer-initiated serving request | Source-backed code | Current | [app/core/serving_requests.py](../../app/core/serving_requests.py) |
| `editable-copy` | Onboarding copy editor | Source-backed code | Current | [docs/ONBOARDING_COPY.md](../../docs/ONBOARDING_COPY.md) |
| `copy-owner` | Account-to-conversation copy binding | Source-backed code | Current | [docs/ONBOARDING_COPY.md](../../docs/ONBOARDING_COPY.md) |
| `literal-signup` | Literal signup copy and substitutions | Source-backed code | Current | [docs/EXACT_SIGNUP_COPY.md](../../docs/EXACT_SIGNUP_COPY.md) |
| `initial-disclosure` | Initial disclosure and consent modes | Source-backed code | Current | [app/core/signup_copy.py](../../app/core/signup_copy.py) |
| `signup-status` | Setup stages and eligibility | Source-backed code | Current | [app/core/onboarding.py](../../app/core/onboarding.py) |
| `emoji-style` | Restrained signup personality | Source-backed code | Current | [app/core/signup_responder.py](../../app/core/signup_responder.py) |
| `natural-days` | Natural dates, weekdays & services | Source-backed code | Current | [docs/NATURAL_AVAILABILITY_HANDOFF.md](../../docs/NATURAL_AVAILABILITY_HANDOFF.md) |
| `date-exclusions` | Date and month exclusions | Source-backed code | Current | [docs/NATURAL_AVAILABILITY_HANDOFF.md](../../docs/NATURAL_AVAILABILITY_HANDOFF.md) |
| `role-windows` | Role-specific recurring windows | Source-backed code | Current | [app/core/recurring_availability.py](../../app/core/recurring_availability.py) |
| `group-context` | Named group / event context | Source-backed code | Current | [docs/EVENT_ROLE_AVAILABILITY_CONTRACT.md](../../docs/EVENT_ROLE_AVAILABILITY_CONTRACT.md) |
| `event-relative` | Follow the real event schedule | Source-backed code | Current | [docs/EVENT_ROLE_AVAILABILITY_CONTRACT.md](../../docs/EVENT_ROLE_AVAILABILITY_CONTRACT.md) |
| `global-frequency` | Global monthly serving preference | Source-backed code | Current | [docs/NULL_FREQUENCY_HANDOFF.md](../../docs/NULL_FREQUENCY_HANDOFF.md) |
| `role-frequency` | Individual role frequency caps | Source-backed code | Current | [docs/EVENT_ROLE_AVAILABILITY_CONTRACT.md](../../docs/EVENT_ROLE_AVAILABILITY_CONTRACT.md) |
| `partial-facts` | Validated partial intake snapshots | Source-backed code | Current | [docs/NULL_FREQUENCY_HANDOFF.md](../../docs/NULL_FREQUENCY_HANDOFF.md) |
| `hard-eligibility` | Deterministic eligibility gate | Source-backed code | Current | [app/core/eligibility.py](../../app/core/eligibility.py) |
| `clearance` | Admin-verified qualifications | Source-backed code | Current | [PLAN.md](../../PLAN.md) |
| `fairness-ranking` | Fairness signals and Clyde recipient ranking | Source-backed code | Current | [app/core/ranking.py](../../app/core/ranking.py) |
| `serve-together` | Serve-with preferences | Source-backed code | Current | [PLAN.md](../../PLAN.md) |
| `flexible-availability` | Flexible availability | Source-backed code | Current | [app/core/onboarding.py](../../app/core/onboarding.py) |
| `timezone-dst` | Timezone and DST correctness | Source-backed code | Current | [app/core/recurring_availability.py](../../app/core/recurring_availability.py) |
| `conditional-preferences` | Preserved conditional preferences | Source-backed code | Current | [app/core/onboarding.py](../../app/core/onboarding.py) |
| `learned-rhythm` | Learn each volunteer's recurring rhythm | Implementation gap | Current | [app/agents/planning_agent.py](../../app/agents/planning_agent.py) |
| `event-recipes` | Event types, recipes & role minima | Source-backed code | Current | [PLAN.md](../../PLAN.md) |
| `monthly-collection` | Monthly availability collection | Runtime / acceptance gap | Current | [docs/AVAILABILITY_PARENT_REVIEW.md](../../docs/AVAILABILITY_PARENT_REVIEW.md) |
| `parent-review` | Owner-bound collection approval | Source-backed code | Current | [docs/AVAILABILITY_PARENT_REVIEW.md](../../docs/AVAILABILITY_PARENT_REVIEW.md) |
| `explicit-preparation` | One-recipient preparation | Source-backed code | Current | [app/web/planning_workflows.py](../../app/web/planning_workflows.py) |
| `availability-followup` | Three-day availability followup | Runtime / acceptance gap | Current | [docs/GLOO_PLANNING_HANDOFF.md](../../docs/GLOO_PLANNING_HANDOFF.md) |
| `greedy-plan` | Constrained monthly draft | Source-backed code | Current | [app/core/scheduler.py](../../app/core/scheduler.py) |
| `gloo-plan-review` | Gloo schedule review & repairs | Source-backed code | Current | [app/agents/planning_agent.py](../../app/agents/planning_agent.py) |
| `plan-publication` | Per-assignment publication review | Source-backed code | Current | [docs/GLOO_PLANNING_HANDOFF.md](../../docs/GLOO_PLANNING_HANDOFF.md) |
| `planning-ui` | Schedule workflow controls | Source-backed code | Current | [web/texty/public/planning-workflows.js](../../web/texty/public/planning-workflows.js) |
| `unknown-events` | Unknown calendar event escalation | Source-backed code | Current | [PLAN.md](../../PLAN.md) |
| `new-event-recipe` | Create new event or recipe by conversation | Implementation gap | Current | [PLAN.md](../../PLAN.md) |
| `proactive-lookahead` | Four-to-eight-week proactive planning | Implementation gap | Current | [app/jobs.py](../../app/jobs.py) |
| `seasonal-planning` | Season-aware staffing | Implementation gap | Current | [data/event_types.json](../../data/event_types.json) |
| `cancel-match` | Resolve the cancelled assignment | Source-backed code | Current | [app/core/inbound.py](../../app/core/inbound.py) |
| `cancel-logistics` | Cancellation changes own schedule | Source-backed code | Current | [docs/HUMAN_CONFIRMATION_MODE.txt](../../docs/HUMAN_CONFIRMATION_MODE.txt) |
| `fill-urgency` | Urgency & optional coverage | Source-backed code | Current | [app/agents/fill_agent.py](../../app/agents/fill_agent.py) |
| `gloo-selection` | Clyde selection and Gloo replacement composition | Runtime / acceptance gap | Current | [README.md](../../README.md) |
| `sequential-offers` | Bounded batch response-window offers | Runtime / acceptance gap | Current | [docs/RESPONSE_WINDOWS.md](../../docs/RESPONSE_WINDOWS.md) |
| `dispatch-deadline` | Dispatch-based response deadlines | Source-backed code | Current | [app/core/offer_windows.py](../../app/core/offer_windows.py) |
| `acceptance` | First eligible affirmative reply | Source-backed code | Current | [docs/MVP.md](../../docs/MVP.md) |
| `partial-response` | Partial & unclear replies | Implementation gap | Current | [PLAN.md](../../PLAN.md) |
| `contact-limits` | No repeated volunteer pressure | Source-backed code | Current | [docs/MVP.md](../../docs/MVP.md) |
| `fill-approvals` | Restricted-role approval | Source-backed code | Current | [docs/MVP.md](../../docs/MVP.md) |
| `fill-escalation` | Unfilled / uncertain coordinator tasks | Source-backed code | Current | [docs/RESPONSE_WINDOWS.md](../../docs/RESPONSE_WINDOWS.md) |
| `old-tranches` | Historical batch/tranche design | History / future scope | Outside current demo | [PLAN.md](../../PLAN.md) |
| `offer-expiry` | Expiry and next-candidate fallback | Source-backed code | Current | [app/agents/fill_agent.py](../../app/agents/fill_agent.py) |
| `one-slot-one-person` | One person per slot | Source-backed code | Current | [app/agents/fill_agent.py](../../app/agents/fill_agent.py) |
| `offer-closure` | Close other offers when the shift is filled | Runtime / acceptance gap | Current | [app/agents/fill_agent.py](../../app/agents/fill_agent.py) |
| `sms-role-approval` | Sensitive-role outreach approval by text | Runtime / acceptance gap | Current | [app/core/send_gate.py](../../app/core/send_gate.py) |
| `placement-notice` | Actual scheduled-placement notice | Source-backed code | Current | [app/core/outbound_conversation.py](../../app/core/outbound_conversation.py) |
| `literal-reminder` | Exact day-before reminder | Source-backed code | Current | [docs/EXACT_DAY_BEFORE_REMINDER.md](../../docs/EXACT_DAY_BEFORE_REMINDER.md) |
| `reminder-timing` | Church-local day-before timing | Source-backed code | Current | [docs/EXACT_DAY_BEFORE_REMINDER.md](../../docs/EXACT_DAY_BEFORE_REMINDER.md) |
| `admin-enrollment` | Admin mobile enrollment & pause | Source-backed code | Current | [app/web/admin_setup.py](../../app/web/admin_setup.py) |
| `admin-readiness` | Real connection readiness | Source-backed code | Current | [app/web/admin_setup.py](../../app/web/admin_setup.py) |
| `connection-check` | Saved-recipient connection check | Source-backed code | Current | [docs/DEMO_ACCEPTANCE_REVIEW.md](../../docs/DEMO_ACCEPTANCE_REVIEW.md) |
| `three-hour` | Three-hour pre-event update | Source-backed code | Current | [app/core/notifications.py](../../app/core/notifications.py) |
| `coverage-digests` | Combined staffing digests | Source-backed code | Current | [docs/MVP.md](../../docs/MVP.md) |
| `saturday-summary` | Saturday coordinator summary | Source-backed code | Current | [app/core/reminders.py](../../app/core/reminders.py) |
| `notification-outbox` | Durable notification outbox | Source-backed code | Current | [app/core/notifications.py](../../app/core/notifications.py) |
| `leader-updates` | Notify ministry leaders and keep an audit trail | Source-backed code | Current | [app/core/reminders.py](../../app/core/reminders.py) |
| `sensitive-routing` | Sensitive-text detection | Source-backed code | Current | [app/core/care.py](../../app/core/care.py) |
| `human-care` | Human-only personal care | Source-backed code | Current | [docs/HUMAN_CONFIRMATION_MODE.txt](../../docs/HUMAN_CONFIRMATION_MODE.txt) |
| `single-point` | Single point of failure | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `burnout` | Burnout / excess load | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `dropoff` | Volunteer drop-off | Implementation gap | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `expiry` | Expiring qualifications | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `chronic-gaps` | Chronic coverage gaps | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `untapped` | Untapped volunteers | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `unused-skills` | Unused skills | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `growing-needs` | Growing role demand | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `capacity-ai` | AI narration of capacity flags | Implementation gap | Current | [docs/AGENT_BUILD.md](../../docs/AGENT_BUILD.md) |
| `ministry-rebalance` | Ministry pool imbalance | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `flag-management` | Flag review and evidence | Source-backed code | Current | [app/agents/capacity_agent.py](../../app/agents/capacity_agent.py) |
| `dashboard` | Coverage dashboard | Source-backed code | Current | [docs/MVP.md](../../docs/MVP.md) |
| `calendar-roster` | Calendar, roles & volunteer roster | Source-backed code | Current | [web/texty/public/app.js](../../web/texty/public/app.js) |
| `review-queue` | Approvals & review queue | Source-backed code | Current | [docs/HUMAN_CONFIRMATION_MODE.txt](../../docs/HUMAN_CONFIRMATION_MODE.txt) |
| `natural-admin` | Natural-language coordinator commands | Implementation gap | Current | [app/agents/admin_agent.py](../../app/agents/admin_agent.py) |
| `human-reply` | Reviewed administrative reply | Source-backed code | Current | [app/web/texty.py](../../app/web/texty.py) |
| `needs-map` | Needs and qualification map | Source-backed code | Current | [app/web/templates/needs.html](../../app/web/templates/needs.html) |
| `audit-viewer` | Agent run & session log viewer | Source-backed code | Current | [app/web/templates/runs.html](../../app/web/templates/runs.html) |
| `mobile-keyboard` | Mobile and keyboard accessibility | Source-backed code | Current | [web/texty/public/accessibility.js](../../web/texty/public/accessibility.js) |
| `removed-simulator` | Historical simulator & demo controls | History / future scope | Outside current demo | [PLAN.md](../../PLAN.md) |
| `profile-edit` | Add and edit volunteer profiles | Source-backed code | Current | [web/texty/public/app.js](../../web/texty/public/app.js) |
| `coverage-view` | Shifts and coverage | Source-backed code | Current | [web/texty/public/app.js](../../web/texty/public/app.js) |
| `conversation-history` | Conversation history | Source-backed code | Current | [web/texty/public/app.js](../../web/texty/public/app.js) |
| `live-refresh` | Automatic dashboard refresh | Source-backed code | Current | [web/texty/public/app.js](../../web/texty/public/app.js) |
| `gloo-only` | Gloo-only language layer | Source-backed code | Current | [README.md](../../README.md) |
| `no-em-dash` | Zero outgoing em dashes | Source-backed code | Current | [app/core/message_style.py](../../app/core/message_style.py) |
| `central-send-gate` | One outbound policy gate | Source-backed code | Current | [app/core/send_gate.py](../../app/core/send_gate.py) |
| `quiet-hours` | Quiet hours & direct reply proof | Source-backed code | Current | [docs/MVP.md](../../docs/MVP.md) |
| `exact-text-review` | Exact content review | Source-backed code | Current | [docs/HUMAN_CONFIRMATION_MODE.txt](../../docs/HUMAN_CONFIRMATION_MODE.txt) |
| `exact-record-review` | Before / after record review | Source-backed code | Current | [docs/HUMAN_CONFIRMATION_MODE.txt](../../docs/HUMAN_CONFIRMATION_MODE.txt) |
| `sender-authority` | Narrow sender-authorized changes | Source-backed code | Current | [docs/HUMAN_CONFIRMATION_MODE.txt](../../docs/HUMAN_CONFIRMATION_MODE.txt) |
| `delivery-recheck` | Approval, claim & native preflight | Source-backed code | Current | [docs/HUMAN_CONFIRMATION_MODE.txt](../../docs/HUMAN_CONFIRMATION_MODE.txt) |
| `privacy-scope` | Scoped conversation history | Source-backed code | Current | [docs/TEST_SESSION_PRIVACY.txt](../../docs/TEST_SESSION_PRIVACY.txt) |
| `dedupe-recovery` | Durable dedupe & uncertain holds | Source-backed code | Current | [docs/MAC_MESSAGES.md](../../docs/MAC_MESSAGES.md) |
| `sender-reply-window` | Immediate reply to the sender | Source-backed code | Current | [app/core/send_gate.py](../../app/core/send_gate.py) |
| `literal-copy` | Literal approved text protection | Source-backed code | Current | [app/core/signup_copy.py](../../app/core/signup_copy.py) |
| `unclear-escalation` | Low-confidence clarification | Source-backed code | Current | [app/core/inbound.py](../../app/core/inbound.py) |
| `review-audit` | Review and suppression audit | Source-backed code | Current | [app/core/confirmations.py](../../app/core/confirmations.py) |
| `mac-transport` | First-party Mac Messages transport | Source-backed code | Current | [docs/MAC_MESSAGES.md](../../docs/MAC_MESSAGES.md) |
| `worker-recovery` | Connector recovery & diagnostics | Source-backed code | Current | [docs/CLYDE_HANDOFF.md](../../docs/CLYDE_HANDOFF.md) |
| `runtime-jobs` | Independent background jobs | Source-backed code | Current | [app/main.py](../../app/main.py) |
| `private-store` | Private SQLite / Supabase storage | Source-backed code | Current | [docs/TEXTY.md](../../docs/TEXTY.md) |
| `profile-sync` | Sender-authorized profile mirroring | Source-backed code | Current | [docs/PROFILE_SYNC.md](../../docs/PROFILE_SYNC.md) |
| `identity-sync` | Identity-only partial publication | Source-backed code | Current | [docs/PROFILE_SYNC.md](../../docs/PROFILE_SYNC.md) |
| `gcal` | Read-only Google Calendar import | Source-backed code | Current | [app/integrations/gcal.py](../../app/integrations/gcal.py) |
| `static-cloud` | Static Cloudflare preview | Source-backed code | Current | [docs/CLOUDFLARE_DEMO.md](../../docs/CLOUDFLARE_DEMO.md) |
| `historical-twilio` | Historical Twilio adapter | History / future scope | Outside current demo | [PLAN.md](../../PLAN.md) |
| `imessage-sms` | iMessage and carrier SMS distinction | Runtime / acceptance gap | Current | [docs/MAC_MESSAGES.md](../../docs/MAC_MESSAGES.md) |
| `scoped-inbound` | Scoped inbound ingestion | Source-backed code | Current | [app/integrations/mac_messages.py](../../app/integrations/mac_messages.py) |
| `profile-cloud-mapping` | Profile cloud role mapping | Source-backed code | Current | [docs/PROFILE_SYNC.md](../../docs/PROFILE_SYNC.md) |
| `pause-resume` | Pause and resume texting | Runtime / acceptance gap | Current | [app/core/admin_text_enrollment.py](../../app/core/admin_text_enrollment.py) |
| `pco-import` | Planning Center service & need import | Source-backed code | Current | [docs/PLANNING_CENTER.md](../../docs/PLANNING_CENTER.md) |
| `pco-webhook` | Signed Planning Center webhooks | Source-backed code | Current | [docs/PLANNING_CENTER.md](../../docs/PLANNING_CENTER.md) |
| `pco-mappings` | Explicit staffing identity mappings | Source-backed code | Current | [docs/PLANNING_CENTER_STAFFING.md](../../docs/PLANNING_CENTER_STAFFING.md) |
| `pco-writeback` | Staffing confirmation / cancellation sync | Runtime / acceptance gap | Current | [docs/PLANNING_CENTER_STAFFING.md](../../docs/PLANNING_CENTER_STAFFING.md) |
| `pco-reconciliation` | Conflict-safe PCO reconciliation | Source-backed code | Current | [docs/PLANNING_CENTER_STAFFING.md](../../docs/PLANNING_CENTER_STAFFING.md) |
| `pco-notifications` | Suppress PCO notification preparation | Runtime / acceptance gap | Current | [docs/PLANNING_CENTER_STAFFING.md](../../docs/PLANNING_CENTER_STAFFING.md) |
| `pco-future` | Split-team and per-time staffing | History / future scope | Outside current demo | [docs/PLANNING_CENTER_STAFFING_CONTRACT.md](../../docs/PLANNING_CENTER_STAFFING_CONTRACT.md) |
| `pco-open-needs` | Open position import | Source-backed code | Current | [docs/PLANNING_CENTER.md](../../docs/PLANNING_CENTER.md) |
| `pco-idempotent-import` | Idempotent plan refresh | Source-backed code | Current | [docs/PLANNING_CENTER.md](../../docs/PLANNING_CENTER.md) |
| `pco-position-mappings` | Explicit position and time mapping | Runtime / acceptance gap | Current | [docs/PLANNING_CENTER_STAFFING.md](../../docs/PLANNING_CENTER_STAFFING.md) |
| `pco-coverage` | Verified PCO coverage | Runtime / acceptance gap | Current | [docs/PLANNING_CENTER_STAFFING.md](../../docs/PLANNING_CENTER_STAFFING.md) |
| `pco-preferences` | Planning Center memberships and preference mapping | Runtime / acceptance gap | Current | [docs/PLANNING_CENTER_HELD_RUNTIME.md](../../docs/PLANNING_CENTER_HELD_RUNTIME.md) |
| `future-scope` | Explicit non-goals / future paths | History / future scope | Outside current demo | [PLAN.md](../../PLAN.md) |
| `cloud-independent` | Cloud runtime with the Mac off | Runtime / acceptance gap | Outside current demo | [AGENTS.md](../../AGENTS.md) |
| `cloud-superadmin` | Cloud superadmin control center | Runtime / acceptance gap | Outside current demo | [AGENTS.md](../../AGENTS.md) |
| `google-voice-prototype` | Google Voice prototype transport | History / future scope | Outside current demo | [AGENTS.md](../../AGENTS.md) |
| `production-twilio` | Registered Twilio number for every church | History / future scope | Outside current demo | [AGENTS.md](../../AGENTS.md) |
| `cloud-cost` | Free prototype hosting with few accounts | Runtime / acceptance gap | Outside current demo | [docs/CLOUD_FREE_HOSTING.md](../../docs/CLOUD_FREE_HOSTING.md) |
| `historical-transports` | Earlier transport alternatives | History / future scope | Outside current demo | [AGENTS.md](../../AGENTS.md) |
| `care-logistics` | Human care logistics | History / future scope | Outside current demo | [PLAN.md](../../PLAN.md) |
| `multisite-pools` | Multi-site and partner volunteer pools | History / future scope | Outside current demo | [PLAN.md](../../PLAN.md) |
| `synthetic-preview` | Portable fictional church preview | Source-backed code | Current | [README.md](../../README.md) |
| `regression-suite` | Backend & frontend regression suites | Source-backed code | Current | [tests](../../tests) |
| `fixed-evals` | Fixed evaluation scenarios | Source-backed code | Current | [docs/EVALUATION.md](../../docs/EVALUATION.md) |
| `live-gloo-proof` | Separate real Gloo evidence | Source-backed code | Current | [docs/GLOO_VERIFICATION.md](../../docs/GLOO_VERIFICATION.md) |
| `device-proof` | Native device delivery acceptance | Runtime / acceptance gap | Current | [README.md](../../README.md) |
| `production-readiness` | Always-on connected operation | Runtime / acceptance gap | Current | [docs/TEXTY.md](../../docs/TEXTY.md) |
| `practitioner-study` | Measure coordinator impact | Implementation gap | Outside current demo | [docs/AGENT_BUILD.md](../../docs/AGENT_BUILD.md) |
| `submission` | Hackathon demo and submission | Human submission fact | Current | [docs/AGENT_BUILD.md](../../docs/AGENT_BUILD.md) |
| `sample-booking` | Interactive sample booking | Source-backed code | Current | [web/texty/public/app.js](../../web/texty/public/app.js) |
| `fictional-church` | Expanded fictional church dataset | Source-backed code | Current | [docs/FICTIONAL_CHURCH_DEMO.md](../../docs/FICTIONAL_CHURCH_DEMO.md) |
| `legacy-admin` | Legacy operations and admin pages | Source-backed code | Current | [app/web/routes.py](../../app/web/routes.py) |
| `test-simulator` | Mock simulator and fake clock | Source-backed code | Current | [app/web/routes.py](../../app/web/routes.py) |
| `privacy-boundary` | Private-data and credential boundary | Source-backed code | Current | [AGENTS.md](../../AGENTS.md) |
| `shared-repository` | Shared repository and portable collaboration | Source-backed code | Current | [AGENTS.md](../../AGENTS.md) |
