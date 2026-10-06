# Replay workflow evaluation

13/25 passed. All data synthetic; all delivery uses MockSMSProvider.

Deterministic fixture replay; not a model benchmark. Token totals: {'input_tokens': 0, 'output_tokens': 0, 'calls': 0}.

| Case | Result | Failure |
|---|---|---|
| cancel_fill | FAIL | state: expected filled, got escalated; filled: expected 1, got 0 |
| cancel_decline | FAIL | state: expected in_progress, got escalated; responses: expected ['no'], got [] |
| cancel_next_tranche | FAIL | state: expected in_progress, got escalated; tranche: expected 2, got 0 |
| ambiguous_shift | FAIL | purpose: expected clarify_shift, got None |
| ambiguous_number | FAIL | cancelled: expected 1, got 0; state: expected in_progress, got None |
| kids_approval_hold | FAIL | state: expected waiting_approval, got escalated; pending_approval: expected True, got False |
| kids_approval_yes | FAIL | state: expected filled, got escalated; filled: expected 1, got 0 |
| partial_offer | FAIL | responses: expected ['partial'], got [] |
| two_yeses | FAIL | filled: expected 1, got 0; state: expected filled, got escalated |
| expired_after_ask | PASS |  |
| no_candidates | PASS |  |
| sensitive_hospital | PASS |  |
| sensitive_loss | PASS |  |
| sensitive_self_harm | PASS |  |
| stop | FAIL | stop_confirms: expected 1, got 0 |
| unknown | FAIL | purpose: expected unknown_number, got None |
| self_report | PASS |  |
| availability_ordinals | PASS |  |
| availability_none | PASS |  |
| availability_pattern | PASS |  |
| quiet_hours | PASS |  |
| gloo_failure | PASS |  |
| start | FAIL | opt_in: expected True, got False; purpose: expected start_confirm, got None |
| confirmation | PASS |  |
| unknown_event | PASS |  |
