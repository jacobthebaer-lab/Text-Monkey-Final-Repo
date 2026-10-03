# Independent text-to-database acceptance

The recorded checkpoint has 13 passing released inbound checks plus 4 passing
provisional persistence checks and one reproduced profile restriction defect.
See `receipt.json` for exact proof limits and hashes of the tested source snapshots.
These results do not verify a combined release or any live external operation.

`tests/test_independent_text_database.py` exercises authenticated `/mac/inbound`
with synthetic session proof, durable duplicate receipts, consent, privilege
preservation, and conservative availability validation. Additional cases require
the released profile-outbox and Planning Center staffing modules. Integrate this
fixture only after those owners release their work; otherwise its persistence
cases fail because the application path is absent.

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

## Bounded combined verification after release

Use an isolated checkout, no private `.env`, and a new synthetic database:

```sh
DATABASE_URL=sqlite:// AUTOMATION_ENABLED=false SMS_PROVIDER=mock LIVE_SMS=false PYTHON_DOTENV_DISABLED=1 python -m pytest -q tests/test_independent_text_database.py
```

Also run the existing ingress rollback and exclusion checks once if those routes
changed during integration. Keep full-suite results separate. These fixtures
mock Gloo, Messages queue acknowledgment, cloud storage, and PCO API reads/writes.
They never establish real transport delivery, live Supabase project readiness,
live PCO staffing success, or profile-to-PCO People synchronization.
