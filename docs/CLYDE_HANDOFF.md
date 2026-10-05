# Clyde handoff

## Current entrypoint, October 4, 2026

Use the shared private repository [jacobthebaer-lab/text-monkey](https://github.com/jacobthebaer-lab/text-monkey), integration branch `codex/complete-text-monkey`. The integrated code and deployed backend checkpoint is [`a9fdfe880b1303a8233e3d088f8570f664f01ae8`](https://github.com/jacobthebaer-lab/text-monkey/commit/a9fdfe880b1303a8233e3d088f8570f664f01ae8), including PRs 8–12.

Clyde (`Clyde-Kertzer`) and Noah (`clementsnc`) have active write access. Set `origin` to `https://github.com/jacobthebaer-lab/text-monkey.git`; new feature branches and pull requests target this repository. The former `clementsnc/planning-center-but-better` repository and retained `main` branch are historical. Preserve integrated history and uncommitted work.

The [public preview](https://text-monkey-demo.pages.dev/) remains a disconnected, synthetic static site. Its source is [`fb2b11be01400b207882be4034f3173f1bf218a0`](https://github.com/jacobthebaer-lab/text-monkey/commit/fb2b11be01400b207882be4034f3173f1bf218a0), with **19 byte-verified UI assets**; the [immutable deployment](https://625ad18a.text-monkey-demo.pages.dev/) matches the alias. PR12 changes backend behavior only, so the public assets were not redeployed for it. Static assets provide no live AI, account backend or delivery authority.

## Implemented behavior and connected proof

- Signup uses Gloo with each person's current response and saved context. Flexible availability and the quiet policy are integrated; required consent, eligibility, recipient scope and zero-em-dash checks remain enforced. Missing Gloo holds a message.
- Booking answers and scheduling notices use the recipient's saved assignments, pending offers, event titles, time and timezone. Gloo cannot add unsupported scheduling facts. Booking-status retries are durable and bounded, remain tied to the original question/session, and recheck current source and recipient bindings before review and delivery. Deleted or changed facts produce a clean hold.
- The [exact approved day-before copy](EXACT_DAY_BEFORE_REMINDER.md) is implemented. Gloo must preserve that wording; a changed body or stale schedule requires fresh review. Implementation completion does not enable background delivery.
- Actual signed Plan webhook delivery now reaches the same Mac database as the backend. A real provider redelivery imported open staffing needs while preserving the existing event and signup data; repeated delivery was deduplicated. This importer does not import people, grant consent, create assignments or send texts.
- The authenticated Planning Center held/no-op comparison is complete under a legitimate coordinator login. The saved native Greeter frequency already matched, producing a no-op; the blockout change stayed held. The audited historical correction is separately bound as local producer evidence, with no vendor-signature or human-review substitution.

Native Planning Center execution remains held. A GET comparison or identical value establishes no write ownership, notification silence or native edit coordination. See [held runtime](PLANNING_CENTER_HELD_RUNTIME.md), [correction lineage](PCO_CORRECTION_LINEAGE.md) and [review receipts](PLANNING_CENTER_REVIEW_RECEIPTS.md) for the source and API guards. Coverage of staffing-only or service-time-only changes is under separate investigation; Plan webhook proof does not establish that coverage.

The dedicated Mac receiver and tunnel remain process-dependent. The Messages reader, general automation, Planning Center writes and polling remain off. Original recipient restrictions and the expired test window are preserved. Google Voice automation remains permanently held; future registered Twilio provisioning requires separate authorization. Historical one-shot native delivery proof does not establish current background uptime or new delivery from PR12.

## Validation and historical evidence

For PR12's reviewed tip `c45ba8a13beebdfa5c080deb279ed83839e6d7f7`, **132 focused initial author checks** and **90 directly affected repair checks** passed. Independent review passed five offer-race/question-isolation checks, seven deletion cases and four native queue binding cases. These are synthetic behavior checks. The merged deployment separately verified unchanged database rows, schema, private configuration and delivery holds; it triggered no live text, Gloo call or native Planning Center mutation. Full historical baselines were not rerun for that release. See [PR12](https://github.com/jacobthebaer-lab/text-monkey/pull/12).

The current static verification and connected runtime/API receipts remain private. Public source contains portable documentation and synthetic fixtures, not private receipt files.

Historical evidence remains available by reference:

- [UI release `7f2b71b`](evidence/cloudflare-release-7f2b71b/README.md): 15 matching UI assets, 635 backend passes with one expected failure, and 37 frontend passes. Those counts and editor/mobile checks describe that older checkpoint.
- Historical backend checkpoint `c4e2446`: 1,605 backend passes with the documented quiet-hours expected failure, and 63 frontend passes. This predates PRs 8–12; it is not a fresh full-suite result for the current runtime. Detailed receipts remain private.
- [Acceptance review](DEMO_ACCEPTANCE_REVIEW.md): the later Gloo-outage recovery and composition fixes, with nine acceptance checks passing. The historical quiet-hours expected failure is separate.
- [Historical fictional signup](evidence/synthetic-signup.json): an earlier real-Gloo check with mock delivery and zero real texts.
- The earlier 25-case real-Gloo run passed 23/25; sensitive cancellation and restricted-role approval each passed a subsequent targeted 1/1 retest. These results do not establish a fresh 25/25 live-model run or new real delivery.

## Start locally and integrate Clyde's work

Follow the [README](../README.md) for Python and isolated SQLite setup. The credential-free static preview is `python3 tools/texty_local_demo.py --port 58123`, then `/texty`. It uses fictional data and has no real account, Gloo or delivery. Never seed or reset a connected database. Backend and frontend test commands remain in the README; live-model evaluation requires private configuration and separate scope.

Recipient ranking, selection, tranche behavior and cadence are unchanged while we await Clyde's scoring update. For integration, Clyde's handoff should identify the actual commit/branch and source/API call sites, the intended score inputs, the output shape and ordering, and how ties are handled. Include the focused test evidence. These are integration details for Clyde's implementation, not a new scoring algorithm or specification.

## Privacy and history

Never commit credentials, tokens, real phones, conversations, databases, logs, native row identifiers, private filesystem paths or delivery receipts. Detailed originals and current connected evidence stay outside Git.

Historical commit `96e2894` contains earlier personal test identity and native receipt metadata in `DEMO_COORDINATION.md`. Keep the repository private, preserve existing proof history by reference and do not copy that material into new docs or fixtures. A history rewrite requires Jacob's authorization. This handoff does not change the user-owned coordination document.
