# Fictional church: three-minute demo and verification

Prepared October 3, 2026. Synthetic checks below ran in the isolated
`codex/fictional-church-dataset` checkout, based on
`fb95b34101a1ee2bd79dd4515911ce693ec01457`; the release commit is the commit
containing this document and its fixture tool/tests.
The three-minute format is a preparation choice, not a verified hackathon rule.
No official judging rubric, deadline, or presentation limit was found in the
repository or the latest ten master-chat turns reviewed. The older
[agent description](AGENT_BUILD.md#evaluation-and-reproduction) mentions an unfinished
90-second video; it does not establish an official submission requirement.

## Dataset and claim boundaries

The requested synthetic schedule is October 4–31, 2026, in `America/Denver`:
eight Sunday services at 9 AM and 11 AM, twelve weekly youth/women/men gatherings,
four additional events/service opportunities, and 100 fictional contacts.
During this range Denver uses MDT (UTC−06:00): 9 AM is 15:00 UTC and 11 AM is
17:00 UTC. Retain the timezone rather than hardcoding this offset for future
dates; the November DST transition changes it.

Actual creation and import were verified in organization `545298`, with no paid
upgrade. Services allows five people and has one enrolled/four remaining before
and after the seed. All 100 fake people were successfully created as **inactive
People-directory profiles**; none were enrolled in Services or native Groups.
The free Services roster therefore does not contain 100 volunteers.

| Surface | Verified dataset | Receipt |
| --- | --- | --- |
| Planning Center People | 100 inactive profiles, reserved `202-555-0100`–`0199` and `example.test` contacts | [Creation](evidence/fictional-church/people-created.json), [repeat reconciliation](evidence/fictional-church/people-repeat.json) |
| Planning Center Services | Separate service type `1826248`; 24 private plans/times; six empty teams/positions; 40 needed-position records totaling 68 open slots | [Staffing creation](evidence/fictional-church/schedule-staffing.json), [repeat/readback](evidence/fictional-church/schedule-repeat.json) |
| Planning Center Groups | Three native unlisted, closed groups with 12 weekly gatherings, zero members, no automated reminders/attendance requests | [Groups readback](evidence/fictional-church/groups-readback.json) |
| Isolated Text Monkey SQLite | 24 imported events, 68 open shifts, 100 staged contacts; no live volunteers, assignments, consent or messages | [Import](evidence/fictional-church/local-import.json), [repeat](evidence/fictional-church/local-repeat.json), [fresh read-only database check](evidence/fictional-church/local-readback.json) |

The twelve Groups gatherings also have Services-plan mirrors so the current
Services importer can display their staffing alongside Sundays and community
events. They count once among Text Monkey's 24 events. This is not a Groups
roster sync. Weekly youth is Wednesday 6:30–8 PM; women Tuesday 7–8:30 PM; men
Thursday 7–8:30 PM, all Denver. Additional events are food-pantry packing Oct 10,
neighborhood cleanup Oct 17, community dinner Oct 23 and fall festival Oct 31.

Protected live service `1826236` and plans `92466235`/`92466244` are excluded.
Every real operation checks their non-personal schedule/state before and after.
Its receipt confirms they remained unchanged during that operation. This does
not freeze the separate live owner against later authorized changes.

The local database is deliberately outside the repository and shared runtime:
`/Users/jacob/Documents/Codex/2026-10-03/task/fictional-church/fictional-church.db`.
Neither the existing live backend nor the browser-local public preview was
reconfigured to use it. The integration owner can select it for a separate
fixture view; do not replace a running real texting database with this file.

All bulk contacts are clearly fictional, use reserved contact values, remain
account-scoped and staged, and receive no SMS consent, clearance, notification
permission, active roster membership, messages, invites, or notifications.
These records provide realistic context, not a pool authorized for outreach.
The isolated test factories below create their own temporary consenting profiles
and verified qualifications; they do not activate the bulk dataset. Existing
live test services, plans, mappings, contacts, and runtime are excluded.

## Three-minute presentation

| Time | Show and say | Evidence required |
| --- | --- | --- |
| 0:00–0:25 | Open the connected church schedule with both Sunday service times and the weekly groups. “A coordinator has a realistic month of ministry to staff.” | Current dataset readback receipt; visible Denver dates/times. Label browser-local preview if that is what is open. |
| 0:25–0:55 | Show an assigned greeter's natural cancellation, the affected slot becoming open, and the replacement-search state. “A text becomes a scoped scheduling request.” | Live owner's actual inbound/application records, or explicitly labelled fixture evidence. Do not invent a participant reply. |
| 0:55–1:30 | Show the eligible candidate decision and the exact outgoing request awaiting coordinator review when required. “Gloo interprets and composes; code checks consent, availability, qualifications, time windows, and approvals.” | Candidate eligibility and exact approval record. An unqualified candidate stays excluded. Bulk staged contacts never become recipients. |
| 1:30–2:05 | Show the selected replacement's affirmative response, one confirmed assignment, and the confirmation's review/delivery status. “The first eligible acceptance fills one slot.” | Actual reply and updated assignment; approval proof. Queued/submitted is not delivered. |
| 2:05–2:30 | Refresh the Text Monkey schedule and coverage. Open the corresponding Planning Center record only if fresh readback proves the outbound change. | Local schedule state is sufficient for a local-update claim. Two-way PCO staffing sync requires an actual write and independent readback; inbound plan sync is a separate claim. |
| 2:30–3:00 | Show one rejected double booking and a duplicate replay that leaves one assignment. Close on the coordinator's reduced manual chasing, without a measured time-savings claim. | Passing named synthetic tests below and their source checkpoint. State laptop Messages and current integration limits plainly. |

Use the existing live owner for the approved Clyde exchange. Any Noah recipient
must already be within that owner's specific authorization. This runbook does
not authorize a new send, recipient, session renewal, background activation, or
rewrite of Jacob's exact signup wording. Do not turn a three-minute presentation
into a deadline for a person to respond.

The live flow is a successful demonstration only after the owner verifies the
actual cancellation, eligible acceptance, schedule state, and matched native
delivery evidence for the relevant messages. Neither the historical device
receipt nor the checks below prove that the current Clyde-to-Noah flow passed.

## Honest fallback

If a live reply, Gloo, Messages, or PCO is unavailable, say: “This next segment is
a deterministic fixture replay with mock delivery.” Show the cancellation,
offer expiry/next candidate, acceptance, and one confirmed local assignment from
the existing tests. They use a scripted Gloo client, a frozen clock, disposable
database, and mock transport. The test named `test_real_cancel_review_ack_yes_journey_does_not_release_sibling`
also uses fixture interpretation and in-process HTTP; “real” in its name is not
native-delivery evidence. Its queue acknowledgment is simulated.

Use [existing sanitized evidence](DEMO_RUNBOOK.md) for historical real-Gloo/mock
composition or native delivery, preserving its original date and scope. A
recording may be used only after the live owner captures an authorized run; none
was created by this task. Label a recorded segment with its capture time and
source/runtime checkpoint. Show the current open gap when an integration did
not finish. Do not portray the static public preview as a connected backend or
claim PCO write-back using only a Text Monkey schedule screenshot.

For a shorter presentation, use 20 seconds of context, 45 seconds of the
cancel/replace/confirm story, and 25 seconds of coverage, constraints, and proof.
This 90-second cut is optional until the actual event requirement is known.

## Synthetic verification receipt

Executed October 3, 2026:
**177 passed in 4.43 seconds**, exit 0 ([receipt](evidence/fictional-church/synthetic-tests.json); 168 workflow checks plus nine fixture checks). No external model/API calls, live database,
native connector, server, or real send was used. The environment overrides were
applied before backend imports. Pytest cache and Python bytecode writes were
disabled; test-local temporary files were managed by pytest.

```sh
env DATABASE_URL=sqlite:// SMS_PROVIDER=mock LIVE_SMS=false \
AUTOMATION_ENABLED=false MAC_BRIDGE_ENABLED=false DEMO_MODE=true \
GLOO_API_KEY= GLOO_API_TOKEN= MAC_BRIDGE_TOKEN= MAC_DEMO_PHONES= \
MAC_TEST_SESSIONS= BACKEND_BRIDGE_KEY= SUPABASE_URL= \
SUPABASE_PUBLISHABLE_KEY= PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python \
-m pytest -p no:cacheprovider \
tests/test_eligibility.py tests/test_fill_agent.py tests/test_offer_windows.py \
tests/test_simultaneous_acceptance.py tests/test_planning_center.py \
tests/test_planning_center_receiver.py tests/test_confirmations.py \
tests/test_consent_approval_acceptance.py tests/test_clock.py \
tests/test_demo_import_package.py tests/test_fictional_church_dataset.py -ra
```

The executed command used the already installed shared virtual environment's
Python, rather than a new environment in this checkout. The command above uses
the portable `.venv/bin/python` spelling; substitute the existing environment
path. No dependency installation was performed.

| Requirement | Passing existing checks |
| --- | --- |
| Cancellation → replacement → confirmation → local assignment | `test_fill_agent.py::test_demo_critical_scenario`; `test_confirmations.py::test_real_cancel_review_ack_yes_journey_does_not_release_sibling` |
| No overlapping booking; one winner under concurrent replies | `test_eligibility.py::test_double_booking_blocks`; `test_fill_agent.py::test_ineligible_yes_gets_thanks_not_assignment`; `test_simultaneous_acceptance.py::test_two_simultaneous_http_acceptances_leave_one_confirmed_winner` |
| Missing, pending, or expired qualifications rejected | `test_eligibility.py`; `test_fill_agent.py::test_gloo_selection_rejects_unqualified_duplicates_and_oversized_batches` |
| No response, decline, and late/reordered replies | `test_fill_agent.py::test_declines_advance_early`; `test_offer_windows.py::test_expiry_is_offer_only_sequential_and_idempotent`; `test_duplicate_yes_and_reordered_no_do_not_cancel_winner` |
| PCO webhook duplicates and retry rollback | `test_planning_center.py::test_signed_sync_deduplicated_no_texts`; `test_webhook_failure_retries_without_receipt` |
| Denver timing and DST elapsed-time behavior | `test_clock.py`; `test_offer_windows.py::test_defaults_and_exact_local_deadline`; `test_dst_elapsed_time_and_fold_copy` |
| Exact human approval, mutable restrictions, and no model-created consent | `test_confirmations.py`; `test_consent_approval_acceptance.py` |
| Staging without live permissions; account separation and duplicate/retry handling | `test_demo_import_package.py` |

The fixture checks additionally cover the 100-person manifest, inactive-only
People creation/retry, duplicate rejection, read-only reconciliation of all
24 completed plans/68 slots, Denver DST preservation, actual importer/staging
code, repeat import without duplicates, and protected/foreign/unowned/symlink
destination rejection. They use synthetic HTTP transport and disposable SQLite.
The real-account receipts above separately establish capacity, creation and
import. Neither type of check proves webhook uptime, native Messages delivery
or outbound PCO staffing write-back. No full suite or real-Gloo rerun was
performed here; earlier model-evaluation misses remain in [EVALUATION.md](EVALUATION.md).

## Reproduce or reconcile the bounded seed

Use the existing private PCO env file and an explicit organization; never copy
credentials into fixtures or Git. Start with a dry-run and live capacity read.
The generator only writes its local fictional manifest/CSV during dry-run:

```sh
python tools/fictional_church_dataset.py dry-run
python tools/fictional_church_dataset.py capacity --env-file PRIVATE_ENV --expected-org 545298
python tools/fictional_church_dataset.py people --env-file PRIVATE_ENV --expected-org 545298 --write-synthetic
python tools/fictional_church_dataset.py schedule --env-file PRIVATE_ENV --expected-org 545298 --write-synthetic
python tools/fictional_church_dataset.py sync --env-file PRIVATE_ENV --expected-org 545298 --service-type-id 1826248 --database /ABSOLUTE/ISOLATED/fictional-church.db
```

Add `--receipt /PATH/TO/receipt.json` to save sanitized operation counts/IDs.
The manifest SHA-256 is
`de33017b45c48a051de460d694a5c01fe2ccf4303fe203bef3309b086a66ed61`.
Existing exact synthetic names are reconciled before creating; ambiguous
duplicates abort. Repeat receipts show zero new people/plans/times/needs and
zero new local events/shifts/contacts. A partially interrupted run should be
reconciled by GET before retrying; network/5xx mutation errors are not blindly
retried. Requests are paced and bounded rate-limit retries respect Retry-After.

Services service-type POST returned HTTP 500 without creating a resource in this
account. The supported UI created the separate service type; the tool safely
adopted its one matching empty onboarding plan. Public APIs expose no
TeamPosition creation or native Groups/Event creation here. In Services UI,
each of the six exact `Synthetic <role>` teams received one same-named empty
position; no leaders or members were added. Native Groups were created through
UI, named `Text Monkey Fictional Church — Youth/Women/Men (Synthetic)`; their
IDs are `3255054`, `3255060`, `3255063`. Reuse these exact groups and their four
October weekly events instead of creating a second recurrence. Keep visibility
unlisted, enrollment closed, chat disabled, database access disabled, reminders,
attendance requests and RSVPs off. Native Groups UI steps are a one-time setup,
not a supported Groups write API or an automated duplicate-safe creator.

Do not mark any bulk contact opted in, qualified or active. Youth interest is an
unverified fictional note, not youth-serving clearance. Keep Messages/Gloo
credentials, real contacts, databases and screenshots of private runtime state
out of the release. The actual live demo owner remains responsible for the
separately authorized participant journey and its delivery evidence.
