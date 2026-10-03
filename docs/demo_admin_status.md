# Three-hour admin-status replay

Run from the repository root with its Python environment and installed requirements:

```sh
python tools/demo_admin_status.py --output /tmp/admin-status-synthetic.json
```

This is an **offline, scripted synthetic demonstration**, not a real Gloo call or a delivered text. It uses the actual `app.jobs.process_due_fill_requests` job, `FakeClock`, fresh in-memory SQLite databases, and `MockSMSProvider`. It starts no web server, worker, or background scheduler. All names, events and reserved 202-555-01xx numbers are fictional. It never opens the live signup database, laptop Messages, or Noah's conversation.

To explicitly compose the same fictional messages through Gloo, keeping delivery mocked:

```sh
python tools/demo_admin_status.py --real-gloo --output /tmp/admin-status-real-gloo.json
# Or select a private credential file explicitly:
python tools/demo_admin_status.py --real-gloo --env-file /absolute/path/to/private.env --output /tmp/admin-status-real-gloo.json
```

Real mode needs `GLOO_API_KEY` in the environment or selected file. Optional `GLOO_ENDPOINT` and `PARSER_MODEL` select the existing Gloo endpoint/model. Credentials are never written to the evidence. Automatic dotenv loading is disabled; even if the credential file contains live transport/database settings, this tool uses only its Gloo fields. No other AI provider or template fallback is available in real mode. A missing key or failed scenario exits unsuccessfully.

## Presenter steps

1. Run the offline command. Expect `PASS: 5 scenarios` and `MOCK delivery only`.
2. Open the JSON evidence and read each scenario's `mock_delivery` text alongside its fake-clock `timeline`.
3. Explain the five outcomes below. For a live AI demonstration, run the explicit Gloo command and show `composition_mode: real_gloo` and `composition_usage.calls`.
4. Show the repeated job tick using a fresh application session: its mock text count stays unchanged.

| Scenario | What the replay verifies | Admin work |
| --- | --- | --- |
| All set | No update at 3h 1m; one update at exactly 3h, with required coverage | No action needed |
| Replacement underway | Exact gap and one already-running replacement search | No action needed while search continues |
| Approval and help | Three gaps, one running search, a restricted-role approval code, and one escalated search | Reply YES A1 or NO for the pending batch; review the escalated search |
| Quiet hours | 06:00 update held; 06:59 still held; 07:00 update uses staffing saved during the wait | No action needed after the spot is covered |
| Synthetic outage | Three failed composition attempts, two-minute retry spacing, no text, internal escalation | Review the internal system error |

The outage is deliberately **scripted in both modes**. It is not evidence of a real Gloo outage. The other four scenarios use real Gloo when `--real-gloo` is supplied. The quiet-hours scenario makes three composition attempts, so a successful real run normally reports six Gloo responses and four mock texts.

## Evidence and limits

Checked-in receipts are `docs/evidence/admin-status-synthetic.json` and `docs/evidence/admin-status-real-gloo.json`. Each includes the application job, transport/composition labels, exact outbound copy, database message states, staffing snapshots, notification states, and fake-time steps. Application `sent` and `MOCK...` IDs mean **mock acceptance only**, never native iMessage delivery.

The replay seeds existing search/approval states; it does not demonstrate candidate selection, accepting an approval, or an end-to-end cancellation conversation. Those transitions belong to the separate fill/signup demos. The dedup check recreates the session/context against committed records; it does not restart a separate OS process. Fixtures are committed and reloaded through the UTC database before each job tick, matching a scheduler reading saved admin-console records.

Quiet hours can make the update later than three hours before the event. This replay uses the saved default quiet-hour policy (ordinary texts open at 07:00; urgent texts at 06:30). The initial staffing gap schedules a 06:30 retry. After that gap is covered, the next tick at 06:59 recomposes current facts and defers the now ordinary notification until 07:00. The 06:29 tick sends nothing before the initial retry. No assertion here enables or verifies a production scheduler or laptop connection.

Regression check:

```sh
python -m pytest -q tests/test_pre_event_updates.py tests/test_send_gate.py
```

Run with ordinary Python (without `-O`), so the replay's scenario assertions remain enabled.

## Integration receipt

Implemented against baseline `9ed9d71203ed86989455e81a6fca2717a8017682`. Verification on October 3, 2026: all five replay scenarios passed in synthetic and real-Gloo modes; real mode recorded six successful Gloo responses and four mocked admin texts. A second offline run produced byte-identical evidence. The 28 notification/send-gate regression tests passed. Missing real-Gloo credentials correctly exited with code 2. No application core, UI, PCO, live database or scheduler configuration changed.

This receipt verifies the pre-event path only. Other status-message paths require separate acceptance for Jacob's all-messages-through-Gloo requirement. The repeatable CLI for the central demo runbook is `python tools/demo_admin_status.py --output /tmp/admin-status-synthetic.json`; real Gloo remains an explicit opt-in.
