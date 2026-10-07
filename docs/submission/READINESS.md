# October 6 submission package status

This is the current preparation checkpoint, not a submitted entry. Historical recording instructions remain in `PACKAGE.md`; their old preview, ranking and connection claims must not be reused as current product evidence.

## Prepared

- [Concise Agent Build Document](../AGENT_BUILD.md): about 1,800 words, incorporating Jacob's supplied description, Cornerstone coordinator validation, Planning Center's verified scope and the integrated Clyde algorithm.
- [Edited project description](PROJECT_DESCRIPTION.txt): 250 words based on Jacob's supplied text, with unfinished sentences repaired. The [original supplied text](PROJECT_DESCRIPTION_SOURCE.txt) is preserved separately.
- [Verbatim prompts and audit appendix](PROMPTS_AND_AUDIT.md): current eight prompts, conditional additions, tool schema expressions, earlier parser and independently identified sessions.
- [Sanitized session audit](evidence/session-audit.json): fresh scripted/mock rehearsal and historical real-Gloo/mock rehearsal, clearly separated. Neither proves current cloud replacement delivery.
- [Backend check](evidence/backend-check.json): 2,879 passed, one expected failure, zero errors. Two fixture repairs use real session types; production safeguards are unchanged.
- Frontend: 129 passed at the pinned source. A separate newer frontend fix has passed 134 tests in the live-testing owner's checkout; that is not included in this package's source checkpoint.
- Clyde's replacement implementation is merged in source. Its dedicated synthetic checks are part of the backend suite. Runtime activation and live acceptance remain separate.
- Jacob confirmed that Jacob, Noah and Clyde will attend Boulder. Individual application approval, rule acceptance and eligibility have not been separately confirmed here.

## Checks that remain

| Item | Evidence now | What closes it |
|---|---|---|
| Frozen replay | 13/25; [original result](evidence/frozen-replay.md) retained | Repair stale scripted fixtures and independently review criteria changes, then rerun; do not relabel old failures as passes |
| Connector suite | 122 passed, 14 failed, 7 skipped | Repair stale browser-control doubles and investigate the persisted-payload privacy assertion; rerun the exact suite |
| Human confirmation | Exact review/hash checks pass synthetically; configuration defaults off | Verify enabled mode and an end-to-end communication plus official-record change in the actual deployed build |
| Cloud acceptance | Live owners are deploying a preference-review button repair and testing approved profile publication | Receive their completed audit of interpretation, exact approval, selection, transport state and roster result |
| Dependency audit | Three high findings in the frontend development dependency chain | Review the affected locked Wrangler/Miniflare/sharp tooling and resolve or document the actual exposure; no forced dependency downgrade was applied |
| Current 90-second demo | Earlier video is 84 seconds, showing a disconnected fictional UI walkthrough | Record and inspect the current agent workflow, using synthetic or properly consented data and accurate evidence labels |
| Production transport | Twilio planned | Complete A2P 10DLC registration and separately authorized provisioning; this is future production work |
| Human submission | Attendance confirmed | Confirm each member's approval/acceptance, current rules, asset rights, judge access and upload destination; retain submission receipt |

## Reproduce these checks

Use a fresh checkout with Python 3.11+, the repository requirements, and the locked Node dependencies. Keep transport and scheduling disabled. No real church credentials are needed for these commands.

```sh
DATABASE_URL=sqlite:// AUTOMATION_ENABLED=false SMS_PROVIDER=mock LIVE_SMS=false GLOO_API_KEY= GOOGLE_VOICE_ENABLED=false GOOGLE_VOICE_DEMO_MODE=false MAC_BRIDGE_ENABLED=false PROFILE_SYNC_ENABLED=false python -m pytest -q
cd web/texty
npm ci --ignore-scripts
npm test
cd ../../cloud/voice
npm ci --ignore-scripts
npm test
cd ../..
GLOO_API_KEY= PYTHON_DOTENV_DISABLED=1 python -m evals.run_evals
GLOO_API_KEY= PYTHON_DOTENV_DISABLED=1 python tools/rehearse_fictional_workflow.py
```

The independent replay currently exits unsuccessfully, as documented above. Offline rehearsal uses explicit scripted Gloo. A real-model check needs a private key and a separately named report; transport stays mocked. Seeding must target a fresh disposable database because it drops/recreates the selected database.

Do not package private handoff files, native receipts, credentials, real conversations, databases, browser cookies or `.env`. Supply the existing MIT license and third-party notices with code. The current source checkpoint and separate historical audit provenance must remain visible. No upload, judge access grant, live contact or runtime activation occurred while preparing this package.
