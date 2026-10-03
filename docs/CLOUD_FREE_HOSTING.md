# Free cloud hosting decision

Verified against official documentation on **October 3, 2026**. This is hosting research, not a provisioning or delivery receipt. The experiment runs a Python backend and a persistent Playwright Chromium connector; the Cloudflare admin site alone does not run those processes.

**Recommendation:** use the existing GitHub account for bounded cloud runtime checks. Finish the pending Google Voice identity verification, then prove that Google accepts the intended account in a cloud browser before opening another hosting account. For ongoing operation of the current containers, Oracle Always Free is the strongest verified $0 infrastructure candidate, subject to signup, capacity and reclamation constraints. There is no verified, uninterrupted, entirely free deployment yet.

| Existing option | Verified allowance and constraint | Fit for this experiment |
| --- | --- | --- |
| GitHub Actions | GitHub Free includes 2,000 minutes/month; a hosted job ends after at most 6 hours. The repository's proof job is capped at 15 minutes. [Official limits](https://docs.github.com/en/actions/reference/limits) | Run the actual images on a cloud Linux host without a new account. The present workflow disables container networking and uses no account credentials, so it proves runtime only. It is not a persistent server or Google/Gloo test. |
| GitHub Codespaces | Personal Free includes 120 core-hours/month, equivalent to 60 hours on a 2-core machine, plus 15 GB-month storage. [Billing](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces) A codespace stops after 30 idle minutes by default and has a 12-hour maximum lifetime. [Lifecycle](https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle) | Possible interactive cloud debugging using the existing account; unnecessary for the current automated proof. Processes stop with the codespace. This is not 24/7 hosting. |
| Cloudflare Browser Run | Free: 10 browser minutes/day, 3 concurrent browsers, one new browser every 20 seconds. Sessions normally close after 60 idle seconds; extending that timeout still consumes browser time. [Limits](https://developers.cloudflare.com/browser-run/limits/) | Useful for a short compatibility probe, with connector adaptation. The current continuously running browser cannot fit this allowance. |
| Cloudflare Containers | Requires the $5/month Workers Paid plan; no Containers allocation on Workers Free. Included resources on Paid do not remove the subscription cost. [Pricing](https://developers.cloudflare.com/containers/platform/pricing/) | Can host containers, but fails the strict $0 requirement. Do not provision it for this experiment. |

Cloudflare supports Playwright and cookie-based authentication. That capability does not establish Google Voice login compatibility, durable Google sessions, or successful texting. Its browser traffic originates from identifiable Cloudflare infrastructure. [Browser Run FAQ](https://developers.cloudflare.com/browser-run/faq/)

## Oracle Always Free: ongoing-host candidate

The current **Always Free tenancy** allowance is **2 ARM OCPUs and 12 GB RAM total**, represented by 1,500 OCPU-hours and 9,000 GB-hours/month. It includes **200 GB combined boot/block storage** and **10 TB/month outbound transfer**. Free compute and block storage must be in the home region. These are the current limits, not the older 4-OCPU/24-GB figures. A small Ubuntu ARM VM is a plausible fit for both containers; production load still needs measurement. [Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

Oracle may lack free capacity in the selected home region. It may reclaim an instance when, over seven days, CPU 95th-percentile utilization, network utilization, and A1 memory utilization are each below 20%. A lightly used texting demo could meet those conditions; uptime is not guaranteed. [Capacity and reclamation rules](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

Signup requires contact details and a valid credit/debit card for identity verification. Oracle can place temporary authorization holds, permits only one free account per person, and provides no Free Tier SLA. Its $300/30-day trial is separate from Always Free. Accounts idle for 30 days may be suspended or terminated. Stay within resources marked Always Free rather than depending on trial credit. [Official signup FAQ](https://www.oracle.com/cloud/free/faq/)

## Acceptance boundary

After Google approval, the next useful evidence is a bounded cloud login/identity check, then an explicitly scoped send/reply test through Gloo with the Mac off. A later Oracle deployment must demonstrate restart recovery, persistent private state, Google reconnection holds, and usage within its free allocation. Keep existing live transport unchanged throughout this experiment.

Gloo usage allowance, Supabase usage and sustained Google Voice compatibility are **not verified as free** by this hosting research. Successful container execution does not prove those costs, authenticated integration, carrier delivery or reliable 24/7 service. No account, VM, paid plan, Google session or message was created by this research.
