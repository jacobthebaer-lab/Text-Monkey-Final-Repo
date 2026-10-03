# Independent text-to-database acceptance

The frozen combined release `ac068a40b573acc1fc202b910507c9ecbd861011` passed
18 original focused HTTP/ingress checks. One added case also passed with profile
and staffing observers enabled in the same app. Both staffing variants then
passed explicit assertions that initial read-back and cancellation retain one
local assignment history row. There are 19 distinct passing cases, no failures.
See `combined-receipt.json` for the current proof and its limits.

The earlier `receipt.json` is historical provisional evidence: 13 released
inbound checks plus 4 provisional persistence passes and the reproduced role
restriction defect. That defect is resolved in the tested combined release;
role/time windows now retain exact cloud catalogue mappings. All tests remain
synthetic; no real external write or delivery is established.

`tests/test_independent_text_database.py` exercises authenticated `/mac/inbound`
with synthetic session proof, durable duplicate receipts, consent, privilege
preservation, and conservative availability validation. Additional cases require
the released profile-outbox and Planning Center staffing modules. They were
present in the combined source above. A checkout without those modules cannot
establish this acceptance path.

The cloud cases use a second isolated SQLite store and different cloud identities.
They exercise queued profile publication, failed write retry, existing cloud
opt-out protection, privilege preservation, and lost local acknowledgment after
a cloud commit. Role windows must preserve exact verified mapped cloud catalogue
identities or remain explicitly held with no cloud mutation. They must never
report synced after silently discarding a restriction.

The staffing case constructs the real app with the staffing observer enabled,
then confirms and cancels an assignment through HTTP. Only mapped PCO staffing
read-back establishes verified state in the strict mock API; inbound retries
produce no extra writes. No helper-only enqueue establishes application wiring.

## Reproducing the bounded combined verification

Use an isolated checkout, no private `.env`, and a new synthetic database:

```sh
DATABASE_URL=sqlite:// AUTOMATION_ENABLED=false SMS_PROVIDER=mock LIVE_SMS=false PYTHON_DOTENV_DISABLED=1 python -m pytest -q tests/test_independent_text_database.py
```

Also run the existing ingress rollback and exclusion checks once if those routes
changed during integration. Keep full-suite results separate. These fixtures
mock Gloo, Messages queue acknowledgment, cloud storage, and PCO API reads/writes.
They never establish real transport delivery, live Supabase project readiness,
live PCO staffing success, or profile-to-PCO People synchronization.
