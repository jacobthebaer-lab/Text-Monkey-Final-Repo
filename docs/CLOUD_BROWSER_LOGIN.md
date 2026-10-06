# Private manual cloud sign-in

This optional desktop lets the human operator sign into Google Voice directly
on the cloud VM. It uses the connector's existing Chromium build, UID 1000 and
private `voice-data` volume at `/data/profile`. No laptop cookies are extracted,
exported or imported. Sign-in alone does not authorize automated Google texts,
enable transport, scan conversations or verify delivery.

Before opening this dedicated profile, both browser entry points atomically set
Chromium's ordinary **Continue where you left off** preference
(`session.restore_on_startup=1`), preserving unrelated preferences. This is needed
for session cookies to survive a normal browser exit; the temporary
`--restore-last-session` switch alone does not keep the preference at exit.
[Chromium's session-restoration implementation](https://github.com/chromium/chromium/blob/main/chrome/browser/profiles/profile_impl.cc)
and [session cleanup](https://github.com/chromium/chromium/blob/main/chrome/browser/sessions/session_data_deleter.cc)
distinguish that behavior. An active or stale browser lock, symlink or malformed
preferences file holds startup; nothing clears authentication or repairs locks
automatically. Previous tabs may reopen within this same dedicated profile.

Both entry points also set the standard desktop browser-profile sign-in opt-out
(`signin.allowed_on_next_startup=false`) in this dedicated profile. This is the
preference used by `BrowserSignin=0`; it disables browser account reconciliation
and sync, while ordinary Google website login remains available. Chromium 154
can otherwise reconcile website cookies against absent or unusable browser
OAuth tokens and issue a Google website logout at startup. It does not export
credentials, bypass Google login, weaken storage or remove automation indicators.
See the tagged [policy handler](https://github.com/chromium/chromium/blob/154.0.8037.92/chrome/browser/policy/browser_signin_policy_handler.cc)
and [account consistency manager](https://github.com/chromium/chromium/blob/154.0.8037.92/chrome/browser/signin/account_consistency_mode_manager.cc).

A network-disabled ARM64 synthetic check proves no-expiry cookie retention across
normal headed-browser close and connector startup, alongside persistent cookies.
It does not establish Google session validity. The runtime owner must separately
verify the actual sender after login and a real restart before allowing texts.
Google can still expire or revoke a session and require human reconnection.

The `manual-login` Compose profile is off by default and never starts with the
normal deployment. It adds Xvfb, x11vnc and [noVNC/websockify](https://github.com/novnc/noVNC)
to the existing connector image. Its only published port is VM loopback
`127.0.0.1:6080`; the VNC server binds container loopback. The desktop has a
temporary random password, no clipboard sharing and no browser debugging port.
It runs outside the application network as a non-root user with a read-only
image, all capabilities dropped and no new privileges. Chromium's own sandbox
is mandatory; startup fails if the host cannot support it. This is temporary
administrative access to a private credential profile.

Use the same reviewed source revision, private environment file and **exact
existing Compose project name** as the deployed connector. Only the designated
runtime operator runs these steps. The helper refuses an active connector or a
missing existing profile volume. It does not stop the connector automatically,
change private configuration or enable any messaging flag.

1. Build the optional image, then stop the connector using the deployment's
   existing Compose helper: `tm_cloud stop google-voice`. Keep it stopped through
   the entire manual login. Do not use `down --volumes`.

   ```sh
   sudo python3 tools/cloud_browser_login.py build --env-file /etc/text-monkey/cloud.env --project-name text-monkey-cloud-20261005
   sudo python3 tools/cloud_browser_login.py start --env-file /etc/text-monkey/cloud.env --project-name text-monkey-cloud-20261005
   sudo python3 tools/cloud_browser_login.py password --env-file /etc/text-monkey/cloud.env --project-name text-monkey-cloud-20261005
   ```

2. On the administrator's computer, use the existing approved SSH key, VM
   address and host-key verification. Bind the SSH forwarding listener to local
   loopback only; no OCI or host firewall rule is added for port 6080 or VNC.

   ```sh
   ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:6080:127.0.0.1:6080 -i /private/path/approved-key ubuntu@VM_PUBLIC_IP
   ```

   Open `http://127.0.0.1:6080/vnc.html`, connect, and enter the temporary password
   in the password field, never in a URL. The remote browser starts at
   `about:blank`. Navigate to `https://voice.google.com` manually and confirm the
   intended dedicated account. Complete only Google's normal sign-in and
   verification steps. Stop if Google denies access or requires a step the human
   cannot complete; do not bypass CAPTCHA, account safeguards or access controls.
   Keep personal-phone forwarding off. Do not send texts during login setup.

3. Close the browser normally, then stop the temporary service. Only after the
   helper confirms the browser is stopped does it clear the durable
   `/data/manual-login.active` marker. A crash leaves this marker in place so the
   connector holds instead of sharing a profile. Explicit `stop` performs the
   same checked cleanup after a crash. Never delete Chromium profile lock files
   to force concurrent access.

   ```sh
   sudo python3 tools/cloud_browser_login.py stop --env-file /etc/text-monkey/cloud.env --project-name text-monkey-cloud-20261005
   sudo python3 tools/cloud_browser_login.py resume --env-file /etc/text-monkey/cloud.env --project-name text-monkey-cloud-20261005
   ```

   Close the SSH forwarding session. In the existing superadmin UI, explicitly
   select **Verify cloud sign-in**. The dedicated-account identity check reuses
   the cloud profile without cookie import; it pauses outgoing work, stops the
   bounded demo window and invalidates prior freshness. Intake and any permitted
   reviewed action remain separately explicit. Resume preserves all existing
   flags and is not transport approval.

Do not expose this UI through Caddy, Cloudflare, a public VNC port or a public
debugging route. Retain the private volume across normal updates. No manual
sign-in, Google access or live runtime mutation was performed while preparing
this source. Actual account compatibility remains unverified; cloud Chromium
can encounter Google's normal sign-in restrictions.

## Chromium sandbox prerequisite

Both the normal connector and this optional desktop require
`deploy/cloud/chromium-seccomp.json`. Keep that file with the reviewed Compose
files; do not remove its `security_opt` or substitute `seccomp=unconfined`.
The connector explicitly enables Playwright's Chromium sandbox, whose default
is otherwise off. There is no unsandboxed fallback.

The policy retains the [official Moby Docker default](https://github.com/moby/profiles/blob/6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef/seccomp/default.json),
commit `6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef`, with five additional rules:
user-namespace `clone`/`unshare`, exact `clone(CLONE_NEWPID | SIGCHLD)`, and
`setns`/`chroot` for Chromium's sandbox namespaces. The unmodified baseline SHA256
is `6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6`;
its Apache 2.0 license is retained in `LICENSE.chromium-seccomp`.
The kernel still enforces namespace permissions; no host capabilities are
granted. Default syscall denials remain. [Playwright's container guidance](https://playwright.dev/docs/docker)
describes the non-root user-namespace approach.

Before restarting the credential browser, the runtime owner must prove this
policy on the actual Linux host using an empty temporary profile, networking
disabled and no published ports. Verify successful rendering, renderer
user/PID/network namespaces distinct from the browser, zero effective
capabilities, no-new-privileges and both Docker and Chromium seccomp filters.
Keep the host's AppArmor and user-namespace restrictions enabled. If they reject
the probe, stop and review the denial; do not disable host protections, add
`SYS_ADMIN`, use privileged containers or bypass the sandbox. A successful
synthetic probe establishes isolation compatibility, not absolute security or
Google sign-in compatibility. Restart/login remains a separate operator action.
