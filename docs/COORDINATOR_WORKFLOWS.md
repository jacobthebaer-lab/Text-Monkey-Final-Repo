# Coordinator proposals and capacity review

The signed-in coordinator API uses the existing Gloo agent loop and exact record
review. It supports the current single-church demo. Preparing a request does not
send a message, activate a scheduler, import a calendar or apply a record change.

## Commands

- `GET /api/coordinator` lists existing active coordinator IDs.
- `POST /api/coordinator/command` accepts only `coordinator_id` and `command`.
  Commands contain 1-1000 characters. The existing verified, allowlisted
  administrator login and exact human confirmation mode are required.
- Gloo first reads saved event, role, recipe, schedule and person IDs, plus the
  saved church timezone. It can answer a schedule question or propose
  `create_event_type`, `create_event`, `update_event`, `set_recipe`, `add_slots`,
  `pause_role` and `mark_unavailable`.
- Results expose exact before/after records, review IDs, content hashes, expiry
  and state. Review through the existing `/api/proposals/{id}/approve` or
  `/reject` endpoint with the displayed `content_hash`. The existing review queue
  shows these proposals. A model tool call is never an approval.

New event types have no automatic calendar title-matching patterns. Approve a
new type first, then use its saved ID to request its event or role recipe. Approve
a new event before requesting its individual staffing slots. A recipe sets a
role count for an existing event type; it does not alter an existing roster or
create event slots. Each resulting record has its own exact review.
Changing event times or type is blocked while active assignments exist, including
assignments added after the proposal, so a schedule edit cannot bypass eligibility.

Event times require explicit offsets and a future, positive duration of at most
24 hours. Slot additions accept 1-30 slots; recipes accept counts of 0-20.
Unavailability accepts 1-62 current or future ISO dates. Ambiguous requests should
receive a question from Gloo. Code rejects unknown IDs, privilege/qualification
changes and unsupported fields. Pausing a role preserves existing assignments
and opens an internal review item for them. Marking a date unavailable removes
that date from the same month's available dates, preserving other preferences
and opening an internal item to review affected assignments.

Reviews bind saved source records and church timezone. Changed sources,
coordinator access, duplicate slots/recipes/events, elapsed event times and
changed or expired hashes block application. Database row locks protect existing
source records during application. On a Gloo outage or bounded-loop exhaustion,
new partial proposals expire; previously completed pending reviews are preserved.
Recognized sensitive commands stay internal and do not reach Gloo. Tool responses
exclude the full before/after volunteer profile; exact records remain in human
review. Existing SMS approval and connected legacy operations remain guarded.

## Capacity narration

- `POST /api/coordinator/capacity` accepts `{}` and explicitly runs a scan.
- `GET /api/coordinator/capacity` reads saved flags without a model call.
- Code computes evidence, flags and permissible human next steps. Gloo selects
  a brief observation, optionally with a warm lead-in, and the supplied next step
  through `narrate_flag`. This is intentionally bounded narration: arbitrary
  free-form claims and actions are rejected.
- Each explanation binds its actual flag ID and evidence hash. Invented facts,
  diagnoses, qualifications, source IDs, actions or em dashes cannot publish.
  Only a completed run publishes validated narration. Missing narration, changed
  evidence, an outage or step exhaustion leaves a visible hold with the original
  structured observation, suggested human next step and scan timestamp retained.
- Flags are deduplicated by their existing evidence keys. Counts describe recorded
  assignments, not verified attendance. A profile's age is not opt-in timing.
  Unknown frequency limits remain unknown; role caps do not become global caps.

These APIs do not add frontend chat controls or connected background capacity
jobs. Their responses are available to the signed-in dashboard client, and exact
record approvals already use its review queue. No transport, qualification,
consent or scheduling authority is granted to Gloo. Automated Google Voice remains
held by project policy. Synthetic tests establish code behavior only; real Gloo
and native delivery remain separate checks.
