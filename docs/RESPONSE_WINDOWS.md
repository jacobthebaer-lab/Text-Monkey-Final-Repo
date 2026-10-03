# Response-window policy

The implementation was built separately from `e8e7eaa` and verified by isolated
integration onto `0417824`. It has not been deployed or used to send real
messages. The shared checkout and live runtime were not modified. All checks
use synthetic data, injected clocks and fake transports.

## Policy

The `offer_response_window` Policy key overrides these defaults:

```json
{"value":{"max_minutes":120,"min_minutes":2,"lead_time_divisor":6,"cutoff_minutes":10}}
```

At dispatch, let L be the absolute time until the shift. The window is
min(120 minutes, max(2 minutes, L/6)), capped at shift start minus 10 minutes.
No automatic offer is sent if the resulting window is below 2 minutes.
Deadlines round down to the second displayed in the text; this conservatively
removes at most one second and may require coordinator review near the minimum.
The default is a configurable design choice, not a validated optimum.

| Lead time | Response window |
|---|---|
| 24 hours | 120 minutes |
| 6 hours | 60 minutes |
| 2 hours | 20 minutes |
| 1 hour | 10 minutes |
| 30 minutes | 5 minutes |
| 15 minutes | 2.5 minutes |
| 12 minutes | 2 minutes |

## Implemented boundaries

- `app/core/offer_windows.py`: policy, UTC calculations, decision clocks,
  material shift snapshot, immutable deadlines, per-offer states, coordinator
  tasks, and unresolved-delivery holds. Metadata uses existing Notification
  rows; no schema changes or new dependencies.
- `app/agents/fill_agent.py`, `app/llm/tools.py`, `app/jobs.py`: sequential
  offers, expiry/fallback, fresh locked acceptance, STOP and shift-change
  handling. Expiration never changes confirmed assignments or yes/no response
  rates. Existing contact budgets, preferences and consent checks still apply.
- `app/core/send_gate.py`, `app/web/mac_messages.py`,
  `app/integrations/mac_messages.py`: delayed dispatch and native preflight.
  Queued/claimed texts do not start the response timer. The worker uses the
  preflight body and saves its attempt before the native side effect. Uncertain
  outcomes are deduplicated by the existing message/claim/checkpoint IDs and
  held for coordinator reconciliation; there is no blind resend or fallback.
- `app/core/inbound.py`: natural replies, plain-language ambiguity questions,
  persisted cancellation/offer clarification, and role/day hints. General
  YES/NO after an either/or cancellation question cannot accept an invitation
  or cancel a booking. Historical offer ambiguity has no 14-day cutoff; an
  ID-only guard spans prior selected sessions without reading old text bodies.
  New volunteer invitations have no
  public offer code. Old explicit-code replies remain compatible internally.
- `app/web/texty.py`: the dashboard exposes the updated escalation cutoff.
  `prompts/fill_agent.md` is v5, with its change recorded in the changelog.
- New tests are in `tests/test_offer_windows.py` and
  `tests/test_offer_transport.py`. Existing batch tests now exercise sequential
  behavior; receipt replay, exact approval, privacy and consent tests remain.

## Integration and remaining work

The final integration commit is based on `0417824`; cherry-pick that single
commit onto the integration branch and rerun `pytest` and `npm test` in
`web/texty`. Merge the backend and Mac worker together: older workers cannot
acknowledge new offers without preflight. Existing offers lacking deadline
metadata fail closed and need coordinator review. No production migration is
needed.

The bounded repairs are complete: cancellation clarification persists across
requests/held reviews, old replies beyond 14 days cannot silently select a new
offer, and rejected preflight claims leave the worker queue safely so a newly
approved ID can proceed. Rejected texts do not consume contact cooldown or ask
budget. Tests cover 15/90/365-day history, previous-session metadata, general
and explicit clarification replies, unchanged rejected message bodies, refreshed
exact review, and native worker recovery from preflight rejection.

Remaining limitations:

1. Exact-content approval and a dispatch-time absolute deadline can conflict:
   a changed deadline creates fresh review instead of silently changing the
   approved text. Longer approval delays can require repeated review. A product
   decision is needed if conservative fixed approved deadlines are preferred.
2. Unknown delivery creates a coordinator task but has no new automated
   reconciliation UI. The existing Mac journal prevents duplicate attempts.
   Synchronous provider calls still share the original transaction/crash gap;
   a durable outbox is required before claiming end-to-end exactly-once SMS.
3. Concurrency checks exercise independent SQLite transactions and HTTP
   receipt replay. PostgreSQL row-lock behavior has not been tested against a
   real server. Native preflight cannot atomically include the external Apple
   Messages side effect; it is performed immediately before submission.

No scope beyond these bounded repairs was added. Live Noah testing remains
owned by the separate testing task. PostgreSQL locking and crash recovery
require separate verification before claiming production delivery guarantees.

## Verified isolated integration

Base: `04178249df81f60323b4801693bbb1e5a6400223`. Both response-window
commits applied without conflicts, preserving the official branding.

- `python -m pytest --disable-warnings --tb=short`: 451 passed (9.65 seconds).
- `npm test` in `web/texty`: 23 passed, zero failures.
- `git diff --cached --check`: clean.
- All message transports and model calls in the tests are synthetic.
- No deployment, real messages, paid calls, new credentials, production schema
  changes, or shared-runtime changes were performed.

The final patch contains one commit above this base and should replace the
earlier checkpoint patch for integration.
