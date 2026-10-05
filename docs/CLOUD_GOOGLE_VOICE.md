# Cloud simulation and default Google Voice hold

October 5 source candidate: a separate, default-off [bounded demo mode](GOOGLE_VOICE_BOUNDED_DEMO.md)
uses individually requested superadmin steps. The normal/production hold and all
historical disconnected proofs below remain unchanged. This candidate does not
change Google's policy or establish competition certification or delivery readiness.

This isolated build provides a disconnected cloud simulation, private backend/container preparation and superadmin controls. **Automated Google Voice is permanently held** under the competition-compliance requirement and [Google Voice's Acceptable Use Policy](https://support.google.com/voice/answer/9230450?hl=en), which prohibits scripted messages. ID approval permits manual Google Voice use only; it cannot enable session import, polling or automated texting. The existing Mac transport remains intact.

## Architecture

The simulation uses a fictional church sender and explicit synthetic fixtures. Jacob's future production plan is registered Twilio with an individual sender number per church. That integration must isolate each church's sender/conversations and requires separate registration, provisioning and live acceptance testing. No paid messaging service is activated by this build.

```text
Coordinator → separate Cloudflare Worker → private Python backend on cloud VM
                                            ├─ durable SQLite + existing rules
                                            ├─ Gloo configuration / synthetic test fixtures
                                            └─ Google connector process health only
                                               └─ permanent provider policy hold
```

The backend can run without an open admin page. Verified Supabase admin identity is required for the portal. Only emails present in both `ADMIN_EMAIL_ALLOWLIST` and `SUPERADMIN_EMAIL_ALLOWLIST` receive cloud controls; the server checks every setup request. Superadmin permission cannot override the Google policy hold.

The historical Playwright adapter remains available to offline tests. Normal connector startup does not launch Chromium or contact Google. Synthetic tests may launch Chromium on `about:blank` to verify the packaged runtime, without a Google session or external network. See [feasibility and policy decision](GOOGLE_VOICE_CLOUD_FEASIBILITY.md).

## Review without a live connection

Run this from the isolated feature worktree:

```sh
python3 tools/texty_local_demo.py --port 58127
```

Open `http://127.0.0.1:58127/cloud-preview`. This loopback-only preview requires no account, credentials, Google setup, cloud host or live link. It reuses the cloud controls with in-memory fixtures. Switch between sample superadmin and coordinator roles, review and approve a fictional draft into a held queue, pause/resume the sample queue, or simulate a Gloo outage. The page always reports disconnected and never claims a real send or delivery. Reload or **Reset preview** clears the sample state.

The preview accepts no Google cookies, exposes no real APIs, and blocks browser network requests through `connect-src 'none'`. Its scripted sample copy is only a UI fixture, not a Gloo fallback. Follow **Open sample admin dashboard** for the existing fictional roster/calendar demo. Nothing in this preview provisions or enables the transport.

## Manual Google Voice setup is separate

The user may finish the intended account's mobile/ID verification directly in Google for manual use. A selected signup number does not prove account activation. Keep personal-phone forwarding off, using [Google's linked-number instructions](https://support.google.com/voice/answer/165221?co=GENIE.Platform%3DDesktop&hl=en) if needed. No Google account, sender number, cookie export or completed ID check is required for this cloud simulation. Do not import that account into the blocked automation adapter.

## Check cloud execution with the existing GitHub account

The `Cloud Voice container proof` workflow builds the actual backend and connector images on a GitHub-hosted Linux AMD64 runner. It exercises backend HTTP health and authorization, launches Chromium on `about:blank`, and verifies the real production connector stays in provider policy hold even with enable flags. The unverified-session adapter check uses a separate offline fixture. Both runtime containers have networking disabled, no credentials, and no published ports. The job is limited to 15 minutes; check the account's remaining included Actions minutes before running it.

This is a bounded cloud runtime test, not an always-on deployment. It requires no new hosting account, but does not test Google login, SMS, Gloo, carrier delivery, or operation after the runner shuts down. Run locally against freshly built deployment images with `bash tools/cloud_voice_container_proof.sh` or use the isolated branch's workflow.

The expanded proof runs the connector and backend twice against fresh private Docker volumes, which are removed afterward. A controlled interruption after the connector's durable reservation must recover as uncertain without preparation or a click. Its intake baseline must survive. The backend's exact approval/composition receipt, pause state and interrupted dispatch claim must survive another process/container; scripted Gloo failure must create no additional review. The browser page and Gloo responses are explicit synthetic fixtures, with runtime networking disabled. Hard browser/OS crash recovery and real API behavior remain unverified. Google session recovery is outside the allowed scope while the permanent policy hold applies.

See the [free hosting decision](CLOUD_FREE_HOSTING.md) before provisioning a continuous host. Existing GitHub is sufficient for these bounded proofs; the current persistent-browser implementation does not fit Cloudflare's free daily browser allowance.

## Prepare the isolated cloud server

The candidate is an Oracle Always Free Linux VM with enough memory for Chromium, running Docker Engine and Compose v2. Debian's Chromium package supports ARM64 and AMD64. This repository does not provision an account, accept paid services, or guarantee capacity. Confirm the selected VM, storage and network fit the current [Always Free limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm). Oracle can reclaim idle resources. Gloo and Supabase allowances must be checked separately; the complete service has not been proven to cost $0 under sustained use.

Use the isolated feature branch and a fresh database volume. From the repository root, prepare a private disconnected environment with the standard-library setup tool:

```sh
python3 tools/cloud_deploy.py init
python3 tools/cloud_deploy.py check
```

`init` exclusively creates `deploy/cloud/.env` with mode `0600`, three independently generated secrets, false delivery/automation/sync flags, mandatory exact review and empty recipient/session scope. It never overwrites an existing file, imports shell values, reads Mac credentials, or copies a database. No account, phone number, Google session or completed ID check is needed to create this scaffold. `check` validates the file and performs bounded, read-only Docker Engine, Compose and Compose-schema checks. Use `--skip-docker` for file validation alone. Output contains field names and actionable status, never configured secrets or Docker diagnostic output.

Provide chosen values by editing the private file, or pass a private JSON object with `init --inputs-file /private/path/cloud-inputs.json`. Only documented setup fields are accepted: `ADMIN_EMAIL_ALLOWLIST`, `SUPERADMIN_EMAIL_ALLOWLIST`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `ADMIN_SITE_URL`, `BACKEND_URL`, `GLOO_API_KEY`, `GLOO_ENDPOINT`, `CHURCH_TIMEZONE`, optional `CLOUD_BACKEND_PORT`, and optional named-tunnel config/credential paths. Reserved sender email/number fields are validated if supplied, but are not needed for the disconnected scaffold and cannot enable Google automation. Inputs cannot override generated secrets, enable delivery, or populate test recipients. Never commit the input file.

The preflight requires public HTTPS origins for Supabase, the admin site and backend; it rejects embedded URL credentials and localhost origins. It accepts only publishable or legacy anon Supabase keys, checks that superadmins are also admins, requires separate secrets and keeps exact review enabled. Gloo credentials and authentication configuration may remain blank while preparing the scaffold. `--require-config` makes missing infrastructure configuration an error. Even complete configuration always reports `provider_policy_hold: true`, `live_ready: false` and `runtime_verified: false`. Google Voice automation stays blocked regardless of ID verification, cookies or environment flags. This tool does not test APIs, verify credentials, connect Google, enable another provider, publish a site or start services.

After reviewing the private configuration, a separate operator can build and start the disconnected containers:

```sh
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml config --quiet
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml build
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml up -d
```

The connector can report authenticated process health while disabled without launching a browser or contacting Google. Process health is independent of missing Google ID, sender number, Gloo key or Supabase settings; application readiness still reports its missing configuration and provider hold.

The compose file exposes the backend only on `127.0.0.1:8000`. The connector has no host port, browser debugging port or public route. Its shared token travels only across the private Docker network. Both services run as non-root users. Database, conversation audit logs and browser profile have separate persistent Docker volumes. Preserve and back up those volumes privately; they contain credentials and conversation data. Do not use `down --volumes` when updating the deployment.

Run **one** backend worker and **one** connector replica against these volumes. Multiple schedulers or browser processes are unsupported. Container health means the process responds; it does not mean Google Voice is connected or that delivery works.

## Connect a separate Cloudflare experiment

Use `deploy/cloud/wrangler.jsonc`, whose Worker name is `text-monkey-cloud-experiment`. It does not replace the current connected Worker or static preview. The [prepared VM definition](CLOUD_VM_PROVISIONING.md) supplies a possible private host, subject to a reviewed plan and real account readiness.

The optional Compose `tunnel` profile uses the pinned Cloudflare image with **HTTP/2 over IPv4**. It requires an existing named tunnel, a dedicated HTTPS hostname and private files specified by `CLOUDFLARE_TUNNEL_CONFIG` and `CLOUDFLARE_TUNNEL_CREDENTIALS`. Copy `deploy/cloud/tunnel.example.yml` privately and replace its nonworking UUID/hostname placeholders. Its only origin is `http://backend:8000` on the private Compose network, followed by a catch-all 404. The files mount read-only, and missing files do not become directories. Keep backend and connector ports closed publicly.

The Terraform network permits TCP 7844 only to Cloudflare's verified global tunnel endpoints. Preserve `--protocol http2 --edge-ip-version 4`, and do not select a separate regional endpoint set without reviewing its network rules. [Cloudflare firewall requirements](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)

The private `.env` may live outside the repository when selected through `CLOUD_ENV_FILE`. The same private file must be passed to Compose with `--env-file`. After a separate deployment review, the optional profile is selected with `--profile tunnel`; it is not started by preparing configuration or running a dry run. No named tunnel or external connection was created by this build.

The Worker forwards authenticated requests to that dedicated HTTPS backend and injects its bridge key server-side. Run its deployment dry run before separately authorized publication:

```sh
cd web/texty
npm ci
npx wrangler deploy --dry-run --config ../../deploy/cloud/wrangler.jsonc
```

Configure `BACKEND_URL` and `BACKEND_BRIDGE_KEY` as private Worker secrets only when deploying the reviewed experiment. Never put those values into committed configuration or the browser. Infrastructure publication does not enable Google automation.

## Cloud controls and future delivery acceptance

Cloud status reports the provider policy hold. Session import, resume, inbound polling and outgoing Google sends remain blocked regardless of ID verification, credentials or environment switches. Do not export Google cookies or attempt a live Google send/reply validation.

The disconnected preview can exercise sample review, pause, recovery and Gloo-outage behavior without accounts or delivery. Infrastructure validation must demonstrate authorization, durable state, restart recovery and continued holds before it is considered ready for a future compliant transport.

A separately authorized registered provider must preserve Gloo-only interpretation/composition, zero em dashes, church-specific sender isolation, consent, eligibility, quiet hours, scheduling, exact text review and duplicate prevention. Named recipient approval and real device/carrier evidence are still required for any future delivery test. A queue acknowledgement or synthetic response does not prove delivery.

## Verification available in the repository

```sh
cd web/texty
npm test
cd ../../cloud/voice
npm test
```

Current build comparison on October 3, 2026, after merging integration revision `06b89eb`:

- Cloud/preview and current admin-status focused tests: **121 passed**; frontend: **62 passed**; connector: **16 passed**. The earlier cloud/preview-only selection had **94 passes**.
- Full Python comparison is **not green**. Integration baseline `06b89eb` has **145 failures, 1,060 passes and 1 expected failure**. This cloud build has **the exact same 145 failed test IDs**, **1,152 passes and 1 expected failure**. No newly failing test IDs were found. The older comparison against `a681bb8` had the same 210 inherited failure IDs in both builds; newer integration tests corrected some expectations.
- The integration merge preserves accurate source-bound admin notifications and review outcomes, cloud role reset, and the legacy native `MAC` queue receipt rule. No provider acknowledgement is promoted to device delivery.
- The actual Compose stack started healthy using generated private setup with blank account/API fields. Public config reported the provider hold; private connector authorization and rejected mutation checks passed. Only disposable proof containers/volumes were removed afterward. The separate Cloudflare Worker deployment dry run passed.
- Actual ARM64 deployment images rebuilt successfully. The expanded network-disabled container proof passed, including the production Google policy lock and offline backend/connector restart checks. This is synthetic/runtime evidence, not Google or carrier delivery.

Final merged cloud proof passed on a GitHub-hosted Linux AMD64 runner: [run 37156725948](https://github.com/jacobthebaer-lab/text-monkey/actions/runs/37156725948), executable revision `9c6527047f18892c6a3fe0db7dee242327980ab9`, completed October 3, 2026. Both actual images built; backend HTTP configuration reported the permanent provider hold; anonymous controls rejected requests; production Node startup stayed held despite enable flags with no browser/account/ledger access. The offline Chromium and both durable restart fixtures passed. Runtime containers had no outbound networking, live credentials or real sends. The earlier standalone policy-lock run [37156393909](https://github.com/jacobthebaer-lab/text-monkey/actions/runs/37156393909) also passed at `9dffe35db55ce84f417583f5b79a19ac9918d8e1`.

Earlier local checkpoints, preserved as historical evidence from October 3, 2026:

- Full Python suite: **1,109 passed, 1 expected failure**, including integration changes through `b4b0b69`.
- Admin frontend: **56 passed**; connector: **14 passed**.
- Both **Linux ARM64 Docker images built**. The backend started with disabled transport, no credentials and no network; its 55 focused backend/integration/guard tests passed inside the image. Chromium also started and closed successfully with no network and the deployment's read-only filesystem, non-root user and restricted capabilities.
- The separate Cloudflare Worker passed its deployment dry run; Compose configuration validated. The superadmin panel was visually inspected with synthetic data.
- The disconnected preview was checked in Chrome: sample review enters a held queue, resume stays disconnected, coordinator mode hides connection controls, and reset clears samples. No browser errors were reported.

Validated on a GitHub-hosted Linux AMD64 runner on October 3, 2026: [cloud container proof](https://github.com/jacobthebaer-lab/text-monkey/actions/runs/37153158437), executable revision `d20601a9e683be45e41caba665b8ffca39e59431`. Both deployment images built, backend HTTP checks passed, and Chromium launched with authenticated connector health reporting no verified session. The runtime containers had no outbound network, credentials, published ports or sends. The first run exposed Chromium's need for writable configuration/cache paths; the deployment image now puts them under the private `/data` volume, and the corrected cloud run passed.

Expanded restart proof also passed on a GitHub-hosted Linux AMD64 runner on October 3, 2026: [backend and connector recovery](https://github.com/jacobthebaer-lab/text-monkey/actions/runs/37154166870), executable revision `ff9a9375caa9fb91ba640d8329e8024cb003f12e`. Separate containers preserved the connector's uncertain reservation and intake baseline without a retry/click, and preserved the backend's exact approval/composition receipt, pause state and interrupted dispatch claim without replay. The scripted Gloo outage created no extra review. These are offline fixtures and a controlled connector interruption, not a real Gloo API check or hard browser/OS crash. Temporary proof volumes were removed. Locally, the added backend recovery proof and existing 14 connector tests passed too.

These are historical synthetic/runtime receipts, not permission to activate the Google adapter. Continuous hosting, live Gloo behavior, a future compliant provider round trip, carrier delivery and sustained free-tier operation remain unverified. Google automation remains on permanent policy hold. No Google credentials were imported, no real texts were sent, and no persistent cloud deployment was activated by this build.
