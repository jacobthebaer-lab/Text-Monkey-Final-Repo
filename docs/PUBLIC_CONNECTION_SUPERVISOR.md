# Public connection recovery supervisor

`tools/public_connection_supervisor.py` supervises the existing Pages-to-Mac
connection. It does not run application jobs, restart the backend or Messages,
change consent or texting settings, send messages, create domains or provision
services. Preparing this tool does not start it or authorize deployment.

The default invocation validates configuration and the approved upload offline.
Live execution requires `--run --approved-config-sha256 DIGEST`, with the exact
digest returned by validation. The operator must review the source, private
configuration, account/project/branch and upload provenance before enabling it.
Do not invoke live mode from a synthetic test result alone.

## Boundaries and recovery sequence

The private configuration fixes the existing `text-monkey-demo` Pages project,
production branch `demo`, production alias and Cloudflare account. It uses the
installed Wrangler 4.146.0 CLI and existing OAuth session. Inherited API tokens,
application credentials and unrelated deployment environments are excluded
from child processes. Only `BACKEND_URL` is updated, using stdin; the existing
bridge secret remains unchanged. Wrangler's Pages secret command defaults to
the production environment. No new build runs during recovery.

Browser-like health probes use `User-Agent: Mozilla/5.0` and
`Cache-Control: no-cache`. An unauthenticated `/api/state` returning 401 is
healthy. A public 403, authentication failure or WAF restriction alone does not
justify recovery. After two unhealthy public polls, recovery requires a healthy
local backend and Mac bridge, an existing administrator login and authenticated
local state, then two failed direct probes of the old tunnel with the same user
agent and existing bridge key. DNS errors, gateway failures or Cloudflare's
explicit tunnel-not-found error qualify; ambiguous timeouts and policy failures
hold for review.

The replacement quick tunnel connects only to the configured loopback backend.
Its PID, command and start time are recorded. Direct unauthenticated state must
return 401 and authenticated state must return 200 before the Pages binding can
change. The approved upload is copied to a new private snapshot, with every
file hash verified. Extra files, changed bytes and symlinks fail validation.
The exact snapshot is deployed with `--no-bundle`, then both the immutable
deployment URL and production alias must pass API checks and served asset hashes.

The supervisor atomically commits one canonical private record binding the PID,
route and verification receipt, then updates the existing route/PID compatibility
files, including the existing Pages secrets JSON's `BACKEND_URL`. Other secret
fields are preserved and fenced against concurrent edits. It stops only the old
recorded tunnel if its command and start time still match. PID reuse never
authorizes killing another process. A supervisor lock excludes another
supervisor, and a separate shared Pages writer lock coordinates source deploys.
Hash comparisons detect changes to the active route/PID/secrets mirrors.

Pending recovery phases survive supervisor restart and reuse the same candidate.
Every resumed origin is validated before credential-bearing probes; a candidate
must also match the URL in its private, process-specific log. Unknown secret or
deployment outcomes hold for reconciliation and never repeat a mutation.
`secret_pending` and `deploy_pending` mean an outcome is unknown. A known secret
success is saved as `secret_updated`, allowing only the still-unattempted deploy.
`verification_pending` resumes API/asset verification without any Cloudflare
CLI command. Holds back off from 60 to 300 seconds. A disappeared pending
candidate requires operator review. Restart after canonical commit finishes
compatibility files without redeploying and rechecks the candidate and public
health before stopping the old process.

## Shared Pages deployment protocol, required before live startup

All deployment owners, including root's normal same-tunnel frontend releases,
must use the configured shared writer lock and canonical deployment state.
The supervisor's own lock cannot detect an unrelated CLI deployment. Do not
start supervision until those owners adopt this protocol. There is no generic
ignore-hash option and no inferred permission to restore older source.

The mode-0600 canonical record contains `account_id`, `project`, `branch`,
`generation` (a fresh 32-character hexadecimal identifier for each approved
source release), `plan_sha256`, `asset_manifest_sha256`, `deployment_origin`
(the verified immutable eight-hex-digit Pages URL), `backend_url` and `status`.
Initially adopt it only after verifying the actual latest approved deployment
and asset manifest. A proposed record is not evidence of production adoption.

Normal deployment scripts import this tool and wrap **every** Pages mutation:

