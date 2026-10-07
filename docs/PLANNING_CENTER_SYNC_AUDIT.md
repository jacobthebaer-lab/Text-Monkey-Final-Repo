# Planning Center and Supabase sync audit, October 6, 2026

Baseline: `1232893df8c03d993e5fc1fa9067162718973466` on
`codex/complete-text-monkey`. Reproductions used synthetic API responses and
disposable SQLite databases. This batch does not deploy, migrate a live store,
write a native API, run a second scheduler or send real messages.

## Reproduced defects and fixes

| Defect | Before | Correction and regression |
| --- | --- | --- |
| Event starvation | Every metadata tick selected the first 100 links; a 101st link never reconciled. | A durable cursor rotates within the exact organization/service scope. A separate file-backed engine resumes after the first batch and visits the remaining link. Per-plan leases still fence writes. |
| Stale availability identity | An empty cache continued clearing a volunteer after person/organization changes, deletion or a phone change. | Cache identity includes mapping ID, normalized creation time and phone hash. Eligibility holds changed, replaced, legacy or not-yet-refreshed runtime identities. Ordinary unmapped local volunteers remain unaffected. |
| Invalid availability cache | A list raised an exception; a reversed interval could silently clear eligibility; refresh could not recover corrupt JSON. | Invalid cache shape/intervals hold. A verified fresh native read can replace malformed cache data. |
| Availability lock and race | Runtime refresh autoflushed a cache insert before HTTP. A concurrent SQLite update failed with `database is locked`; there was no post-GET identity/cache comparison. | Runtime refresh closes its read transaction before GET and commits only through a short identity/profile/cache fence. Concurrent mapping, phone or cache edits hold the old result and preserve the newer record. |
| Untitled plan corruption | Schedule import used the service name for an untitled plan, then metadata sync pulled an empty title into that same event. | Both paths use the same service-name fallback and 200-character projection. Fallback resource identity is validated. |

The initial adversarial suite reproduced eight failures; the separate untitled
plan and file-backed writer-lock cases also failed before their fixes. The new
audit file has 23 regressions, including restart, concurrent edit and malformed
organization-ID cases. Independent review found that list/dictionary organization
IDs could reach a SQL bind and raise an exception; strict numeric-string
validation now holds these values before the query, without hiding DB failures.

Two pre-reservation HTTP expectations were also stale. Their replacement checks
the autoqueued approved reservation, native U, real synthetic HTTP confirmation
to C, and cancellation to D on the same native record. Profile publication still
makes no staffing write and does not mirror assignments into the cloud.

## Validation

```sh
python -m pytest -q -o addopts='' tests/test_planning_center*.py \
  tests/test_eligibility.py tests/test_ranking.py tests/test_profile_sync.py \
  tests/test_planning_profile_sync.py tests/test_cloud_registration.py \
  tests/test_independent_text_database.py
```

Result: **458 passed**. This covers connector and neighboring profile/eligibility
contracts, not the entire application suite. `git diff --check` passed.

## Feature catalog disposition

| Catalog ID | Audit evidence | Remaining boundary |
| --- | --- | --- |
| `pco-import` | Scoped import tests; matching untitled projection regression. | Existing links only; import is not automatic native creation of local events. |
| `pco-webhook` | Signed receiver, replay/deduplication and tamper tests. | Native subscription/runtime configuration remains owner-managed. |
| `pco-mappings` | Explicit person mapping and exact scope rejection tests; cache identity regressions. | Services and People identity are separate; no native membership or consent is inferred from signup. |
| `pco-writeback` | Approved/U, confirmed/C, cancelled/D, replacement dependency, unknown outcomes and HTTP transition tests. Prior live U evidence can be reused. | This audit makes no fresh real C/D or replacement delivery claim. |
| `pco-reconciliation` | Three-way conflicts, pre/post HTTP fences, unknown recovery, capacity barriers and availability CAS tests. | Conflicting records hold for scoped reconciliation. One scheduling store remains authoritative. |
| `pco-notifications` | False/null preparation fields and native readback tests. | Account-level automations and actual notification silence require independent live evidence. |
| `pco-future` | Unsupported split/per-time/child interval operations remain held in tests. | Future/non-goal scope; do not substitute plan-wide acceptance. |
| `pco-open-needs` | Open needs plus C/U capacity arithmetic and non-resizing mismatch tests. | Open needs are unfilled places, not total staffing or confirmed coverage. Unmapped roles are not resized. |
| `pco-idempotent-import` | Stable event/shift keys, occupied history preservation and create-only local edit protection tests. | A metadata/profile mirror is not an assignment mirror. |
| `pco-position-mappings` | Reviewed role bindings and exact native position/time tests. | Only reviewed mapped roles; no broad adoption of native positions. |
| `pco-coverage` | U counts as reserved; only local confirmed plus verified native C and resolved latest intent counts as coverage. | A live U reservation is not live confirmed coverage. |
| `pco-preferences` | Profile publication, role remapping, blockout import, held frequency preview/executor and correction lineage tests. | Broad frequency execution stays disabled. General hours, global caps, relative windows and outbound blockouts have separate ownership, source and notification contracts. |

Supabase registration and profile tests passed in the isolated suite. Existing
private PostgreSQL/schema acceptance is prior evidence, not a fresh database
audit. The live Mac scheduler remains the sole delivery/scheduling authority;
cloud scheduling, staffing and API exports remain disabled.

## Integration and morning follow-up

No additional database table or migration is required: cursor and cache data use
existing private Policy rows. After source review and owner-controlled rollout,
old availability caches deliberately hold until the normal GET refresh supplies
their new identity fields. Deleted mappings with old caches require explicit
local reconciliation instead of silently clearing a prior constraint.

The explicit one-shot `refresh_mapped_availability(session, ...)` adapter retains
caller-owned transaction semantics. Background jobs now use
`sync_mapped_availability(factory, ...)`; new runtime callers should use it too.

Actual native decline-to-replacement phone delivery and broad outbound native
availability remain unverified. The runtime owner should use a specifically
authorized real test in the morning, preserve the existing conversation and
native record, and verify recipient delivery separately. Synthetic evidence does
not authorize overnight real sends, paid transport or a second writer.
