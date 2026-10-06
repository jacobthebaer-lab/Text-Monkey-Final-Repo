# Calendar preferences and seasonal review

The existing monthly collection already expands “second and fourth Sundays”
within a selected month. `planning_patterns.py` adds a durable, typed calendar
restriction for later months without changing the scheduler's ranking algorithm.

```
calendar_patterns = {
  "weekday_ordinals": [{"weekday": 6, "ordinals": [2, 4]}],
  "annual_unavailable_months": [12]
}
```

Monday is zero and Sunday is six. Ordinals mean the occurrence of that weekday
in the month. Fifth weekdays exist only when the calendar actually has one.
Annual absences require an explicit recurring statement or exact human review.
“Away this December” remains a one-time dated exclusion, never an annual rule.

Integration contracts:

- Append `calendar_reasons(volunteer.preferences, event.starts_at, timezone)`
  to the existing eligibility reasons. It only restricts dates; consent,
  qualifications, role windows, one-time exclusions, budgets and review still
  apply independently.
- `calendar_dates(patterns, month)` expands a selected future month for an
  existing approved collection. It does not write availability or infer permission.
  Annual absence alone cannot supply positive availability for other months;
  expansion requires an explicit weekday/ordinal pattern.
- `learned_patterns(session, volunteer, now, timezone)` returns a review-only
  proposal from completed assignments/events in the last three calendar months.
  A weekday/ordinal must appear in at least two separate months. Duplicate
  services on one date cannot inflate that threshold. No absence is inferred
  from nonattendance, and existing explicit annual absences are retained.
- The authenticated administrator/Gloo tool may call `stage_pattern_review`
  to stage the existing `confirm_record` review. It snapshots the entire prior
  volunteer and changes only the calendar preference. Existing review handles
  approval, expiry and stale-record checks. Never auto-approve a history proposal.
- `seasonal_staffing_report(session, month, now, timezone)` is a read-only
  recipe recommendation. It compares actual recorded role-slot counts for the
  same event type and calendar month across at least two prior years with the
  actual future event's slots. The report exposes source event IDs and counts;
  no attendance forecast, staffing requirement, invitation or booking is invented.
  Unknown event types and insufficient history are shown as unresolved.

The planning/eligibility owner integrates the restrictive hook. The natural
administrator/capacity owner exposes proposal and report tools and routes any
staffing change through existing explicit review. This module never sends texts,
calls a model, changes qualifications, adds slots or creates assignments.
