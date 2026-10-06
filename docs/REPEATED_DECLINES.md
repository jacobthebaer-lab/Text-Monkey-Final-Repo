# Repeated-decline concerns

This adds the separate `dropoff` universe extension for repeated declines. The
existing six-week absence detector is unchanged. The new helper creates internal
`repeated_declines` flags, never a text, escalation send, assignment, qualification
or suggestion to pressure someone.

Project defaults, not official or competition thresholds, are three declined
distinct event offers in 56 days spanning at least 14 days. Prior known serving
history requires three completed assignments on distinct completed events in the
last 365 days, spanning at least 28 days and ending before the first decline.
Change thresholds with Policy `capacity_repeated_declines`, value
`{"value":{"minimum_events":4,"window_days":70}}`; supported settings appear in
`DEFAULTS`. Unknown keys, noninteger/bool values and inconsistent windows hold the
scan. Offers and completed history are each bounded to 5,000 records by default.
A truncated scan holds without falsely resolving an existing concern.

Only a newly recorded actual scoped decline counts. `record_decline` binds its
original received Message to one Outreach, FillRequest, event, submitted offer,
dispatch/deadline chronology and unchanged message hashes. It requires an active
dispatched offer plus `sent`, `submitted` or `delivered` status and the actual
reply. Native/API submission alone is not confirmed handset delivery. Evidence
labels submission with an actual scoped decline separately from provider-delivered
status. Queued, dispatching, rejected, failed, uncertain, expired-before-response,
partial, accepted and opt-out replies do not qualify. Bare legacy
`Outreach.response="no"` records are not replayed or backfilled.

Multiple roles or fills for the same event count once. The concern requires an
active opted-in nonadministrator volunteer and excludes current suppression.
Each scan rechecks original proof before counting it. It updates one stable flag
per volunteer; below-threshold, opted-out or expired evidence marks that flag
`resolved` and retains its previous evidence. A later qualifying pattern can
reopen the same row. Human dismissal stays dismissed. Flags contain evidence IDs,
dates, thresholds and hashes, without raw conversations or phone numbers.

## Minimal owner hooks

In `fill_agent.on_outreach_reply`, immediately after the validated decline sets
`outreach.response="no"` and `outreach.responded_at`, in the same transaction:

```python
if intent == "decline" and ctx.reply_to_message_id is not None:
    from app.core.repeated_declines import record_decline
    record_decline(session, outreach, ctx.reply_to_message_id, now)
```

This hook must follow existing offer scope/deadline/intent validation. The helper
does not classify input or supply consent. Trusted calls without actual inbound
evidence produce no record; the old generic no-code path remains uncounted.

In `capacity_agent.scan`, before its logger records the final `flags` list:

```python
from app.core.repeated_declines import refresh
decline_review = refresh(s, now)
flags.extend(decline_review["flags"])
```

The owner may expose the fixed `held` reason in internal scan diagnostics. No
model/provider call belongs in either hook. Both hooks are integrated. Capacity
scans narrate these internal flags through the existing evidence-bound Gloo
workflow; an outage holds narration and preserves evidence. Nothing contacts
the volunteer.
