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

Preflight only checks dependency and environment-key presence; it makes no Gloo call and never reads `.env`. A privately saved `.env` key may therefore be reported absent from the process environment while the existing replay can still load it. Do not paste or print credentials. Use the already configured private Gloo key, or place your own authorized key in ignored `.env`.

Run the existing signup checker with safe overrides **before any backend import**:

```sh
DATABASE_URL=sqlite:// SMS_PROVIDER=mock LIVE_SMS=false \
AUTOMATION_ENABLED=false MAC_BRIDGE_ENABLED=false DEMO_MODE=false \
MAC_BRIDGE_TOKEN= MAC_DEMO_PHONES= MAC_TEST_SESSIONS= \
MAC_TEST_SIGNUP_REPLY_UNTIL= BACKEND_BRIDGE_KEY= \
SUPABASE_URL= SUPABASE_PUBLISHABLE_KEY= \
.venv/bin/python tools/check_synthetic_gloo_signup.py
```

This invokes the actual Gloo API for a fictional Jordan Demo conversation: ordinary name, explicit YES consent, Greeter interest, then Sunday availability and twice-monthly frequency. All delivery uses the mock provider. The explicit overrides protect the module-level application initialized during import as well as the replay's own settings: both use disposable in-memory SQLite, with Mac delivery and scheduling disabled. It does not run `app.db.seed`, reset a live database or open Messages.

The script exits nonzero on a failed signup step. Its ignored `evals/reports/synthetic-gloo-signup.json` distinguishes real model usage from simulated replies and records `real_messages_sent: 0`. Check `passed`, expected routes, consent, saved role/frequency and Gloo usage. Gloo failure must remain a visible failure with no template substitute. This result does not prove real-device delivery or background scheduling.

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
