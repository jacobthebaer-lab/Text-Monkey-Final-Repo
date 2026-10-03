# Google Voice cloud feasibility and current decision

Research and policy update: **October 3, 2026**. The original research led to an isolated experimental browser connector. The later competition-compliance requirement supersedes the proposed automated Google Voice activation path. See [current cloud build and evidence](CLOUD_GOOGLE_VOICE.md).

## Current decision

Google's [Voice Acceptable Use Policy](https://support.google.com/voice/answer/9230450?hl=en) prohibits messages sent through automated processes such as scripts. The application therefore keeps automated Google Voice on **permanent policy hold**, including session import, polling and sending. Completed ID verification, stored cookies, an enabled environment flag or superadmin approval does not remove that hold.

**ID approval permits manual Google Voice use only.** It is not a prerequisite for the disconnected cloud simulation or authorization for an automated send/reply test. Technical browser automation does not establish a compliant product transport.

Jacob's future production plan is a registered Twilio sender for each church. Provider registration, paid resources, provisioning and live delivery require separate authorization and verification. The current Mac-connected deployment remains intact.

## Historical technical research

The experiment began on `codex/google-voice-cloud-research`, initially based on `7939651`, then integrated `edc40f2` and `e974a38` before later implementation updates. The original checkout's uncommitted work was preserved.

The initial investigation found that [mautrix/gvoice](https://github.com/mautrix/gvoice) implemented account lookup, inbox reads, long polling and sends. Its [setup documentation](https://docs.mau.fi/bridges/go/setup.html?bridge=gvoice) described headless Electron for personal-account sending; its [authentication documentation](https://docs.mau.fi/bridges/go/gvoice/authentication.html) used a Google session rather than a supported Voice OAuth integration. These were technical observations, not Google authorization or proof that our account could send from the cloud. That bridge was not adopted into Text Monkey.

Text Monkey instead built an independent Playwright Chromium adapter and tested it with synthetic fixtures. Linux ARM64 and GitHub-hosted AMD64 container startup and controlled durable-state recovery were verified. No Google session, real Google DOM, live Gloo round trip or carrier delivery was verified. The permanent policy hold now prevents activating that adapter for Google automation; the synthetic fixtures remain useful for reviewing scheduling, approvals, fail-closed behavior and transport-independent recovery.

The signup UI requested mobile and government-ID verification. Selecting a number during signup did not establish activation. For manual Google Voice use, [Google's linked-number instructions](https://support.google.com/voice/answer/165221?co=GENIE.Platform%3DDesktop&hl=en) describe disabling forwarding and removing a linked number after setup. Manual signup does not require enabling the cloud connector.

## Cloud hosting conclusion

Cloud operation without the Mac is possible for the backend/simulation. The current Cloudflare Worker forwards to a separate backend; publishing static Pages assets does not host that backend. Confirmed Supabase admins and a separate server-enforced superadmin allowlist already exist in the experiment. Keeping the admin page open must not be required for cloud runtime execution.

[Cloudflare Browser Run](https://developers.cloudflare.com/browser-run/limits/) includes only 10 free browser minutes/day. [Cloudflare Containers](https://developers.cloudflare.com/containers/platform/pricing/) requires the paid Workers plan. Neither establishes a continuously running, free deployment of the current two-container design.

The existing GitHub account supports bounded cloud container proofs. Oracle Always Free is a candidate for ongoing generic hosting, with account verification, home-region capacity and idle-instance reclamation constraints. Current limits and the distinction between runtime tests and 24/7 operation are recorded in [CLOUD_FREE_HOSTING.md](CLOUD_FREE_HOSTING.md). The [prepared VM definition](CLOUD_VM_PROVISIONING.md) is not an executed deployment. Gloo and database allowances remain separate unverified costs.

## Evidence still needed

1. Review the permanent Google policy hold and synthetic recovery behavior on the isolated branch.
2. If hosting is authorized, provision the reviewed cloud simulation infrastructure and verify durable state, authentication, restart recovery and resource usage. Google account approval is not required for this step.
3. For a separately approved compliant provider, complete registration, church-specific sender isolation, Gloo integration and exact review/consent/scheduling checks before any live delivery.
4. Prove actual send/reply and carrier delivery separately, with the local runtime and Mac off, before calling that future transport fully cloud-ready.

No historical synthetic result or successful manual sign-in is evidence of working automated Google Voice delivery. Do not restore the superseded Google activation instructions.
