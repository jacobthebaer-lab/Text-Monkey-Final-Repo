# Private manual cloud sign-in

This optional desktop lets the human operator sign into Google Voice directly
on the cloud VM. It uses the connector's existing Chromium build, UID 1000 and
private `voice-data` volume at `/data/profile`. No laptop cookies are extracted,
exported or imported. Sign-in alone does not authorize automated Google texts,
enable transport, scan conversations or verify delivery.

The `manual-login` Compose profile is off by default and never starts with the
normal deployment. It adds Xvfb, x11vnc and [noVNC/websockify](https://github.com/novnc/noVNC)
to the existing connector image. Its only published port is VM loopback
`127.0.0.1:6080`; the VNC server binds container loopback. The desktop has a
temporary random password, no clipboard sharing and no browser debugging port.
It runs outside the application network as a non-root user with a read-only
image, all capabilities dropped and no new privileges. Chromium's process
sandbox is disabled for this Docker setup; the container restrictions still
apply. This is temporary administrative access to a private credential profile.

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
