# Prepared Oracle cloud VM definition

Prepared **October 3, 2026** in `deploy/cloud/terraform/`. No account, plan against OCI, VM or paid resource has been created. This definition prepares a generic cloud backend/simulation host.

**Google automation remains on policy hold.** The competition-compliance requirement and [Google Voice's Acceptable Use Policy](https://support.google.com/voice/answer/9230450?hl=en) prohibit the scripted-texting path. **ID approval only permits manual Google Voice; it does not authorize automated cloud texts.** Provisioning this host must not import a Google session, enable automated Voice sends, or turn the existing Mac deployment off. A registered provider is a separate future integration.

## What this definition creates

- Exactly one `VM.Standard.A1.Flex` Ubuntu 24.04 ARM VM, fixed at **1 OCPU / 6 GB RAM**, with one **50 GB balanced boot disk**. No alternate shape, replicas, Marketplace image, paid capacity fallback, load balancer, NAT gateway or database service is requested.
- One isolated VCN, subnet, Internet gateway, route table and custom security list. The subnet uses only that custom list. The VCN's default permissive list is not attached.
- SSH key access from one explicit administrator public IPv4 `/32`. Password/root SSH login is disabled. Backend, connector, Docker and browser-debugging ports have no ingress rule.
- Outbound TCP 443 for HTTPS dependencies; UDP/TCP 53 and UDP 123 only to OCI's `169.254.169.254` DNS/NTP endpoint. Cloudflare Tunnel gets TCP 7844 to the 20 exact documented global IPv4 endpoints. Run the tunnel with **`--protocol http2 --edge-ip-version 4`**, without `--region us`; QUIC/UDP 7844 and the separate US endpoint set are intentionally absent. [Cloudflare endpoint contract](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/), [OCI DNS](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/dns.htm), [OCI NTP](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/configuringntpservice.htm).

The ephemeral public IPv4 enables the narrow SSH management route. It does not publish the application. The optional Compose `tunnel` profile implements the TCP/IPv4 contract above. Its private config and credential files, dedicated hostname and named tunnel must be supplied separately after the host is reviewed; its example has nonworking placeholders and a catch-all 404.

## Inputs required from the actual account

The template deliberately has **no account defaults and no default-zero usage totals**. Its example is invalid until these values are provided privately:

1. Tenancy OCID, existing compartment OCID, explicit private OCI credential-profile name, home region and exact availability-domain name.
2. An Oracle platform **Canonical Ubuntu 24.04 ARM image OCID**, verified in the console as eligible for the intended Always Free VM. The plan reads its OS/version, boot-size requirement and A1 compatibility and rejects Marketplace listings. Human review must also establish that it is an official platform image; labels alone are not proof.
3. One SSH **public** key and the administrator's exact public IPv4 `/32`. OCI API signing credentials stay in the local `~/.oci/config` profile and private key file; neither belongs in Terraform variables, Git, this document or chat.
4. A fresh inventory across **all tenancy compartments**, including stopped A1 instances and unattached/retained boot/block disks. Supply totals for other A1 OCPUs, other A1 memory, other boot/block GB and other VCNs. Exclude only resources already managed by this same Terraform state, which the definition adds once. Unknown inventory means stop.

The guard adds this VM's allocation to the supplied totals and rejects amounts above **2 OCPUs, 12 GB RAM, 200 GB combined boot/block storage and 2 VCNs**. It checks the selected region against OCI's actual home-region subscription, checks the AD, and requires a review timestamp within 30 minutes of planning. [Current Always Free limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

These are arithmetic and identity guards, **not an automatic tenancy inventory or billing guarantee**. Inspect all compartments, current monthly usage, existing services, image/licensing eligibility, available free capacity and the final OCI cost estimate. Oracle can reject capacity requests and reclaim idle instances; do not bypass those limits with another shape, region or paid upgrade. Gloo and other service costs are separate. [Hosting decision](CLOUD_FREE_HOSTING.md)

## Review, plan and later apply

Use a private workstation with Terraform 1.5 or newer, below 2.0, and Oracle provider **9.3.0**, pinned by this definition. The [official OCI setup instructions](https://docs.oracle.com/en-us/iaas/Content/dev/terraform/tutorials/tf-provider.htm) describe the API-key profile. Apply credentials must be authorized for this exact tenancy and compartment.

From the repository root, these preparation commands do not apply resources:

```sh
cd deploy/cloud/terraform
umask 077
cp terraform.tfvars.example terraform.tfvars
# Fill the private file using the verified inputs above.
terraform init -backend=false
terraform fmt -check
terraform validate
terraform plan -input=false -out=review.tfplan
terraform show -no-color review.tfplan
```

`init` downloads the pinned provider. `plan` makes authenticated, read-only OCI queries and saves account identifiers and the proposed configuration privately. Review the provider lockfile generated by `init`; keep credentials, `.terraform/`, `.tfvars`, plans and state out of Git. The directory's `.gitignore` covers those private outputs. Store state and its backups privately so later runs manage the same VM rather than creating another.

Before applying, review every proposed resource and the fresh account budget. The plan must contain one A1 instance with the fixed size, a 50 GB balanced boot disk, the isolated network above, no replacements/deletions and no unrelated resources. Replan after any account change or stale inventory. **Do not use `-target`, disable guards, pass `-refresh=false`, run `-auto-approve`, or repeatedly apply on capacity failure.**

Only after the specific plan is approved, run `terraform apply` interactively and review its newly refreshed plan before entering `yes`. Do not apply the saved `review.tfplan` later without refreshing the account review: saved plans preserve the earlier state of the world. No apply is part of this build.

The instance has `prevent_destroy` and preserves its boot disk. Do not remove those protections to force a replacement. A retained disk still consumes the storage allowance; include it in the next review. Back up private application data before any explicitly approved migration or removal.

## Host bootstrap and application handoff

Cloud-init checks for Ubuntu 24.04 ARM64, installs Docker Engine and Compose from [Docker's official signed apt repository](https://docs.docker.com/engine/install/ubuntu/), and prepares `/opt/text-monkey-cloud`. It uses the official Ubuntu ARM mirror over HTTPS and holds if another active HTTP repository remains. It configures OCI's private NTP service. No application checkout, credentials, browser session or message is created. Docker use remains via `sudo`; the Docker socket is never exposed.

After an approved deployment, connect as `ubuntu` from the allowed address and verify:

```sh
sudo cloud-init status --wait
sudo systemctl is-active docker
sudo docker compose version
uname -m
```

Expected architecture is `aarch64`. Treat cloud-init failure as a held setup; inspect its local log and repair the specific prerequisite without opening arbitrary ports. The host is only infrastructure-ready. Continue with the prepared cloud deployment/readiness bundle, using simulation or a disabled transport. Keep secrets and private state out of images and Terraform user data. The tunnel must follow the TCP/IPv4 contract above, and the backend must remain bound to loopback.

## Validation performed for this build

All Terraform and example-variable files were parsed as HCL; cloud-init was parsed as YAML and its embedded bootstrap script passed `bash -n`. Core instance fields were checked against the [pinned official OCI provider source](https://github.com/oracle/terraform-provider-oci/tree/v9.3.0).

The [official HashiCorp Terraform Docker image](https://hub.docker.com/r/hashicorp/terraform), version **1.13.5**, completed `init -backend=false` with the signed **OCI 9.3.0** provider, `fmt`, and provider-backed `validate`. The generated provider checksum lockfile is included. Validation ran with container networking disabled and no OCI credential files mounted. **No OCI plan/API query, actual bootstrap or provisioning was performed.** A future operator must complete the authenticated review/plan above before any apply.
