# Dedicated-account Google Voice browser bridge: hackathon candidate

This is a tested synthetic implementation, **not an activated Google Voice
integration**. No real account/thread has been inspected, no texts sent, no
cloud VM provisioned, and no laptop-independent deployment verified.

## Boundaries and fastest cloud path

Use one persistent Ubuntu 24.04 VM with a private virtual display, one headed
Chromium/Chrome profile and one browser worker. Run FastAPI in a separate
process on that same VM; the Cloudflare admin remains separate. Preserve the
current Gloo client, Supabase store, SendGate and admin approval logic.

The existing documentation (`TEXTY.md`) describes FastAPI and a temporary
local tunnel as laptop-dependent. Moving only Chrome would leave that
dependency. For a genuine cloud test both **backend and browser worker** must
run remotely, and the admin's backend origin must point to that remote backend.
The current Cloudflare configuration supplies no Browser Run binding. Main
worker owns admin deployment and upstream changes; this branch does not edit it.

Smallest proposed first test: existing approved Linux VM if available, otherwise
DigitalOcean Basic **2 GiB/1 vCPU/50 GiB at $12/month cap, $0.01786/hour**.
A 24-hour synthetic soak is about **$0.43** of compute, excluding taxes,
backups, network overages and any other products. Chrome plus FastAPI may need
more memory; **4 GiB/2 vCPU/80 GiB is $24/month, $0.03571/hour**. Resource fit
is an engineering estimate, not measured on Linux yet. [Official DO prices](https://www.digitalocean.com/pricing/droplets).

Cloudflare Browser Run is an alternate experiment: Free allows 10 browser
minutes/day; Paid includes 10 hours/month then $0.09/hour. One continuously
running browser for 30 days adds roughly $63.90 beyond the included hours,
plus the Workers plan and other usage. [Official pricing](https://developers.cloudflare.com/browser-run/pricing/).
[Human Live View](https://developers.cloudflare.com/browser-run/features/human-in-the-loop/)
can support manual login, but bot-identified sessions may be blocked and
[reusing a running session](https://developers.cloudflare.com/browser-run/features/reuse-sessions/)
does not establish durable profile recovery after platform termination. This
MVP does not implement a CDP/Cloudflare transport or store cookie snapshots.

Availability of an existing DigitalOcean account/approved VM is **unknown**;
no account inventory, login or credential inspection was performed.

No provider, spending limit, new OAuth app, persistent host access, tunnel
credentials or remote session storage has been approved by this work. A precise
proposal for owner approval is: one 2 GiB DO VM, Ubuntu 24.04, maximum $12/month
compute, a 24-hour synthetic cloud test (~$0.43 compute), no add-on backups or
new paid services. Approval must also cover storing the dedicated browser
profile and bridge credential on that host and the approved HTTPS exposure.
Do not purchase or provision from this document alone.

## Implementation and compatibility

- `app/sms/voice_provider.py`: interchangeable provider selected explicitly
  with `SMS_PROVIDER=google_voice`; only SendGate reserves outbound IDs.
- `app/integrations/voice_browser.py`: visible DOM adapter, exact account label,
  receiving E.164, thread URL/ID and recipient checks **before body reads**.
- `voice_journal.py`: SQLite WAL, FULL synchronous durable inbox, seen IDs,
  initial timestamp cutoff, local STOP suppression and outbox evidence.
- `voice_worker.py`: read-only by default; optional ingress and separately
  enabled exact-approved outbound. One click only, stop on auth/DOM/policy
  errors, no automated login, challenges, private APIs or evasion.

For minimal integration `/mac/*`, `MAC<session-id>:` IDs and `mac_messages`
approval tags are retained as **internal compatibility names**, not a claim
of Apple delivery. This lets existing provenance-scoped history, Gloo replies,
STOP, quiet hours, budget, care holds and exact signed-in reviews stay intact.
The new authenticated `/mac/transport` handshake matches provider, real clock,
confirmation mode and exact session map. `X-TextMonkey-Transport: google_voice`
is required for this backend selection; legacy Mac workers are rejected.
Never run both transports on this backend or reuse a prior Mac session ID.

This candidate's `confirmation=true` rule is a **Google Voice test activation
restriction**, not a change to the product's normal routine automation mode.
The active backend/configuration has not been changed. Use a separately staged
test backend if the active product must retain automatic mode. `AUTOMATION_ENABLED`
continues to control the existing backend scheduler; the browser worker never
starts its own agent scheduler. Normal-mode browser outbound is not supported
in this phase: the minimum future change is an explicitly authorized worker
mode that accepts gate-authorized routine claims without an exact review, plus
a backend preflight that rechecks every routine purpose/consent/care/quiet-hour/
budget/session/automation restriction immediately before click. Existing review
proofs must remain mandatory for review-required actions. Do not bypass the
current guard simply to enable automatic replies.
The inherited admin label still says Mac; main worker may rename its display.

Each recipient must have a fresh session ID and a timezone-aware window of at
most two hours, bound in backend and worker config identically. Natural replies
such as `JOIN`, `YES`, `What am I booked for?` are admitted **only in the verified
project-only account and exact bound direct thread**. The trusted worker adds
session provenance. The tester does not type routing markers. Legacy markers
are accepted and stripped if present. Bare STOP remains effective after expiry;
START needs an active window. Personal accounts, mixed-purpose threads,
groups, contact-name resolution and other recipients are unsupported.

## Unresolved live DOM prerequisite

`bridge/google_voice/config.fixture.example.json` is **synthetic only**. Its
selectors are not Google Voice selectors. The live worker refuses it without
`live_dom_reviewed=true`, `project_only_account=true` and
`dedicated_profile_confirmed=true`. These flags represent actual human review,
not shortcuts to make the program run.

A reviewed contract must identify exactly one account label, receiving phone,
direct thread ID, recipient, composer and send control; each rendered bubble
must have a stable unique ID, `in`/`out` direction, one text body and an absolute
timezone-aware ISO timestamp exposed in the DOM. If the actual UI lacks those
fields, this adapter fails closed: implement and fixture-test a safe visible-DOM
normalizer after dedicated-account inspection before attempting ingress. Do not
invent IDs from body text, guess contact names, pull private endpoints, or use
anti-detection patches. No live selector compatibility is claimed.

The browser reads only loaded bubbles. Virtualized older rows below the
persisted bootstrap cutoff are excluded; missing IDs/times stop processing.
This is a bounded event stream, not a complete archive. Opening a real thread
may mark messages read, so even browser-only soak requires the dedicated
account and an approved read session.

## Synthetic local test

From repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r bridge/google_voice/requirements.lock
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests/test_voice_bridge.py
```

These tests use headed Chromium with HTTP/HTTPS blocked and local fabricated
HTML; a FastAPI TestClient checks the full gate/review/claim/browser/receipt
flow. Real Gloo and messaging are replaced with deterministic fixtures.

For a CLI read-only check copy the fixture config outside Git, replace its file
URL with the absolute fixture path, and set writable profile/journal paths:

```sh
.venv/bin/python -m app.integrations.voice_worker --config .voice-bridge.json --fixture --once
```

No token is needed in this default read-only mode. Health is written next to the
journal as `.health.json`: status (`ready`, `login_required`, `blocked`,
`dom_changed`), last successful sync and outbound-enabled flag; no text or
credentials. Read-only creates a seen-ID baseline but never claims, posts
inbound, sends, or calls Gloo. Switching to ingress requires a fresh journal
(the mode is part of the identity), so old observation does not become input.

## VM runbook: execute only after host/access approval

1. Create the approved Ubuntu VM; allow SSH from the owner, no public VNC,
   Chrome debug or worker ports. Create service user `textmonkey`. Put checkout
   in `/opt/textmonkey`, runtime state in `/var/lib/textmonkey` (0700) and
   environment files in `/etc/textmonkey` (root-owned 0600). Keep secrets,
   browser profile, SQLite/WAL and backups out of Git and images.
2. Install Python/venv, Xvfb and x11vnc from the Ubuntu repositories. Create
   `.venv`, install the checked-in version lock, then run Playwright's
   `install --with-deps chromium`. The worker uses its paired official browser;
   ordinary branded Chrome may be selected with `browser_channel=chrome` only
   after the owner installs and verifies it. This is not an evasion mechanism.
3. Start the private display with `voice-display.service`. For login the worker
   must be stopped. Run x11vnc bound to loopback only, connect through an SSH
   tunnel, and have the human log into **the dedicated project account**:

   ```sh
   x11vnc -display :99 -localhost -rfbport 5900 -forever
   DISPLAY=:99 .venv/bin/python -m playwright open --browser chromium --user-data-dir /var/lib/textmonkey/profile https://voice.google.com/
   ```

   On the owner's machine use `ssh -L 5900:127.0.0.1:5900 approved-vm`, then a
   VNC client at localhost:5900. Do not expose VNC publicly, transfer personal
   Chrome profiles, export cookies, print storage state or automate credentials.
   If login is blocked or challenged, stop for the human; no workaround is
   supplied. Close this browser before starting the worker. Disable x11vnc
   after setup.
4. Have the owner designate Noah's **exact** recipient number and thread in the
   project account. Do not infer them from personal history. Review metadata
   and live DOM only in that account, install a fixture-tested DOM contract,
   and set `/var/lib/textmonkey/config.json` to that exact account/line/thread.
   No additional recipients. The JSON holds no passwords, cookies or API keys.
5. Backend environment (values supplied privately by owner, not in commands):
   `SMS_PROVIDER=google_voice`, `MAC_BRIDGE_ENABLED=true`,
   `MAC_MESSAGE_SERVICES=SMS`, exact `MAC_DEMO_PHONES`, fresh `MAC_TEST_SESSIONS`,
   strong `MAC_BRIDGE_TOKEN` and `ADMIN_PASSWORD`,
   `COMPETITION_CONFIRMATION_REQUIRED=true`, `DEMO_MODE=false`,
   `AUTOMATION_ENABLED=false`. Retain the owner's authorized Gloo/Supabase
   credentials and configuration. Worker env uses `VOICE_BRIDGE_TOKEN` with
   that bridge token. Set `PLAYWRIGHT_BROWSERS_PATH=/opt/textmonkey/browsers`
   during browser installation and in the worker environment so systemd
   `ProtectHome` does not hide browser binaries.
   Do not print secrets. Use one FastAPI process for SQLite; for existing
   Postgres, owner verifies transport migrations first. This branch does not
   apply migrations or create roles/OAuth applications.
6. Start separate `backend.service` and read-only `voice-worker.service`.
   Backend binds localhost:8000, worker config backend URL may be
   `http://127.0.0.1:8000`. Keep admin exposure behind an owner-approved permanent
   HTTPS origin/tunnel; coordinate its Cloudflare `BACKEND_URL` and bridge
   key with main worker. A laptop tunnel is not sufficient. Named-tunnel
   credentials, DNS changes or additional infrastructure need explicit approval.
7. Execute the staged plan below. Only after authorization for outbound set
   `live_delivery_approved=true` and add `--enable-ingress --live-delivery`
   to the worker command. Stop the service before changing config. Each new
   account/line/recipient/window/DOM contract requires a fresh journal/session.

The systemd samples use `Restart=no` for the worker: login/session expiry,
DOM drift and backend proof rejection remain paused until human review.
The backend and private display can restart independently. Browser profile and
journal locks prevent competing local workers. Operationally designate one
host; do not clone profiles or run a second remote Voice worker.

## Docker alternative (artifact, Linux build not verified here)

The Dockerfile uses the official `mcr.microsoft.com/playwright/python:v1.58.0-noble`
image with the same pinned Playwright version. No ports are published; run as
UID 10001 with a private durable state mount. Example commands after approval:

```sh
docker build -f bridge/google_voice/Dockerfile -t textmonkey-voice:fixture .
docker run --rm --init --shm-size=1g --mount type=bind,src=/approved/state,dst=/state textmonkey-voice:fixture
```

Default command performs observation only. To run synthetic tests in the
container override the command with `python -m pytest tests/test_voice_bridge.py`.
Manual login needs an owner-approved private display access path; the VM
runbook is the simpler first test. The local Docker daemon was unavailable, so
image build, Linux dependency compatibility and VM resource use are open checks.

## Staged test plan and success evidence

1. **Synthetic cloud run:** build/start on the approved remote host; run the
   headed fixture suite and backend integration test. Restart worker and VM,
   preserve state, prove one profile/journal lock, inspect only synthetic health.
2. **Dedicated-account browser-only soak:** after human login and DOM review,
   run without ingress/outbound for at least 30 minutes; ideally 24 hours.
   Validate account/line/thread and stable IDs/timestamps. Simulate logout/DOM
   drift in fixtures, and confirm worker stays paused. Opening threads can mark
   read. No backend queues, Gloo calls or outbound at this stage.
3. **Natural inbound-only:** use a fresh session/journal, `--enable-ingress`,
   outbound disabled. First poll baselines existing history. Only **new** Noah
   test replies in the approved bound thread enter normal Gloo history. Verify
   one durable receipt/proposal, no old message replay and no other account.
4. **One exact-approved reply:** signed-in admin reviews exact phone/body/hash;
   enable separately authorized low-volume outbound. Verify one click, one new
   matching outbound bubble and backend `submitted`; tester confirms receipt.
   Browser evidence means observed submission, **not carrier delivery**.
5. **Recovery:** fixture-test lost inbound response, lost claim response (server
   leaves it dispatching), lost ACK, crash after click, duplicate identical
   bubbles, expired session/proof and STOP arriving during prepare. No unknown
   retry, no claims during observation, no blind repair of uncertain history.
6. **Laptop-off proof:** disconnect local backend/tunnels and close the laptop.
   An approved tester sends one new natural input; remote worker/backend/Gloo,
   signed-in cloud admin approval and final tester receipt must work. Record
   cloud health times and scoped audit IDs. Until this passes, do not claim
   laptop independence or live Google Voice readiness.

Outbox state transitions: `queued -> sending -> observed_sent | unknown`;
pre-attempt policy failure is `failed`. Restart turns `sending` into `unknown`.
Unknown reconciliation compares new stable outbound IDs against the persisted
pre-click set and exact body; one match records evidence, zero/multiple matches
remain unknown. No worker transition sends an unknown operation again; unresolved operations
hold later output to the same recipient too. Assume
no concurrent manual outbound composer use during the test; otherwise matching
body alone cannot establish which actor sent it. Unknown ACK maps to backend
`uncertain`; if later evidence appears, keep it locally and require a human
backend reconciliation because ACKs are terminal. An unsaved/lost claim response
also requires operator review; never reset server rows to queued to recover it.
