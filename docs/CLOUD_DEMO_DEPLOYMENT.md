# One-server cloud demo deployment

Prepared October 5, 2026. Reuse the existing Cloudflare account and connected
development frontend. Put the existing Compose backend, private connector and
Cloudflare Tunnel on **one persistent Linux VM**. No laptop process is required
for that architecture. This runbook has not provisioned a host, imported a
session, contacted Gloo, changed a deployed site or sent a text.

## Hosting choice and current access

Prefer the existing [prepared Oracle Always Free definition](CLOUD_VM_PROVISIONING.md):
one Ubuntu 24.04 ARM64 VM, **1 OCPU / 6 GB RAM / 50 GB boot disk**, using the
existing Terraform and its reviewed free-tier guards. The same Cloudflare
frontend and Compose backend retain their functionality; choosing this host
does not remove features.

Current OCI Always Free allowances include 1,500 A1 OCPU-hours and 9,000 GB-hours
monthly, equivalent to **2 OCPUs / 12 GB**, and **200 GB combined boot/block
storage** in the home region. Existing resources count toward those limits.
Available free capacity is not guaranteed, and qualifying idle instances can
be reclaimed after a seven-day review period. The full Gloo/Supabase operating
cost and sustained free uptime have not been established.
[Official allowances and conditions](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

Cloudflare authentication already works for the existing personal account.
No Oracle CLI/config was found locally; that does not establish whether Jacob
already has an account. Missing account facts are authorized OCI access,
tenancy/compartment, credential profile, home region/availability domain,
eligible platform image, SSH public key/admin IP and a fresh account-wide
free-capacity/storage inventory. Follow the existing definition's review and
plan requirements. Do not upgrade to pay-as-you-go, accept terms, select paid
capacity or provision anything as part of this preparation.

DigitalOcean Basic Regular, 2 GiB / 1 vCPU / 50 GiB, is an **unapproved optional
$12/month fallback**, not the selected host. No paid plan has been activated.
[Official optional fallback price](https://www.digitalocean.com/pricing/droplets).

## Deployment handoff

1. After actual OCI access, inventory and free capacity are verified, follow
   the [existing Oracle review/plan/apply handoff](CLOUD_VM_PROVISIONING.md).
   Its fixed VM, network restrictions and cloud-init already prepare Docker
   Engine/Compose; do not duplicate or redesign them. Keep backend, connector
   and browser-debugging ports closed publicly. If free capacity is unavailable
   or eligibility is unclear, hold provisioning without a paid fallback.
2. Place the reviewed source from `jacobthebaer-lab/text-monkey` at
   `/opt/text-monkey-cloud/repo`. Use the reviewed feature revision, then the
   integrated `codex/complete-text-monkey` revision when merged. Do not copy a
   Mac database, Messages credentials or an existing browser profile. Prepare
   `/etc/text-monkey` as a private directory outside Git.
3. Choose the dedicated backend hostname in an existing authorized Cloudflare
   DNS zone. Create a named tunnel for this VM and route only that hostname to
   it. Privately adapt `deploy/cloud/tunnel.example.yml`: its origin stays
   `http://backend:8000`, with the catch-all 404. Place the config and tunnel
   credential JSON in `/etc/text-monkey/tunnel.yml` and
   `/etc/text-monkey/tunnel.json`. Restrict their ownership/permissions while
   allowing the tunnel container's user to read both read-only mounts. Do not
   publish those credentials or expose the connector.
4. Privately create `/etc/text-monkey/cloud-inputs.json`, mode `0600`, containing
   actual string values for `VOICE_EXPECTED_EMAIL`, `VOICE_EXPECTED_NUMBER`,
   `ADMIN_EMAIL_ALLOWLIST`, `SUPERADMIN_EMAIL_ALLOWLIST`, `SUPABASE_URL`,
   `SUPABASE_PUBLISHABLE_KEY`, `ADMIN_SITE_URL`, `BACKEND_URL` and `GLOO_API_KEY`.
   Set `CLOUDFLARE_TUNNEL_CONFIG` and `CLOUDFLARE_TUNNEL_CREDENTIALS` to the
   absolute paths above. The superadmin must also be an admin; the Supabase key
   must be publishable/legacy anon. `ADMIN_SITE_URL` is the existing connected
   dev origin and `BACKEND_URL` is the new dedicated HTTPS origin. Optional
   `GLOO_ENDPOINT` and `CHURCH_TIMEZONE` retain their documented defaults.

From the reviewed repository root on that VM:

```sh
sudo python3 tools/cloud_deploy.py init --demo --inputs-file /etc/text-monkey/cloud-inputs.json --env-file /etc/text-monkey/google-demo.env
sudo python3 tools/cloud_deploy.py check --demo --require-config --env-file /etc/text-monkey/google-demo.env
```

`init --demo` requires complete explicit private inputs, generates three
different secrets and exclusively creates the `0600` environment file. It
does not inherit shell settings, overwrite an existing file, import a session,
start services or start a demo conversation window. `check` reports field names
and readiness limitations without printing secrets. Without `--demo`, both
commands retain the default disconnected setup and production provider hold.

Start with empty participant/session scope. The generated demo flags preserve
`AUTOMATION_ENABLED=false`, exact review, disabled profile/Planning Center
sync and the existing consent, eligibility, schedule, quiet-hour, Gloo,
no-em-dash and deduplication gates. Register actual consenting participants in
the authenticated admin screen later. Flags alone prove neither consent nor
provider permission.

Use the same private file for Compose interpolation and backend configuration.
This helper clears inherited environment overrides and gives the runtime a
distinct project name and fresh volumes:

```sh
tm_cloud() {
  sudo env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CLOUD_ENV_FILE=/etc/text-monkey/google-demo.env \
    docker compose --project-name text-monkey-google-demo-20261005 \
    --env-file /etc/text-monkey/google-demo.env \
    -f /opt/text-monkey-cloud/repo/deploy/cloud/compose.yaml "$@"
}
tm_cloud config --quiet
tm_cloud build
tm_cloud --profile tunnel up -d
tm_cloud ps
```

Run exactly one backend worker and one connector replica. The project creates
private `backend-data`, `backend-logs` and `voice-data` volumes, preserving
SQLite, audit state and the browser profile across container restarts. The
backend remains loopback-only; the connector has no public port. Preserve these
volumes and private backups across updates. Never use `down --volumes` or
delete the VM/disk as a restart procedure.

## Reuse the existing Cloudflare dev site

The recorded connected frontend is the Worker configured in
`web/texty/wrangler.jsonc` (`texty-volunteer-demo`) at
`https://texty-volunteer-demo.jacobthebaer.workers.dev`. Its current backend
binding is unverified. Verify this is the intended dev origin and retain
its current private backend bindings for rollback before retargeting it. The
separate `text-monkey-demo.pages.dev` site is a static synthetic preview; its
current build has no live backend proxy. Reusing Cloudflare hosting does not
by itself connect that Pages preview.

Existing private authentication configuration can supply the Supabase origin,
publishable key and admin allowlist. A nonempty superadmin allowlist has not
been established; configure the actual authorized administrator privately,
rather than treating a synthetic role as proof of admin access. Existing tunnel
records point to the laptop Planning Center receiver and must not be repurposed
as proof of a cloud backend.

After the deployment is authorized, publish the reviewed frontend to the
confirmed dev Worker and enter `BACKEND_URL` and the generated
`BACKEND_BRIDGE_KEY` through Wrangler's private secret prompts, using that
same Worker configuration. Do not put secrets in public assets or arguments.
Verify Supabase confirmation/recovery redirects for the exact dev origin and
verify `/api/config` through the Worker reaches this cloud backend. The
existing Mac runtime remains separate; preserve its configuration for rollback.

## Acceptance and truthful readiness

Sign in as the intended verified superadmin. Use only the authorized dedicated
sender, verify the actual account email and number, and keep personal-phone
forwarding off. Follow the [bounded demo controls](GOOGLE_VOICE_BOUNDED_DEMO.md)
for participant registration, session setup, exact review and an explicit
bounded conversation window. A new VM or successful `check` is not permission
to import a Google session or send texts. Normal/production Google Voice
automation remains held; this candidate does not certify provider or
competition compliance.

Before claiming the laptop can stay off: verify the cloud host and tunnel,
intended admin authentication, real Gloo availability, an explicitly authorized
round trip to a consenting participant and actual receipt on that device while
the laptop runtime is off. Then restart the cloud containers and verify durable
state, no duplicate submission and the requirement to start a fresh window.
An open admin tab is unnecessary during an active cloud window, but the window
expires or stops on restart; this is not indefinite unattended production
texting. A provider UI acknowledgement proves submission only. Actual session
lifetime, delivery and cloud operation remain unverified until these checks.
