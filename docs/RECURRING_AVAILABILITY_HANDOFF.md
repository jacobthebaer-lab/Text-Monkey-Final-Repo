# Role-specific recurring availability

A volunteer can state different roles, weekdays, local time ranges and group
contexts in one answer. Gloo now has a schema for these facts; checked code
validates them without converting ranges or groups into service-hour enums.
Eligibility requires a matching role/context window covering the whole event.
Sunday 08:00–10:00 never permits a 10:00–11:00 event.

Base: `edc40f29053dbe0c31b0728338b9f7ff15ab1f44`.
Worktree: `/Users/jacob/.codex/worktrees/role-availability-windows/Text Monkey`.
Branch: `codex/role-availability-windows`. Local commit is recorded in the final
handoff receipt. No push; integration and live runtime belong to their owners.

## Owned files

- `app/core/recurring_availability.py`: schema instructions, strict normalizer,
  merge helper, complete-event window matching.
- `app/core/eligibility.py`: authoritative window rule replacing legacy flat
  weekday/service/role preferences when windows are nonempty. Qualification,
  paused-role, date-exclusion, unfinished-signup and overlapping-assignment rules
  remain independent. Ranking, assignment writes, review and delivery already
  use this shared check.
- `tests/test_recurring_availability.py`: 31 cases, including exact end boundaries,
  local timezone, mixed roles/groups, unresolved mapping, corrections, frequency
  retention, invalid schema, candidate selection and assignment write rejection.
- `tools/verify_recurring_windows.py`: one real Gloo interpretation of fictional
  input without transport, sessions or profile writes.
- `docs/evidence/recurring-windows-real-gloo.json`: safe fictional real-Gloo proof.
- `docs/RECURRING_AVAILABILITY_HANDOFF.md`: this receipt.

No onboarding, signup prompt or conversation-copy files were edited. The
conversation editor must integrate the contract below before any live replay.

## Conversation editor contract

Import `WINDOW_SCHEMA_INSTRUCTIONS`, `normalize_recurring_windows` and
`merge_recurring_windows` from `app.core.recurring_availability`.

1. Append the schema instructions to the existing real Gloo interpreter, passing
   role and event-type catalogues, sender-specific saved windows and selected
   role facts. Do not interpret the sender's text with keyword rules or convert
   the old invalid enum strings into guessed hours/context.
2. Preserve `recurring_windows` in availability context, the validated partial
   snapshot and final volunteer preferences. Call
   `merge_recurring_windows(data, previous, roles, event_types)` while validating.
   Omitted windows retain previous facts; a supplied list is Gloo's complete
   corrected snapshot. [] is reserved for an explicit removal.
3. A valid nonempty window snapshot supplies availability facts. Clear legacy
   preferred_services for ranges instead of inventing single-hour service enums.
   Frequency stays unknown until supplied; ask for missing frequency when it is
   the only missing signup fact. Windows never imply consent or qualifications.
4. Unmapped role/group labels are stored with empty catalogue-ID lists and stay
   ineligible until genuinely resolved. Unspecified hours use null/null and
   all_day=false, preserving the difference from an explicit all-day statement.

Window fields are weekday (Monday=0), role_ids, role_label, any_role, start_time,
end_time, all_day and event_context. Times are church-local HH:MM; end 24:00 is
supported. An event context is null or {label,event_type_ids}. Known type IDs
match event.event_type_id; arbitrary title similarity cannot broaden eligibility.
Named roles use any_role=false. Explicit any-role flexibility uses true with
empty role IDs and null role_label. Cross-midnight ranges are deliberately held
for clarification; an event ending exactly at the following midnight can fit a
window ending 24:00. A single window must cover the entire event.

### Eligibility correction after independent review

Null/null with all_day=false is unknown and **ineligible**, including after a
frequency-only reply and when the event-type mapping is resolved. The editor
must request actual hours or explicit all-day availability rather than treat
the window as midnight-to-midnight. A group-type ID is not an exact serving
interval; this module currently has no verified exact-event binding API.

Timed windows hold any offset-changing interval, because local endpoints cannot
prove that the entire actual event fits a clock-time range. This conservatively
holds the Denver fall-back example 2026-11-01 07:30–08:40 UTC (01:30 MDT–01:40
MST) for a 01:30–01:45 window. Explicit all-day windows can cover an otherwise
matching local-day event across DST. Interval order is checked in UTC, so the
repeated hour cannot accidentally invalidate explicit all-day availability.

Signatures/schema remain unchanged. The narrow followup's final commit hash is
in the final receipt. **135 focused eligibility/fill/review tests passed**,
including eight new regression cases. No broad rerun, new Gloo call or live
runtime action was performed for this correction; the previous full-suite and
Gloo evidence below remain scoped to the initial release.

## Verification and limits

- Focused eligibility/fill/review suite: **127 passed**.
- Final full Python suite: **827 passed, 1 expected failure**, 21.02 seconds.
- `git diff --check` passed.
- One real Gloo call (`gloo-openai-gpt-5-mini`) correctly returned distinct
  Sunday greeting 08:00–10:00 and Wednesday group-specific coffee windows,
  frequency_unknown with null maximum, and unspecified Wednesday hours.
- No actual recipient conversations, credentials, databases or native receipts
  are in the commit. The checked-in reply is explicitly fictional.
- No live texts, runtime DB modifications, readers, session renewals,
  Planning Center or Supabase writes occurred. Existing actual inbound/Gloo
  records were left intact; only the live owner may reprocess after integration.

The one-call Gloo probe verifies schema interpretation; the focused/full tests
verify validation and shared eligibility. Full conversational integration and
actual transport/reply verification remain with the editor and live owner.
