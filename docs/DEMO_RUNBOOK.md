# Text Monkey demo runbook

Use Python 3.11+ from the repository root. There are three distinct demonstrations: the public portal preview, real Gloo with fictional data and mock delivery, and an explicitly authorized Messages device test. A preview or a queued text does not prove delivery.

## 1. Credential-free portal preview

```sh
python3 tools/demo_preflight.py
python3 tools/demo_preflight.py --launch-preview --port 58123
```

Open `http://127.0.0.1:58123/texty`. Home, coverage, roster, church setup and Settings use browser-local fictional data. No account, backend database, Gloo key or Messages connection is required. The launcher binds only to loopback and serves allowlisted public assets; backend requests and writes are rejected. Stop with Ctrl-C. If the port is occupied, select another unused local port.

The released portal has no incoming-text simulator, sample-send controls, admin-text preview button or fake pause/resume toggle. Settings should accurately show disconnected texting/Gloo and disabled background delivery. Demonstrate coverage and setup using the fictional browser state; use the replay below to demonstrate conversations. Browser-local preview data remains separate from connected church records.

## 2. Real Gloo, fictional signup, mock delivery

Install backend dependencies in an isolated virtual environment if needed:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tools/demo_preflight.py --mode gloo
```

Preflight only checks dependency and environment-key presence; it makes no Gloo call and never reads `.env`. The signup checker also disables dotenv loading. An authorized private `GLOO_API_KEY` must already be present in the process environment; a key stored only in `.env` will not be loaded. Do not paste or print credentials.

Run the signup checker with safe overrides **before any backend import**. This optional command calls the real Gloo API and consumes its configured credit:

```sh
PYTHON_DOTENV_DISABLED=1 DATABASE_URL=sqlite:// SMS_PROVIDER=mock LIVE_SMS=false \
AUTOMATION_ENABLED=false MAC_BRIDGE_ENABLED=false DEMO_MODE=false \
MAC_BRIDGE_TOKEN= MAC_DEMO_PHONES= MAC_TEST_SESSIONS= \
MAC_TEST_SIGNUP_REPLY_UNTIL= BACKEND_BRIDGE_KEY= \
SUPABASE_URL= SUPABASE_PUBLISHABLE_KEY= \
.venv/bin/python tools/check_synthetic_gloo_signup.py
```

This invokes the actual Gloo API for a fictional conversation: `JOIN`, the disclosed exact starter, `Jordan Demo`, Greeter interest, then Sundays 9–10am twice a month. The ordinary full-name response to the recorded starter establishes consent; there is no mandatory YES step. The replay checks three essential mock replies against the approved exact copy and requires silent completion after saving preferences. It creates no assignments or qualification grants.

The checker forces disposable in-memory SQLite, mock delivery, disabled Mac delivery, scheduling, profile publication and PCO activation before importing the backend, even if inherited environment variables point to a connected runtime. It scopes exact signup copy to its fictional phone and uses the current five-role menu with its clearance requirements. It does not run `app.db.seed`, reset a live database or open Messages.

The script exits nonzero on a failed signup step. By default it reserves a new ignored `evals/reports/signup-<uuid>-logs/` directory; `--output-dir` selects another new directory. Existing paths and symlink ancestors are refused before backend imports or model calls. The final summary prints the exact `synthetic-gloo-signup.json` report path. Check `passed`, actual versus expected routes, `mock_messages` statuses and provider IDs, `consent_provenance`, saved role/day/time/frequency, `gloo_audit`, `real_gloo_usage` and `real_messages_sent: 0`. A composed body or provider acknowledgment alone cannot pass as mock delivery. Gloo outage, changed exact copy, an em dash or suppressed intake produces a failed receipt with no template substitute. Offline tests inject a scripted Gloo double and label its usage separately; that evidence is not a real-Gloo run. This result does not prove real-device delivery or background scheduling.

The integrated `tools/demo_admin_status.py` covers three-hour event status. Read [its replay instructions](demo_admin_status.md) for the synthetic and explicit real-Gloo modes. Its fictional events, fake clock and mock provider show all-set status, gaps, approvals, quiet-hour deferral and duplicate prevention. Distinguish scripted Gloo from a real-Gloo run; neither sends an actual text.

## 3. Actual Messages device test

```sh
.venv/bin/python tools/demo_preflight.py --mode messages
```

This intentionally returns exit code 2, `requires_runtime_review`. Local source files and a key cannot establish an active connection, eligible recipient, selected sender line or delivery. Preflight does not read private worker values, renew a session, start a worker or contact anyone.

For a specifically authorized device test, use [Mac transport setup](MAC_MESSAGES.md) with the existing saved recipient, consenting profile, selected receiving line/service and bounded session. Retrieve saved event/staffing/admin settings from the connected console. Keep synthetic replay reports in ignored `evals/reports/`; retain private worker configuration/checkpoints in ignored `.mac-bridge.json` and `.mac-state/`. Do not seed/reset the connected database or reuse browser-preview records as consent.

Check the connected console's blockers and backend/worker state before proceeding. An expired session, opt-out, missing consent or Gloo failure holds delivery. Natural signup replies must be ordinary user text without a typed marker. Any activation or new outbound device test must stay within the user's specific current authorization; this runbook is not authorization to send or extend a test.

Compare the exact Gloo-composed body and app queue/claim/ack records with native Messages evidence on the selected line. `submitted` means accepted by Messages; verify actual delivery separately. An uncertain send requires reconciliation, not an automatic retry. Verify carrier SMS on a real receiving device separately from iMessage. Three-hour admin updates require the live scheduler, enrolled consenting admin, active recipient session, Gloo and Messages; a successful one-off check does not establish background operation.

## Planning Center and final evidence

Planning Center's owner verified real API seeding/sync of synthetic plans/open needs and a live signed webhook through a dedicated receiver; this is separate from the static portal, scheduler and texting. The receiver exposes its health/webhook routes, not the admin API. Private credentials, signing secrets and local runtime state remain outside Git. Use [Planning Center instructions](PLANNING_CENTER.md) and the owner's current receipt when demonstrating this integration; do not infer people import, consent, assignments or message delivery from a successful sync.

Preflight prints fixed labels and booleans only. Exit 0 means the requested preview prerequisites are present, or Gloo prerequisites are configured but unverified; exit 2 means blocked or runtime review required. It never loads configuration files, opens databases, makes network calls or claims fresh runtime evidence. The optional preview launch serves only the public loopback application. Preserve the separate proofs: portal appearance, real Gloo/mock replay, PCO sync/webhook, actual native delivery and scheduler status.

## Offline application rehearsal before recipient selection

```sh
.venv/bin/python tools/rehearse_fictional_workflow.py
```

This tool is offline only by default. It reuses the signup checker in a disposable
SQLite store with MockSMS and a fake clock. Its Gloo replies are scripted fixtures,
not evidence that the real model interpreted the conversation. An inherited funded
Gloo key is cleared before backend imports. No worker, scheduler, Messages bridge,
profile publisher or Planning Center mutation runs.

The fictional scenario follows actual application paths:

1. Jordan sends JOIN, receives the disclosed exact starter, supplies a full name,
   chooses Greeter and saves Sundays 9–10am, twice a month. Completion is silent.
2. Avery supplies only a first name. The application asks only for the missing last
   name, retains the earlier answer, then saves the same role/window quietly.
3. A Greeter assignment for the fictional Sunday Welcome event is supplied
   explicitly as a fixture. The application holds it for an exact record review.
4. The scheduled notice includes Jordan's name and the persisted event, role and
   time. The day-before reminder preserves the approved literal wording, with
   only the saved recipient/role/time substitutions.
5. Jordan cancels through the inbound parser at Saturday 10pm. The booking changes
   under actual sender authorization; acknowledgment chatter stays suppressed.
   Replacement selection stops at the real quiet-hour boundary.
6. At the three-hour boundary, the consenting fictional coordinator's status is
   held for exact review. Avery is then explicitly supplied as the replacement,
   passing the real Greeter eligibility checks and exact record review. A separate
   Child Care eligibility check refuses missing verified clearance.
7. Changed staffing must invalidate an older status review, recapture current
   facts through Gloo and require a fresh exact review. The final status must say
   the single required spot is covered. Repeated ticks must not duplicate it.

No scoring, candidate search, tranches or recipient-selection algorithm is called.
The replacement is a supplied fixture; Clyde's actual selection integration remains
pending. Signup grants neither qualifications nor coordinator/pastor access. The
coordinator's privileges and consent are labeled setup fixtures.

Exact reviews use the real protected application review endpoint with a fictional
authentication dependency in this isolated application. Wrong hashes and duplicate
approvals are rejected. This is not a real Supabase sign-in or Jacob's approval.
`awaiting_approval` is not a queue receipt; MockSMS `sent` plus a `MOCK` provider ID
is simulated delivery, never native delivery. The evidence keeps those states
separate and marks native verification false.

The command writes `fictional-workflow.json` into a new ignored
`evals/reports/workflow-<uuid>-logs/` directory. `--output-dir` accepts only a new
directory with no symlink ancestors. Failed phases produce `passed: false`, partial
timeline and nonzero exit status; do not present an incomplete rehearsal as passing.
Optimized Python (`-O`) is refused because it disables the assertion checks.

A real-model pass is a separate, explicitly authorized Python invocation:
`run(gloo=bounded_real_gloo(isolated_settings), model_provenance="real_gloo")`. It still uses fictional inputs,
MockSMS and the same factual/exact-copy assertions. The supplied settings must keep
storage and transport isolated. The model wrapper permits at most 24 HTTP attempts,
150,000 total input UTF-8 bytes (including instructions) and 1,024 output tokens per
call; retries are disabled. Those are hard resource ceilings, not a dollar-price
quote. Invalid, paraphrased or unavailable model output fails the rehearsal without
manufacturing the expected copy. The ordinary CLI does not expose a real-model
switch, load credentials or authorize that pass.

The complete scripted rehearsal passes with the separately reviewed admin-status
repairs `318b6ae` and `5182c46` on integration base `7691251`. The older 0/1 review
expires with `not_queued`; its body and hash remain unchanged. The unsent notification
returns to pending. An ordinary `notifications.flush_due(ctx)` recaptures current
facts through one new Gloo composition and stages a distinct exact review. Approved
mock delivery links the notification to its actual mock message. This narrower
notification path avoids invoking fill timers or candidate selection.

The expected complete path uses 22 model calls. An offline pass proves application
contracts against scripted responses, not real model behavior or native delivery.
The separate mocked-SDK pass proves the real client protocol under the same hard
budget without a vendor request; its reported real-Gloo usage stays zero.

Initial JOIN may legitimately return `signup_invitation` or `signup_name_needed`:
Gloo can recognize signup intent before a name is supplied. Both are accepted only
with the exact disclosed starter, one recorded/mock-sent reply and no profile yet.
The later name reply must still establish the recorded disclosure/consent provenance.
An initial authorized real-model attempt stopped after two successful responses
because the original harness accepted only the first route. That failed artifact
does not prove the full real-model rehearsal; a mocked-SDK regression covers its
observed response shape without another vendor call.

Real-model checkpoint, October 4, 2026: a final authorized pass on `f80ba06`
passed JOIN and the full-name reply, with the exact starter and interests prompt
recorded as two MockSMS deliveries. Greeter interpretation succeeded, but Gloo's
availability copy contained literal Unicode escape text for the approved emojis.
The existing exact-copy guard held it as `invalid_exact_copy` with no fallback or
third mock message. The pass stopped before the remaining workflow. Across the two
attempts, eight real HTTP requests used 37,086 submitted input UTF-8 bytes and
3,343 reported output tokens, within the original aggregate limits. Private execution
and response evidence preserves both attempts. The complete real-model rehearsal
has **not passed**; the 31 focused offline checks establish synthetic application
and client-protocol behavior only. No native text, ranking or PCO mutation ran.

Model evidence classification is an explicit operator declaration, not a client-type
test or vendor attestation. The offline CLI declares `scripted_gloo`. Injected
clients default to `injected_unverified`; mocked SDK checks declare
`mocked_gloo_protocol` and preserve only protocol usage, with zero
`real_gloo_usage`. An authorized real pass must explicitly declare `real_gloo` and
be supported by its actual execution audit. Do not use that declaration for a
mocked adapter or infer vendor execution from a class name or response shape.
