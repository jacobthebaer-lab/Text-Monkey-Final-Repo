# Text Monkey full project audit

**Completed scoped audit and current release acceptance, October 7, 2026.** The [public demo](https://text-monkey-demo.pages.dev/) passed final focused desktop browser QA. Backend/native/publisher/registration run tested application `269dee8f887a763eb8fd72bc806cb830dd88e35e`; frontend/inventory use `a7a8940569a7cb5c3c25683eb80d1831d80d65d0`; the separately reviewed operational PR159 component is `2d5dc0104f0f0dbea48da4072240db873f9f48cd`.

The corrected frontend was published once and verified through direct, immutable and public aliases: connected Mac config, authenticated state, unauthenticated rejection and exact served asset hashes passed. A known verification_pending hold recovered through the same reviewed helper in verification-only mode with zero redeploy. Its exact first failure cause was not persisted, so propagation is not asserted. Authentication/API requests retain NoRedirect; only bounded same-origin asset redirects are accepted. The final supervisor checkpoint is healthy; backend/native/tunnel/registration remained unchanged during the frontend-only correction.

## Test matrix

| Validated source/component | Result | Scope |
| --- | --- | --- |
| Complete backend at `269dee8`, tree `c4772c2` | 4,120 passed, 0 failed, 1 existing expected failure; no warnings | All seven application-source phases completed successfully |
| Current frontend PR160, merge `a7a8940` | 230 full-suite tests; separate 231-check independent run includes one additional attack fixture | These counts overlap and are not summed; backend/evals/Worker stayed at tested `269dee8` |
| Cloud/offline connector at `269dee8` | 140 passed, 7 existing opt-in skips | Offline/fake-adapter proofs; no Google Voice automation |
| Universe at `269dee8` | 32 Python and 45 Node passed; syntax/build passed | Refreshed report/Universe artifact checks are recorded separately, without adding them to this run |
| Operational PR159 | 105 owner, 107 independent and six actual asset checks, separately reported | Component-only verifier/supervisor fix; application/web/evals unchanged from `269dee8` |
| Final frontend-only release helper | 72 independent offline checks; actual single publication/API/assets passed | Protocol and cloud publication proof, separate from application tests and browser QA |

Refreshed Universe artifacts passed 32 Python and 45 Node checks plus syntax/build validation; after the final prose consistency edits, both inventory checks, the 183-record feed/appendix consistency and public privacy checks passed. These checks validate the reporting artifacts, not new application behavior.

The original backend baseline at `1232893` was 3,449 passed, 55 failed and one existing expected failure. The first integrated run at `619251f` was 4,056 passed, 50 failed and one existing expected failure. All 50 were stale contracts/fixtures; reviewed PR156/157 modernization preserved application/evals trees and the 24-call actual-model rehearsal budget. The passing final rerun supersedes those failures without erasing their history in the [bug ledger](BUG_REMEDIATION_REPORT.md). Focused owner/reviewer results overlap and are never added into complete-run totals.

The historic quiet-hours evaluation retains its earlier expectation while the current sender-reply contract permits an immediate reply; proactive quiet-hour guards remain enforced. Seven unchanged cloud skips concern optional browser/session environment proofs. Earlier dependency failure and historical connector results remain documented in the ledger.

All **183 feature records, 15 systems, 10 decision paths, 94 nodes and 347 original mappings** are preserved. The [appendix](feature-universe/AUDIT.md) and [machine-readable evidence](feature-universe/audit.json) identify source, scoped tests, actual acceptance and unexercised reasons. The 13 integration records compile existing receipts. Reporting coverage does not establish implementation of every future idea, every behavior, or every native recipient outcome.

## Actual acceptance and preservation

| Evidence | What passed | Practical limit |
| --- | --- | --- |
| Final public browser | Existing session restored; Home/Settings/Volunteers/protected profile/Shifts passed. Roster and PCO comparison each showed 103 distinct clean names; current shift/notice labels were clean. Eighteen message bodies matched the read-only DB baseline exactly. Eligible-only bulk selection and send-control state passed, then selection was cleared without sending; repeat welcome stayed disabled. Console had zero warnings/errors | Focused desktop; no form submits, live record edits, resets or native actions. Mobile/history pagination unexercised; historical message wording preserved |
| Runtime/preservation | All seven processes, including the supervisor, are healthy; 103 distinct volunteer names, protected 18-message history and 21 claims/native ledger entries preserved. Exact pre-audit native dispatch hash unchanged, zero dispatching/uncertainty and zero new audit texts | Real existing evidence and process health, not new recipient delivery, an actual forced-outage test or an uptime-duration guarantee |
| Auxiliary registration worker | Source `269dee8`, same journal/source identity/baseline, fresh heartbeat and read-only cloud proof of zero new records | Separate process from backend/native/publisher; no broad new-registration write acceptance |
| Protected volunteer readback | Name/phone/consent matched privately across local/cloud/native. All 31 December dates matched exact native generated-date union; October 11, 09:00–10:15 Denver event matched systems | Native GET and read-only databases, zero writes/texts; identity and native IDs omitted publicly |
| Calendar fixture API | 24 scoped checks for import/publication, edits/deletion and output-feed recovery; seven fixtures cleaned, existing data preserved | Not every church source, legacy untracked import or continuous scheduling |
| PCO metadata/Supabase publisher | Bidirectional fictional title/time readback and lost-response recovery without duplicate PATCH. Normal publisher create/two updates/idempotent repeat preserved all 149 existing profiles; scoped role/date mapping passed | No invented consent, qualifications, clearance, assignments or broad target authority |
| Native PCO staffing, PR155 | Same-person U → C → local D → repaired D-to-C; repeat zero writes. Coordinator D became local cancellation plus exactly one FillRequest; repeat deduplicated without echo/Messages | Exact fictional fixture; no second-candidate actual replacement-message delivery |
| PCO blockouts/date boundary | Reviewed preview POST/PATCH/DELETE was manually executed and cache exclusion/move/empty checks passed. Single-day native probe established inclusive local 23:59:59; corrected PR158 requires exact midnight start before normalization. Final zero PlanPeople/blockouts/needed positions; fixture inactive/private/reminders off, zero native texts | DST/multiday are source/fake-API checks. Automatic writer remains deferred for unsupported leader-email suppression/conflict side-effect contract |
| Targeted actual Gloo | Pristine exact disclosure/YES/preferences and one completion reply; two correct 09:00–10:15 Denver offers; all 14 final model calls completed | Isolated database/local recorder, no real/native delivery or consent override |

Gloo model-call latency was mean 4.816 seconds and max 10.187 seconds. Signup endpoint stages took 6.608, 6.545, 10.369 and 20.613 seconds; replacement endpoint 23.7 seconds. Per-call and endpoint measures differ and do not establish production latency. Earlier Gloo defects/corrective retests remain in the ledger.

## Evidence limits and separately governed work

The current audit/report objective is complete. Unexercised mobile/pagination, carrier SMS, dedicated scoped ingestion, new native SMS, extra replacement recipients and forced-outage/due-event scenarios are limits of this evidence, not instructions to resend protected messages. Source, actual APIs, local recorder, native submission and recipient observation remain distinct.

Consent, identity, qualifications, quiet hours, exact record/body review and deadlines remain requirements. Generic pairs need genuine facts/review; split coverage needs the selected-store migration, allowed role and interval/helper review. Full target preference combinations and native partial-time mapping retain their specific acceptance boundaries. Automatic blockout writing remains deferred by the genuine external notification/conflict contract.

Google Voice automation is permanently held by reviewed shipped Python/Node entry points. Future registered Twilio provisioning, human eligibility/licenses/consent, submission and practitioner impact remain separately governed. Public reports omit credentials, private paths, account identities, phones, raw conversations and native resource IDs. No all-features-working, new native-delivery or always-on claim is made.
