# Evaluation evidence

The workflow runner always constructs an isolated in-memory SQLite database and `MockSMSProvider` directly. Live transport settings cannot make these evaluations send a real text. `--live` uses real Gloo classification and tool calling with mocked delivery; default replay uses explicitly scripted model responses and is not a model benchmark.

## Current 25-case contract

The current corpus retains all 25 original journey IDs. Its complete replay passes 25/25 against integration `60fb70cfbfb2cf259b43a6b4d415eb1706cd118b`, including actual mock-delivered invitation evidence before any replacement reply. The report is `evals/reports/20261007-130641-740598-replay.md`. This is a fresh complete fixture replay, not a fresh complete real-Gloo result.

The runner now uses current valid synthetic inputs and normal workflow entry points: a frequency cap of eight, explicit outreach enablement confined to the mock database, church-local invitation facts, due notification drainage and a disclosed prior-consent fixture for START. It does not inject a delivered replacement invitation, change application gates or manufacture native proof. A scripted acceptance, decline or partial reply fails the evaluation unless the exact person actually received a matching mock-provider invitation through the workflow.

The current expectations differ explicitly from the historical contract:

| Journey | Current assertion and reason |
|---|---|
| Ambiguous shift | Both bookings remain approved; a source-bound AI clarification is sent and the cancellation review stays unresolved. Its current purpose is `signup_reply`. |
| Bare number | `2` cannot choose a booking. Both bookings remain approved and each input gets its source-bound acknowledgment. |
| Unknown sender | No roster record and no unsolicited outbound text are created. |
| Quiet hours | An initiating sender receives one immediate acknowledgment at 23:00. Proactive replacement outreach remains absent through 06:29 and starts at the 06:30 urgent quiet-window boundary. This follows the user's current immediate-acknowledgment requirement. |
| START | Restoration requires recorded earlier disclosure and name reply. A separate negative regression verifies that an imported flag alone cannot grant consent. |
| Expired after ask | The qualification expires after a real mock-delivered invitation, then YES still cannot assign the person. This makes the eligibility recheck non-vacuous. |

Restricted-role approval is checked before outreach. The runner preserves the one-hour response-window fixture for the 61-minute expiry journey and still applies the application's hard-rule validation after assignments. The direct pytest suite runs all 25 journeys without a quarantine or expected failure; negative regressions cover missing invitation evidence, missing prior consent and unknown criteria.

Run `python -m evals.run_evals` for the current complete replay. `--case ID` is a targeted retest only. `--corpus frozen` retains the original expectations, including superseded ones, and reports their failures honestly. Reports use unique microsecond names and preserve earlier reports.

## Historical evidence, unchanged

- Original local baseline: 152 backend tests passed.
- First complete build: 186 backend tests and 25/25 original deterministic fixture replays passed.
- First real-Gloo evaluation: 23/25 passed in `evals/reports/20261003-103923-live.md` and its JSON trace. It predates the newer shared integration.
- Sensitive cancellation: `evals/reports/20261003-104914-live.md` records an individual real-Gloo rerun after the strict logistics backstop. The original failed report remains intact.
- Restricted role: the individual `kids_approval_hold` real-Gloo recheck `20261003-105228-live` passed 1/1. These two individual checks are not a fresh passing run of all 25 cases.
- Replaying the unmodified runner and original criteria on integration `d0570c62426ea6b3dc6b0c9d96ec95a8c33d49c9` produced 12/25. Its original inputs included an invalid frequency cap and omitted current transport-policy, invitation-copy and deferred-notification fixture setup. The retained earlier 13/25 checkpoint concerns its own source, not this later replay.

The original criterion bytes are preserved as `evals/cases/workflows-v1-frozen.yaml` (SHA-256 `59447bad5a9b1d9c4465a1047d465bd937c02bd03649336a4ece98c774a0e3dd`). Historical criteria and failed reports are not retroactively relabeled as passing.

## Real-Gloo evaluation

After source and corpus review, run `python -m evals.run_evals --live --workers 4 --env-file PRIVATE_PATH` with authorized Gloo credentials. API errors, missing outputs, missing delivered-offer evidence and criterion failures remain failures. A complete result must include all 25 cases; neither individual retests nor a subset can establish 25/25. This command uses mock delivery and cannot prove native Messages, recipient observation or production database behavior.
