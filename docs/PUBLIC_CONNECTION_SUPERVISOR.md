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
files. It stops only the old recorded tunnel if its command and start time still
match. PID reuse never authorizes killing another process. A file lock excludes
another supervisor. Hash comparisons detect changes to the active route/PID by
an external operator before cloud changes and commit.

Pending recovery phases survive supervisor restart and reuse the same candidate.
Unknown secret/deploy outcomes retain both tunnels. There are at most three
cloud mutation attempts per recovery, with hold backoff from 60 to 300 seconds.
Exhaustion or a disappeared pending candidate requires operator review; the tool
does not spawn an unlimited series of replacements. Restart after canonical
commit finishes compatibility files without redeploying and rechecks public
health before stopping the old process.

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
upload integrity, exact deployment commands, restart recovery, retry limits,
private permissions, credential-safe subprocesses, writer locks and PID reuse.
They establish no live Cloudflare or native delivery result.
