# Connected-app user registration

Supabase remains the shared account and volunteer backend. Volunteer records
live in `texty.volunteers`; Authentication users represent administrator logins.
The Table Editor paginates at 100 rows, so a newer volunteer can be on page two.

The bounded Mac demo retains a separate SQLite transport store. Its existing
profile publisher only follows explicitly scoped recipients. The database-only
registration worker closes the gap for **future volunteer profiles created in
that connected store**, including administrator additions. It does not extend
texting permissions, send messages, call Gloo, run scheduling, copy conversation
text, grant privileges or change existing cloud profiles owned by other writers.

Initialize a private journal once against the intended source and target. This
records existing profile identities as a baseline, excluding historical fixtures
from automatic backfill. Future normalized phone identities are registered in
the private `texty` schema. Existing cloud identities are preserved. Incomplete
profiles are saved as inactive; validated complete preferences are translated
through the existing profile publisher's role mapping and conflict guards.
Unresolved preferences retain a pending identity. Failures retry, and the unique
phone and cloud receipt marker prevent duplicates after a lost acknowledgment.

```sh
python tools/register_cloud_users.py --source-db "$PRIVATE_SOURCE_FILE" \
  --scope-file "$PRIVATE_PROJECT_SCOPE" --target-env-file "$PRIVATE_TARGET_ENV" \
  --journal "$PRIVATE_REGISTRATION_JOURNAL" --initialize
python tools/register_cloud_users.py --source-db "$PRIVATE_SOURCE_FILE" \
  --scope-file "$PRIVATE_PROJECT_SCOPE" --target-env-file "$PRIVATE_TARGET_ENV" \
  --journal "$PRIVATE_REGISTRATION_JOURNAL" --watch
```

The scope supplies `project_ref` and optional `role_map`; recipient text scopes
remain independently enforced by the transport. The target uses the existing
private database credential, project binding and TLS connection checks. Source
SQLite is opened read-only. A single-worker journal lock, restrictive file
permissions and source-file binding protect retries and refuse silent rebaselining
after a replaced database. Keep the journal, credentials and receipts outside Git.

The worker must be running for this local-to-cloud registration path to work.
It does not make the entire local and cloud scheduling databases identical, or
change a browser-local disconnected preview into the connected application.
