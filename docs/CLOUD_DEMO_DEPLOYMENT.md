# One-server cloud deployment

Prepared October 5, 2026. Keep the existing Cloudflare development frontend and
run the same Compose backend/private connector on one persistent Oracle Always
Free VM. The optional HTTPS overlay supplies a public origin without buying a
domain or using a tunnel. This preparation has created no cloud resources,
Google sessions, real Gloo requests, public certificates or texts.

## Host and free-tier boundaries

Use the existing [Oracle definition and review guards](CLOUD_VM_PROVISIONING.md):
Ubuntu 24.04 ARM64, **1 OCPU / 6 GB RAM / 50 GB boot disk**, in the account's
Phoenix home region. Check account-wide compute and boot/block inventory,
actual free capacity, image eligibility and SSH access before provisioning.
Reuse an existing VM when present; do not apply the prepared definition to
duplicate a manually created server.

Current Always Free A1 allowances are 1,500 OCPU-hours / 9,000 GB-hours monthly,
equivalent to 2 OCPUs / 12 GB, plus 200 GB combined boot/block storage in the home
region. Use only Always Free resources, never expiring trial credits, paid
capacity or a pay-as-you-go upgrade. Capacity is not guaranteed and qualifying
idle instances may be reclaimed. Full Gloo/Supabase operating cost and permanent
free uptime remain unproven. The same backend retains its features on this host.
[Official allowances](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

Prefer one retained **reserved public IPv4** after the operator verifies the
actual tenancy quota and $0 account estimate. Oracle's official guidance says
public IPs, including unassigned reserved IPs, incur no charge, and current docs
confirm a reservation survives the assigned VM's lifetime. No reservation was
added by this build. Preserve the reservation when replacing the VM. Before
releasing any address, disconnect the Worker's old backend origin so credentials
cannot be sent to a later owner of that address.
[Oracle cost guidance](https://blogs.oracle.com/developers/setting-up-a-virtual-cloud-network-vcn-in-oracle-cloud-infrastructure),
[IP lifecycle](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/managingpublicIPs.htm).

DigitalOcean's $12/month VM remains an unapproved optional paid fallback.

## HTTPS origin without an owned domain

The existing Cloudflare account has no DNS zones. Use the VM's actual public
IPv4 to form `https://text-monkey.<IPv4-with-dashes>.sslip.io`, replacing the
placeholder with the complete address. The free DNS service resolves the
embedded IP automatically, without another hosting account. Its operator
supports Caddy/Let's Encrypt HTTP-01 certificates for these individual
hostnames. Public DNS availability and shared certificate rate limits remain
dependencies; this is not guaranteed uptime. [Operator documentation](https://nip.io/).

Use that exact HTTPS origin as `BACKEND_URL`. Direct IP origins are rejected
by setup because the unchanged Cloudflare Worker cannot fetch them.
[Cloudflare limitation](https://developers.cloudflare.com/workers/platform/known-issues/#fetch-to-ip-addresses).

The optional `deploy/cloud/compose.https.yaml` adds Caddy and persistent
certificate/config volumes. It removes the backend's host port and publishes
only TCP 80/443; the connector remains private. Only `/api/*` reaches
`backend:8000`; other application paths return 404. Caddy preserves Authorization
and X-Texty-Bridge, and its Docker-network address requires the backend bridge
check rather than the localhost shortcut. Caddy's admin API is disabled.

`Dockerfile.caddy` uses the published official 2.11.6 Alpine multiarch image by
immutable digest, replacing its binary with the official **2.11.7** release and
architecture-specific SHA512 verification. ARM64 and AMD64 are supported. The
2.11.7 official image was not yet published when checked; its HTTP/2 POST proxy
fix is relevant to this API. [Official release/checksum assets](https://github.com/caddyserver/caddy/releases/tag/v2.11.7).

## Deployment handoff

1. Follow the existing Oracle account/inventory review and provisioning handoff.
   Its cloud-init prepares Docker Engine and Compose. For this optional HTTPS
   route, additionally permit public inbound **TCP 80 and 443** in OCI and the
   host firewall. Port 80 handles ACME challenges and HTTPS redirects. Keep SSH
   restricted to the administrator IP and keep ports 8000, 8765 and browser
   debugging private. Existing outbound HTTPS/DNS is sufficient; no tunnel or
   additional managed service is needed. The existing Terraform network does
   not include this HTTPS ingress delta yet.
2. Place the reviewed `jacobthebaer-lab/text-monkey` source revision at
   `/opt/text-monkey-cloud/repo`. Keep the Mac runtime and its private state
   separate. Prepare `/etc/text-monkey` privately outside Git.
3. Create `/etc/text-monkey/cloud-inputs.json`, mode 0600, with actual string
   values for `ADMIN_EMAIL_ALLOWLIST`, `SUPERADMIN_EMAIL_ALLOWLIST`,
   `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `ADMIN_SITE_URL`, `BACKEND_URL`
   and `GLOO_API_KEY`. Superadmins must also be admins, the Supabase key must be
   publishable/legacy anon, and `ADMIN_SITE_URL` must be the intended connected
   development origin. Do not configure/import Google sessions or enable live
   transport while the production policy hold applies.

From the reviewed repository root on the VM, prepare the disconnected runtime:

```sh
sudo python3 tools/cloud_deploy.py init --inputs-file /etc/text-monkey/cloud-inputs.json --env-file /etc/text-monkey/cloud.env
sudo python3 tools/cloud_deploy.py check --require-config --env-file /etc/text-monkey/cloud.env
```

These commands generate independent secrets and exclusively create/check a
0600 environment file. They do not inherit shell settings, overwrite existing
files, verify API credentials or start services. Normal disconnected flags,
empty participants/sessions, exact review and all messaging guards remain.

After the infrastructure deployment is authorized, use the same private file
for Compose interpolation and backend configuration. This helper clears shell
overrides and adds HTTPS only. On first deployment, choose a distinct project.
On updates, keep the exact existing project name and retained volumes:

```sh
tm_cloud() {
  sudo env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CLOUD_ENV_FILE=/etc/text-monkey/cloud.env \
    docker compose --project-name text-monkey-cloud-20261005 \
    --env-file /etc/text-monkey/cloud.env \
    -f /opt/text-monkey-cloud/repo/deploy/cloud/compose.yaml \
    -f /opt/text-monkey-cloud/repo/deploy/cloud/compose.https.yaml "$@"
}
tm_cloud config --quiet
tm_cloud build
tm_cloud up -d
tm_cloud ps
```

Use a current Compose version supporting the documented `!reset` merge tag.
Run one backend worker and one connector replica. Preserve private backend data,
logs, connector state and HTTPS data/config volumes across restarts and updates.
Never use `down --volumes` as a restart procedure. The original disconnected
Compose and optional named tunnel remain available without the HTTPS overlay.

## Separately authorized bounded demo

The default commands above retain disconnected operation. The separately
reviewed [bounded demo controls](GOOGLE_VOICE_BOUNDED_DEMO.md) add explicit
participant registration, exact review and a time-limited conversation window.
They do not establish provider permission, competition compliance or delivery.

For an explicitly authorized new demo environment, add actual private
`VOICE_EXPECTED_EMAIL` and `VOICE_EXPECTED_NUMBER` to the setup inputs and use
`tools/cloud_deploy.py init --demo` and `check --demo --require-config`. The tool
requires complete inputs, exclusively creates the private environment file and
never overwrites the existing runtime configuration. Begin with empty
participants and sessions; retain broad automation, profile/Planning Center
sync and the existing consent, eligibility, schedule, quiet-hour, Gloo,
no-em-dash and deduplication controls. Do not create a second runtime or replace
existing volumes when updating the authorized existing deployment.

Use the [private manual browser login](CLOUD_BROWSER_LOGIN.md) for direct human
sign-in on the cloud host. Stop the connector before opening its existing
profile. Keep it stopped until the helper confirms shutdown and clears its
marker. Sign-in alone does not enable messaging. Explicit profile verification
must match the actual dedicated account and sender, invalidate old freshness
and preserve personal-phone forwarding off before any separately authorized
demo action.

A provider UI acknowledgement establishes submission only. Verify actual
receipt on the consenting device before claiming delivery. Established-thread,
inbound and post-send DOM behavior require their own real-account evidence.
An active conversation window expires or stops on restart and must be started
again explicitly; it is not indefinite unattended production texting.

## Existing frontend and verification

Use the connected `text-monkey-demo.pages.dev` frontend and its reviewed
[Pages build route](CLOUDFLARE_DEMO.md). The earlier `texty-volunteer-demo` Worker
is historical; do not retarget it as part of this deployment. Preserve the
current Pages release and private backend bindings for rollback.

Enter the selected HTTPS `BACKEND_URL` and generated `BACKEND_BRIDGE_KEY` through
private secret prompts for the intended Pages project. Verify Supabase Site URL
and allowed redirects for that exact frontend origin. Configuration verification
is separate from actual confirmation and password-recovery round trips. Check
the public certificate and `/api/config` through Pages, authenticated access,
direct origin rejection without the bridge, 404 for unrelated routes, durable
state and cloud restart recovery. Test with the laptop runtime off before
claiming independent hosting; process health does not establish delivery.

Production Google Voice automation remains held; no infrastructure check,
synthetic proof or session import establishes live readiness or actual delivery.
