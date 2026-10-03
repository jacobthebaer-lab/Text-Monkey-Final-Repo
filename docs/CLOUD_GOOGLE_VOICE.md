# Cloud Google Voice experiment

This isolated build adds a cloud transport to Text Monkey and a disconnected preview that needs no live account or link. The current deliverable is the disconnected build. Google Voice signup, cloud provisioning and real delivery testing are deferred; the later setup sections apply only when activating a live connection. Existing Mac transport remains selectable.

## Architecture

The demo treats the configured Google Voice number as the church's SMS sender. Forwarding to the coordinator's personal phone is disabled, and incoming texts remain in the Google Voice account for the connector. Jacob's production plan is registered Twilio with an individual sender number per church. Future production routing must bind each church to its own sender and conversation scope; the Google session controls in this experiment are demo setup only.

```text
Coordinator → separate Cloudflare Worker → Python backend on the cloud VM
                                            ├─ durable SQLite + existing rules
                                            ├─ Gloo interpretation/composition
                                            └─ private Google Voice connector
                                               └─ persistent headless Chromium
```

The backend runs its existing scheduler and the Google Voice transport loop without an open admin page. A verified Supabase admin identity is required for the portal. Only an email present in **both** `ADMIN_EMAIL_ALLOWLIST` and `SUPERADMIN_EMAIL_ALLOWLIST` receives connection controls. The server checks this role on every cloud setup request; hiding controls is not the access boundary.

The connector drives Google Voice's web app using its own Chromium session. It is an experimental browser adapter, not an official Google API or the upstream mautrix/Electron bridge. Google page changes or expired sessions can hold processing. See [feasibility research](GOOGLE_VOICE_CLOUD_FEASIBILITY.md) for the evidence and limitations behind the hosting choice.

## Review without a live connection

Run this from the isolated feature worktree:

```sh
python3 tools/texty_local_demo.py --port 58127
```

Open `http://127.0.0.1:58127/cloud-preview`. This loopback-only preview requires no account, credentials, Google setup, cloud host or live link. It reuses the cloud controls with in-memory fixtures. Switch between sample superadmin and coordinator roles, review and approve a fictional draft into a held queue, pause/resume the sample queue, or simulate a Gloo outage. The page always reports disconnected and never claims a real send or delivery. Reload or **Reset preview** clears the sample state.

The preview accepts no Google cookies, exposes no real APIs, and blocks browser network requests through `connect-src 'none'`. Its scripted sample copy is only a UI fixture, not a Gloo fallback. Follow **Open sample admin dashboard** for the existing fictional roster/calendar demo. Nothing in this preview provisions or enables the transport.

## Finish Google Voice setup

Finish signup in the intended Google account, including the mobile and identity verification Google currently requests. A number selected during signup is not a fully claimed number. Use the final claimed number in configuration.

