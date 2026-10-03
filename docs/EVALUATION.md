# Evaluation evidence

All tests use synthetic data. The workflow eval runner always constructs MockSMSProvider directly: setting a live transport in another environment cannot make these evals send a real message.

- Original local baseline: 152 backend tests passed.
- First complete build: 186 backend tests and 25/25 deterministic fixture replays passed.
- First live Gloo evaluation: 23/25 cases passed. See `evals/reports/20261003-103923-live.md` and the JSON trace. This run predates the merge of the newer shared integration branch.
- Sensitive-cancellation repair: `evals/reports/20261003-104914-live.md` records a real Gloo rerun passing after the strict logistics backstop. The original failing report is retained. No case criteria were changed.

The cases are 25 hand-built workflow scenarios, not a standardized benchmark. Default replay verifies the deterministic workflow with explicitly scripted model fixtures. `--live` verifies real Gloo classification and tool calling, using mocked delivery. Reports distinguish these modes. API errors, missing outputs and criteria failures remain failures.

## Failures and changes

1. **Restricted-role outreach incomplete.** A live model run returned completion without requesting outreach; the original engine reported a progressing fill with no approvals. The newer integrated fill engine requires Gloo to choose from the eligible pool and verifies completed outreach, otherwise escalating. Its live recheck is recorded separately.
2. **Sensitive cancellation lost under guarded refusal.** The guarded response could not be parsed. Care escalated, but the unambiguous cancellation did not proceed. A strict first-person cancellation backstop now preserves the logistical command while keeping the sensitive hold and urgent handoff. Ambiguous, conditional or questioning language still escalates without guessing. Parser prompt version 2 records this change.
3. **Quiet-hours criterion conflicts with a later product policy.** The immutable `quiet_hours` case expects no reply at night. The newer shared integration intentionally sends an immediate acknowledgment to an initiating sender, while holding proactive outreach until morning. Its dedicated unit test verifies that behavior. This case remains a failure in the eval report and an explicitly expected failure in pytest pending human approval to revise its older criterion; it is not counted as a pass.
4. **Offer policy differs between baselines.** The fixture explicitly configures a one-hour offer window to reproduce the frozen 61-minute expiry case. Real code still honors the configured response window and prevents overlapping offers; fixture advancement calls the same job entry point as the app.

Run `python -m evals.run_evals` for fixture replay, or `python -m evals.run_evals --live --workers 4` with GLOO_API_KEY configured privately. Live evaluation creates new reports; it never rewrites expectations. Individual retests use `--case sensitive_self_harm` or another exact case ID. Raw generated logs are ignored; committed traces are synthetic.
