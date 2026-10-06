# Reviewed same-date role pairs

This contract closes the planner's independent-role gap. A participant can require
two known roles on the same church-local date, with each role's existing serving
window and qualifications. It adds no signup, consent, qualification, calendar
event, texting or timer activation. It preserves Clyde's algorithm selection and
scores; the shared hard eligibility check can only restrict its existing pool.

## Intake and coordinator review

```python
same_day_role_pairs = [{"role_ids": [greeter_role_id, production_role_id]}]
```

Pairs use two existing, distinct IDs. Pairs must be disjoint; chains and ambiguous
or unknown roles are held. First/second service times remain in the existing
role-specific `recurring_windows`. Role caps remain in `role_frequency_caps` and
never become an unstated global cap.

The intake owner retains `pending_constraints` and its original source while
mapping is unresolved. This module does not read or interpret its description.
For preference-level `pending_constraints`, an authenticated coordinator caller
may stage the exact normalization:

```python
approval = paired_planning.stage_rules(
    session, clock.now(), volunteer,
    pairs=[{"role_ids": [greeter_role_id, production_role_id]}],
    resolved_constraint_indexes=(same_day_constraint_index,),
)
```

Only matching `kind: same_day` entries with those known role IDs can be resolved.
Every unrelated pending constraint remains held. If intake stores constraints
inside a separate pending profile, its owner must publish/complete that profile
through its existing reviewed contract. This helper does not finish signup or
activate a pending draft. Staging changes no participant facts. The normal
authenticated `/api/proposals/{id}/approve` route requires the displayed exact
hash to apply the `confirm_record` review.

A durable `planning-rules:<volunteer_id>` receipt binds executable pairs to that
approved review, its exact hash and current role snapshots. Merely writing a pair
preference or preserving a description cannot create pairing eligibility. A role
change or unmatched pending constraint requires fresh review.

Annual/ordinal calendar restrictions belong to `planning_patterns.py`:

```python
calendar_patterns = {
    "weekday_ordinals": [],
    "annual_unavailable_months": [12],
}
```

The eligibility hook calls its restrictive `calendar_reasons`. December therefore
holds both members of the pair. Ordinal weekdays are a global calendar restriction;
do not infer second/fourth Sundays from a request to serve twice monthly or apply
a Sunday-only global pattern to an otherwise allowed women's ministry event.
Use an empty ordinal list when only an annual absence was explicitly stated.
Pattern review and history inference remain owned by that module; there is no
duplicate `excluded_months` schema here.
John's current December exclusion is one-time. Preserve its explicit December
2026 unavailable dates through the existing monthly `Availability` record; do
not convert that statement into recurring annual absence. His women's ministry
weekday/ordinal restriction must stay role-specific, not a global calendar rule
that excludes otherwise allowed Sunday roles.

## Planning and publication

`scheduler.preview_draft` creates both same-date virtual choices together or
neither. Both full eligibility checks apply, including current consent, signup,
role windows, qualifications, care, overlaps, date exclusions and monthly limits.
A Greeter cap of two permits two paired Sundays and four placements when no global
limit was stated. An explicit global cap of three permits only one whole pair.
An unqualified Production participant produces no Greeter-only proposal and a
clear `held_constraints` reason. Model repairs and swaps still pass the final
complete-pair validator; incomplete virtual groups cannot be published.

Gloo reviews the plan through the existing workflow. Each complete pair becomes
one ordinary `confirm_record`, with `record: AssignmentPair`. Its exact reason
shows both role names and church-local starts; its after-values show both actual
shift IDs. The existing generic record-review UI and authenticated API handle it.
One approval creates both approved planner assignments in the same transaction.
Rejection creates neither; a failed API review rolls back both.

Before publication, the source is reread and compared: current event/role details,
church timezone, preference and qualification hashes, pair-rule proof, consent,
care and slot occupancy. Source changes hold the whole pair; reviewed payloads and
hashes are not rewritten. No text is composed or queued by a record approval.

Replacement eligibility requires an already assigned, currently eligible partner
on the same local date. It does not make a new paired volunteer eligible for a
single unsolicited offer. A partner losing qualification holds the candidate.
Paired context is used only for jointly validating proposed placements; it does
not alter the selection algorithm or manufacture outreach authorization.

## Verification and runtime boundary

Focused synthetic tests cover two service times on two shared dates, a Greeter-only
cap, explicit global limits, unavailable/unqualified partners, overlap, missing
events, Gloo swap validation, annual/ordinal restrictions, pending/unreviewed rules,
source changes, review dedup, and authenticated atomic publication/rollback.
Existing planning, eligibility, ranking, capacity, confirmation, calendar-pattern
and Clyde algorithm tests are checked together. No real Gloo, native Messages,
live database, recipients or scheduling were activated in this lane.

Verification: 346 combined checks passed against finalized pattern dependency
`89b746c8e83e8ee19564b5905b016e145448c09a` (cherry-picked as `353f31a`). The final
recorded-pair status refinement was retested with all 28 paired checks, including
the authenticated API, source changes and calendar integration.

Connected acceptance still requires the reviewed participant profile, actual role
qualifications, approved event/slot records, exact Gloo text reviews and separate
native delivery proof. These code tests do not certify that live runtime.
