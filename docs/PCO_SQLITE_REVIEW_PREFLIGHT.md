# SQLite frequency-review schema handoff

This batch supplies the remaining SQLite-specific artifact for the private Mac
store. It adds no application imports, routes, settings, signing keys, scheduler
or executor wiring. The existing PostgreSQL artifact is unchanged. No live
migration is applied, and schema compatibility never authorizes native writes.

## Read-only report

From the reviewed checkout, use the explicitly selected existing SQLite target:

```sh
python -m scripts.pco_sqlite_review_preflight --database /explicit/private/target.db
```

There is no apply option. The target is resolved as an existing file, opened with
SQLite URI `mode=ro` and `query_only=ON`, and inspected within a read transaction.
Paths are URI-escaped. Only schema metadata is read; no messages, phone numbers,
profile JSON, credentials or row contents are selected. The report omits the
target path, hashes its filesystem identity, and hashes the observed relevant
schema. Keep actual-target reports outside Git in a mode600 private file. The
report is a point-in-time snapshot; recheck it immediately before a separately
authorized migration. Exit0 means schema-compatible, not approved; exit2 is a
compatibility hold or sanitized unavailable-target result.

Source tables are checked against the nine committed-source models for required
columns, declared types, nullability, primary keys and identity uniqueness.
Additional source columns are preserved. The six feature tables require the
exact model columns and named eight indexes, no unexpected indexes/constraints
or triggers. Any CONFLICT token in feature-table SQL is conservatively held,
including explicit ABORT and appearances in comments or strings. This handles
case, whitespace and comments separating ON and CONFLICT without parsing SQL.
The reviewed schema relies on default constraint errors; REPLACE or IGNORE
could silently bypass a durable claim or attempt uniqueness safeguard.
Partial or incompatible feature schema and global index-name
collisions hold application. Equivalent but differently declared types are
conservatively held for owner comparison. `complete` compatible schema means
the creation artifact is unnecessary; never rerun it against existing tables.

## Proposed SQLite artifact

[SQLite DDL](migrations/20261003_pco_frequency_reviews.sqlite.proposed.sql) creates
only the six specified feature tables and eight indexes. It uses DATETIME for
the existing UTCDateTime storage convention, explicit NOT NULL primary keys
and no PostgreSQL schema/RLS clauses. Required source-column SELECTs use WHERE0
and return no private records. It does not change source tables or populate any
mapping, approval, receipt, permission or claim.

The sole runtime owner must independently review the artifact, verify the exact
checkpoint/target and fresh private preflight, preserve a consistent SQLite
backup including WAL state, and obtain explicit selected-target application
authorization. Coordinate all writers before application. Run the complete
transaction with stop-on-first-error; for example SQLite CLI's `-bail` behavior.
On any error roll back or close the connection without committing. The artifact
uses BEGIN IMMEDIATE and plain CREATE statements, so missing sources, existing
tables or index collisions fail instead of being silently adopted. The report's
key/index/constraint checks remain mandatory; the SQL column checks alone do not
prove full compatibility. No application startup or broad metadata.create_all
should apply this file.

After an approved application, rerun the read-only report to require compatible
complete schema. Preserve all existing data, receipts and unknown claims. A
partial/incompatible target needs a separately reviewed forward correction;
never drop a table or reset claims to make this creation artifact pass. SQLite
has no PostgreSQL role/RLS enforcement, so protect the database, backup, WAL/SHM
and parent directory under the existing private backend access policy. Source
schema inspection does not establish that the real committed source, mapped
person/membership, current recipient scope or notification policy is ready.

Explicit remaining holds: actual source/mapping verification, migration review
and application authorization, private signing-key provisioning, protected review
router and durable-authority executor wiring, native notification silence,
native-edit coordination and fresh native preflight. None is manufactured or
cleared by the script, artifact or synthetic tests. No text-session extensions,
Noah resume, transport/permissions or outreach-algorithm changes belong here.

```sh
python -m pytest tests/test_pco_sqlite_review_preflight.py
```

Focused tests execute DDL only in temporary synthetic databases, preserve a
source sentinel, verify all model tables/indexes and NOT NULL/unique primary
keys, and exercise rollback, missing source/identity uniqueness, partial or
incompatible schemas, unexpected triggers/indexes, index collisions, URI
escaping and the read-only/no-apply CLI. Actual private-target findings remain
outside the source handoff.
