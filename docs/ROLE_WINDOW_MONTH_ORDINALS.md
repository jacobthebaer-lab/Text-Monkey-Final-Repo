# Role-specific monthly weekday restrictions

`recurring_windows` accepts two optional calendar fields. They apply only to that
window's role, weekday and event context, before checking its clock interval or
named-group event-follow mode. Existing windows without these fields retain
their historical weekly behavior.

- `month_ordinals`: a nonempty list of at most five integer occurrences, each
  from 1 through 5. `[2]` means the second occurrence of the window's weekday in
  each permitted month. `[1,2,3,4,5]` explicitly permits every occurrence. A fifth
  weekday is eligible only when it actually exists; there is no fourth-week
  fallback. Booleans, strings and out-of-range values are invalid.
- `months`: a nonempty list of at most twelve exact `YYYY-MM` values. Use this
  only for explicitly positive availability restricted to particular dated
  months. It never creates an annual rule. Omission imposes no month restriction
  on a new window. Invalid values hold eligibility.

The calendar date is the actual event start in the church's configured timezone,
including when its UTC timestamp is a different day or month. The existing whole
event interval, mapped group, role, qualifications, explicit date exclusions and
monthly serving caps still apply.

This fictional normalized example permits childcare only on the second
Wednesday, 18:00 to 20:00, for the already configured women's group. IDs must
come from the actual administrator catalogue; labels alone cannot grant a match.

```json
{
  "weekday": 2,
  "role_ids": [101],
  "role_label": "Fictional Childcare",
  "any_role": false,
  "time_mode": "clock",
  "start_time": "18:00",
  "end_time": "20:00",
  "all_day": false,
  "event_context": {"label": "Fictional Women's Group", "event_type_ids": [201]},
  "month_ordinals": [2]
}
```

Separate Sunday Greeter and Production windows remain weekly. Their independent
`role_frequency_caps` can limit each role to twice per month; a weekday ordinal
is not a frequency cap and must not be stored as a global planning pattern.

If the sender explicitly says this childcare availability is **only December
2026**, add `months: ["2026-12"]` to that window. If the sender says **December
2026 off**, retain dated `unavailable_dates` for that month instead. Do not add a
positive `months` field or block future Decembers. Never infer a year.

Gloo supplies the structured interpretation. Followups that omit the snapshot
preserve prior windows. When Gloo re-emits the same role, weekday and mapped
context without optional scope fields, merging preserves their prior values,
including a clock-hours correction. Exact prior clock intervals disambiguate
multiple windows; if their remaining calendar scopes conflict, validation holds
the result for resolution rather than choosing a broader interpretation.
If the same role and weekday changes event context, any calendar fields present
on its prior windows must be explicitly supplied in the new window. This also
applies when a label-only group becomes a mapped catalogue group. Omission
holds the result instead of dropping scope or transferring another group's
restriction. Gloo must return the intended calendar fields before eligibility
can use the resolved window.
Explicit supplied ordinal values replace old values; an explicitly cleared
window snapshot `[]` retains the existing clear behavior. All existing Gloo,
unfinished-signup, care, consent and assignment safeguards remain independent.

Profile serialization retains both optional fields while mapping role and event
IDs by stable catalogue names. This patch changes no schema, runtime settings,
administrator catalogues or live profiles, and does not establish delivery or
actual scheduling proof. Tests use in-memory fictional data and the real
candidate selection and assignment eligibility paths.
