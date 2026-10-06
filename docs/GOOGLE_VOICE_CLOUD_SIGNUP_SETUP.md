# Default-off cloud signup setup

`GOOGLE_VOICE_SIGNUP_ENABLED` makes the separate continuous signup mode available
to the backend and private connector. It defaults to `false`, including in
`cloud_deploy.py init --demo`; a missing field in an existing environment also
means off. The flag alone never enables the worker. The bounded demo window and
its controls remain separate, and ordinary disconnected setup retains its hold.

For a new explicit cloud candidate, add the string field
`"GOOGLE_VOICE_SIGNUP_ENABLED": "true"` to the existing complete private setup
inputs, then prepare/check a new private file:

```sh
python3 tools/cloud_deploy.py init --demo --inputs-file /private/path/cloud-inputs.json --env-file /private/path/new-cloud.env
python3 tools/cloud_deploy.py check --demo --require-config --env-file /private/path/new-cloud.env
```

The tool accepts only literal `true`/`false` for this field; `true` requires
explicit `--demo`. It creates independent secrets and mode 0600, does not
inherit shell flags, and never overwrites an existing file. Configuration
receipts report availability separately from worker startup or live readiness.
No account, service, signup worker or demo window is started by these commands.

For the existing deployment, the sole runtime operator instead updates only
this field in the existing private environment after source review. Preserve
its secrets, sender identity, Compose project name, private volumes and
sandbox policy; do not regenerate the environment or copy laptop cookies.
Use that same file for Compose interpolation and backend `env_file`, with
shell overrides cleared as in the existing deployment runbook. The reviewed
Compose configuration forwards the identical field to both services.
No infrastructure or runtime settings were changed while preparing this source.

After deployment and dedicated cloud sender verification, an authenticated
superadmin separately enables signup through
`POST /api/cloud-texting/signup/enable` with `{"enabled":true}`. This persists
the scoped operator authority that the worker checks and can resume after a
cloud restart. Disable through the same control with `{"enabled":false}`.
Cold connector startup and typed private connection timeouts on read-only
health/intake probes can enter `waiting_connection`. The saved authorization ID,
sender and complete participant-session scope must remain unchanged. Five
failures at most, with 15/30/60/120/240-second backoff, fit inside one durable
ten-minute window; restarting does not extend it. No composition or delivery
occurs until exact sender/scope readiness and STOP-first intake pass again.
Auth/scope mismatches, unknown UI formats, Gloo failures, operator holds and
uncertain submissions remain held. Historical generic held records are never
automatically reclassified. A temporary HTTP health response proves process
availability, not sender or participant readiness. An ambiguous browser error
still needs operator review; cookie persistence alone cannot prove recovery.
Adding an admin-registered number then follows the scoped signup flow; the
worker is limited to its initial invitation and actual inbound signup replies.
It does not authorize a general outbound scheduler or unknown numbers.
Consent, STOP, Gloo availability, quiet hours, exact review and sender-identity
holds continue to apply. The environment flag is not a grant of provider
permission or competition certification.

Verify the admin-registration invitation, an actual target-specific reply,
STOP handling and cloud restart recovery with the laptop runtime off before
claiming continuous operation or delivery. Synthetic setup checks establish
configuration behavior only; real account, Gloo and delivery results remain
separate runtime evidence owned by the operator.