Mobile verification does not require permanent forwarding. In Google Voice settings, turn off forwarding to the personal mobile and remove the linked number after setup if desired. [Google's linked-number instructions](https://support.google.com/voice/answer/165221?co=GENIE.Platform%3DDesktop&hl=en) explain both operations. The cloud connector uses the Voice account directly.

## Check cloud execution with the existing GitHub account

The `Cloud Voice container proof` workflow builds the actual backend and connector images on a GitHub-hosted Linux AMD64 runner. It exercises backend HTTP health and authorization, launches Chromium on `about:blank`, and verifies the authenticated connector reports an unverified Google session. Both runtime containers have networking disabled, no credentials, and no published ports. The job is limited to 15 minutes; check the account's remaining included Actions minutes before running it.

This is a bounded cloud runtime test, not an always-on deployment. It requires no new hosting account, but does not test Google login, SMS, Gloo, carrier delivery, or operation after the runner shuts down. Run locally against freshly built deployment images with `bash tools/cloud_voice_container_proof.sh` or use the isolated branch's workflow.

The expanded proof runs the connector and backend twice against fresh private Docker volumes, which are removed afterward. A controlled interruption after the connector's durable reservation must recover as uncertain without preparation or a click. Its intake baseline must survive. The backend's exact approval/composition receipt, pause state and interrupted dispatch claim must survive another process/container; scripted Gloo failure must create no additional review. The browser page and Gloo responses are explicit synthetic fixtures, with runtime networking disabled. Hard browser/OS crash recovery, real API behavior and live Google session recovery remain separate checks.

See the [free hosting decision](CLOUD_FREE_HOSTING.md) before provisioning a continuous host. Existing GitHub is sufficient for these bounded proofs; the current persistent-browser implementation does not fit Cloudflare's free daily browser allowance.

## Prepare the isolated cloud server

The candidate is an Oracle Always Free Linux VM with enough memory for Chromium, running Docker Engine and Compose v2. Debian's Chromium package supports ARM64 and AMD64. This repository does not provision an account, accept paid services, or guarantee capacity. Confirm the selected VM, storage and network fit the current [Always Free limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm). Oracle can reclaim idle resources. Gloo and Supabase allowances must be checked separately; the complete service has not been proven to cost $0 under sustained use.

Use the isolated feature branch. Do not point this deployment at the existing connected database or replace the existing Cloudflare preview. From the repository root:

```sh
cp deploy/cloud/.env.example deploy/cloud/.env
chmod 600 deploy/cloud/.env
```

Fill the file privately on the VM:

- Generate three different long random values for `VOICE_API_TOKEN`, `BACKEND_BRIDGE_KEY` and `ADMIN_PASSWORD`. For example, run `openssl rand -hex 32` separately for each. Never paste these values into a chat or a committed file.
- Set `VOICE_EXPECTED_EMAIL` and `VOICE_EXPECTED_NUMBER` to the account and final E.164 Voice number. The connector refuses another account or number.
- Configure the existing Supabase URL, publishable key, verified admin email allowlist and separate superadmin allowlist. No service-role database key is required for sign-in.
- Set `ADMIN_SITE_URL` to the new experiment site once chosen and allow that redirect URL in Supabase Auth. Set the private Gloo key. Missing Gloo holds replies.
- Leave `AUTOMATION_ENABLED`, `LIVE_SMS` and `GOOGLE_VOICE_ENABLED` false during initial setup. Leave test recipients and sessions empty until the exact test participants are authorized.

Validate without printing resolved secrets, then build and start:

```sh
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml config --quiet
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml build
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml up -d
```

The compose file exposes the backend only on `127.0.0.1:8000`. The connector has no host port, browser debugging port or public route. Its shared token travels only across the private Docker network. Both services run as non-root users. Database, conversation audit logs and browser profile have separate persistent Docker volumes. Preserve and back up those volumes privately; they contain credentials and conversation data. Do not use `down --volumes` when updating the deployment.

Run **one** backend worker and **one** connector replica against these volumes. Multiple schedulers or browser processes are unsupported. Container health means the process responds; it does not mean Google Voice is connected or that delivery works.

## Connect a separate Cloudflare experiment

Use `deploy/cloud/wrangler.jsonc`. Its Worker name is `text-monkey-cloud-experiment`; it does not replace `texty-volunteer-demo` or the static Pages preview. The VM is required; static Pages does not host Python or Chromium.

Expose the loopback backend through a named Cloudflare Tunnel managed on the VM, with a dedicated HTTPS hostname routing to `http://127.0.0.1:8000`. Keep the VM firewall closed for port 8000 and do not create a public route to the connector. Follow [Cloudflare's tunnel setup](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/) for the account's supported method. A persistent tunnel, hostname and tunnel credential are deployment prerequisites; a temporary quick tunnel is not a stable runtime.

From the repository root after installing the existing frontend dependencies, set Worker secrets interactively:

```sh
cd web/texty
npm ci
npx wrangler deploy --dry-run --config ../../deploy/cloud/wrangler.jsonc
npx wrangler secret put BACKEND_URL --config ../../deploy/cloud/wrangler.jsonc
npx wrangler secret put BACKEND_BRIDGE_KEY --config ../../deploy/cloud/wrangler.jsonc
```

Use the dedicated backend HTTPS origin for `BACKEND_URL` and the same backend bridge key as the VM. Publishing is a separate deployment step using `npx wrangler deploy --config ../../deploy/cloud/wrangler.jsonc` after reviewing the dry run and confirming this is the experiment Worker. An actual deployment is not performed by building this branch. The proxy preserves each signed-in administrator's bearer and injects the private bridge key server-side.

## Connect the Google session

Sign in to the experiment admin portal as a superadmin. Open Settings → Cloud texting. The panel reports disabled, paused, session-connected or reconnect-required states and queue counts. Normal admins cannot import sessions or pause the transport.

In an isolated browser profile signed into only the intended Google account, finish Google Voice setup. Export the Google session cookies as a Playwright-compatible JSON array using a trusted local method. Required fields include `name`, `value`, `domain` and `path`; `path` must be `/`. Only Google account/Voice domains are accepted. The backend also requires `SID`, `HSID`, `SSID`, `APISID` and `SAPISID`. Do not export other sites, reuse an everyday multi-account browser profile, or install an unknown cookie exporter.

Paste the array into the superadmin session field and select **Connect Google session**. The field clears after every attempt and is never saved to browser local/session storage. The server transfers it to the private connector, which stores the session in its persistent volume and checks the account and Voice number. Reconnection leaves outgoing texts paused. Pausing outgoing texts does not disable incoming STOP handling or review creation. Passwords, ID images and verification codes do not belong in this form.

The connector establishes an initial baseline instead of replying to old history. It reads thread bodies only for explicitly allowlisted test recipients. Blank test scope means no conversation bodies and no sends. Read-only status checks, successful cookie import and a running process are not evidence of working outbound delivery.

## Bounded delivery validation

This build initially requires exact review and bounded recipient sessions. It is not enabled for unbounded production messaging.

1. Choose the exact consenting test recipient and confirm the intended Google Voice account is ready. Add only that recipient to `GOOGLE_VOICE_DEMO_PHONES`. Compose passes the same scope to the connector as `GOOGLE_VOICE_ALLOWED_PHONES`.
2. Add `GOOGLE_VOICE_TEST_SESSIONS` as JSON mapping that E.164 phone to `{ "id": "<32 hex characters>", "starts_at": "<UTC timestamp>", "expires_at": "<UTC timestamp>" }`. Each window must be at most two hours. Create fresh private identifiers and times for the approved session; do not commit the filled scope.
3. Enable `GOOGLE_VOICE_ENABLED=true`. A reviewed delivery test additionally needs `LIVE_SMS=true`. The Google Voice transport loop runs independently of `AUTOMATION_ENABLED` in non-demo mode, so leave scheduled automation off for a manual test. Enable `AUTOMATION_ENABLED=true` only when separately testing scheduled work. Recreate the containers after environment changes. Keep the existing exact-review requirement enabled.
4. Recheck cloud status, then explicitly resume outgoing texts in the superadmin panel. Verify Gloo, consent, quiet hours, eligibility and review requirements before approving a specific outgoing message.
5. Prefix test replies with `[TEXTY <sessionid>]` using that recipient's configured 32-character session ID. This identifies inbound test scope; unmarked messages are ignored except STOP, which remains effective. Observe the recipient's phone and its reply separately. `queued` is a reservation; `submitted` means Google Voice accepted a browser submission. Neither proves carrier delivery. `uncertain` is held and must not be automatically retried.
6. Repeat the round trip with the Mac and local backend/connector off. Restart the cloud services and verify durable idempotency, inbound deduplication and reconnection holds. Test that normal admins receive 403 for setup mutations.
7. Pause outgoing texts and restore `LIVE_SMS=false` after the bounded test. Record only sanitized results for review. Review a PR to `codex/complete-text-monkey` after required checks; do not merge or replace the current deployment based solely on synthetic tests.

## Verification available in the repository

```sh
cd web/texty
npm test
cd ../../cloud/voice
npm test
```

Validated locally on October 3, 2026:

- Full Python suite: **1,109 passed, 1 expected failure**, including integration changes through `b4b0b69`.
- Admin frontend: **56 passed**; connector: **14 passed**.
- Both **Linux ARM64 Docker images built**. The backend started with disabled transport, no credentials and no network; its 55 focused backend/integration/guard tests passed inside the image. Chromium also started and closed successfully with no network and the deployment's read-only filesystem, non-root user and restricted capabilities.
- The separate Cloudflare Worker passed its deployment dry run; Compose configuration validated. The superadmin panel was visually inspected with synthetic data.
- The disconnected preview was checked in Chrome: sample review enters a held queue, resume stays disconnected, coordinator mode hides connection controls, and reset clears samples. No browser errors were reported.

Validated on a GitHub-hosted Linux AMD64 runner on October 3, 2026: [cloud container proof](https://github.com/jacobthebaer-lab/text-monkey/actions/runs/37153158437), executable revision `d20601a9e683be45e41caba665b8ffca39e59431`. Both deployment images built, backend HTTP checks passed, and Chromium launched with authenticated connector health reporting no verified session. The runtime containers had no outbound network, credentials, published ports or sends. The first run exposed Chromium's need for writable configuration/cache paths; the deployment image now puts them under the private `/data` volume, and the corrected cloud run passed.

These checks cover the implementation and actual cloud container startup. Continuous hosting, Google session compatibility, real Google DOM, inbound/outbound round trip, carrier delivery and sustained free-tier operation still require live evidence. Nothing here claims those live checks passed. No Google credentials were imported, no real texts were sent, and no persistent cloud deployment was activated by this build.