```python
config, previous_plan, config_digest = validate(private_config_path)
with source_deployment(config) as (current, finalize):
    # Use current['backend_url'], never a previously cached BACKEND_URL.
    # Apply the approved CLI commands and verify the new immutable URL,
    # production alias, authenticated API, 401 guard and complete asset hashes.
    finalize(new_reviewed_plan_path, verified_immutable_url)
```

`source_deployment` atomically reserves `deployment_pending` before yielding.
An exception or omitted finalization keeps the reservation pending, blocking
recovery and another source writer. Finalization records a new generation and
manifest, so an old supervisor configuration holds even if the tunnel and PID
are unchanged. Re-review and repin the supervisor configuration to the new
approved source afterward. The helper does not perform or authorize the CLI
commands or assert their verification on the caller's behalf.

Recovery reserves `recovery_pending` with its journal ID under the same lock
before tunnel or cloud changes. Pending recovery blocks normal source writers,
including after the supervisor exits. Successful recovery keeps the source
generation and manifest, updates the canonical backend route and immutable
deployment URL, then restores `ready`. The canonical record's backend URL is
authoritative for future deploys; the Pages secrets JSON is a crash-resumable
compatibility mirror.

Unknown outcomes require a separately reviewed reconciliation under that same
writer lock. For a lost deploy result, identify the actual production deployment
in the pinned account/project and prove its approved assets and replacement
connection before recording its immutable origin and advancing the journal to
`verification_pending`. Do not merely clear a pending phase or retry its CLI.
An unknown secret result likewise stays held until its intended binding is
established by reviewed evidence. Preserve both tunnel processes during review.

## Private configuration

Save a mode-0600 JSON object outside Git with these fields:

| Field | Meaning |
| --- | --- |
| `plan`, `plan_sha256` | Approved mode-0600 recovery plan and its exact SHA-256. The plan contains the absolute `upload` directory and a `files` mapping of relative paths to SHA-256 hashes. |
| `bridge_file` | Existing mode-0600 JSON containing `backend_bridge_key`. |
| `login_file` | Existing mode-0600 JSON containing the approved administrator's `email` and `password`. Used only for backend sign-in; tokens and state bodies are never logged. |
| `local_origin` | Existing `http://127.0.0.1:PORT` backend. |
| `public_origin` | `https://text-monkey-demo.pages.dev`. |
| `project`, `branch`, `account_id` | `text-monkey-demo`, `demo`, and the reviewed existing Cloudflare account ID. |
| `node`, `wrangler`, `cloudflared` | Absolute paths to the installed executables and Wrangler CLI script. |
| `state_dir` | New private mode-0700 directory outside Git for lock, journal, canonical record, held receipts, tunnel logs and upload snapshots. |
| `route_file`, `pid_file` | Existing private route JSON (`backend_url`) and plain integer tunnel PID file. |
| `pages_secrets_file` | Existing mode-0600 Pages secrets JSON. Recovery updates only its `BACKEND_URL` and preserves the bridge key. |
| `deployment_state_file`, `deployment_lock_file` | Shared canonical Pages record and writer lock in the same private directory, used by every deployment owner. |
| `deployment_generation` | Exact reviewed source generation from that canonical record. A new source release invalidates the old supervisor configuration. |

Validate without network or process actions:

```sh
python tools/public_connection_supervisor.py --config /private/path/supervisor.json
```

After explicit review, the operator may run the same file with the printed
configuration digest. No launch agent, scheduler or Codex automation is created
by this tool. Ctrl-C stops supervision while preserving the active tunnel. The
Mac must remain awake and connected; this cannot make a laptop always available.

`current.private.json` is the canonical atomic record; the compatibility files
are separate atomic mirrors, not a multi-file transaction. Existing tools that
read only those mirrors can see an intermediate update if the process crashes.
`recovery.private.json` resumes that update under the lock. The operator should
inspect pending state before doing a separate manual recovery.

Synthetic tests cover ordering, old-tunnel confirmation, WAF/authentication holds,
upload integrity, exact deployment commands, verification-only restart, unknown
outcome holds, resumed-origin validation, source-generation fencing, shared
writer locks, private permissions, credential-safe subprocesses and PID reuse.
They establish no live Cloudflare or native delivery result.
