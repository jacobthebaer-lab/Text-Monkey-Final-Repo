# Signup failure classification at e0ceb11

Bounded audit only; no full-suite rerun or legacy expectation mass edits.
Representative reproduction: 12 failures and 3 passing natural-reader checks.
Updated-policy acceptance then passed 12 targeted checks with mock-only transport.

- Exact/concise four-message tests fail after successful complete preference save
  because they demand the removed completion text. Initial name, interests and
  availability prompts and natural full-name variants work. Three outgoing
  compositions remain, with no fourth composition or assignment/qualification.
- Independent HTTP provenance fails only at the fourth native pull, where the
  removed completion message is expected. A new policy acceptance check verifies
  all four real inbound-route receipts, duplicate retries, altered-body conflicts,
  three submitted mock native messages and no completion queue.
- Adaptive off-topic/failure/style cases run after their field was already asked.
  Central dedup suppresses the repeat BEFORE recovery composition, so old expected
  redirect, Gloo failure or invalid-style branches are not reached.
- Frequency-first answers retain a partial draft; the already-asked availability
  field is suppressed. A later natural day answer still completes silently.
  A fresh genuinely missing days field still composes its question once, without
  an acknowledgment, and retains the sender's monthly frequency.
- Partial-name questions share the central `name` dedup bucket with the welcome.
  Thus a last-name follow-up after an earlier full-name prompt is suppressed.
  The supplied first name remains stored and a later real last-name reply still
  establishes the original consent provenance and advances. This is the shared
  policy's coarse field granularity, not a caller exception or fabricated name.
  If a one-time narrower essential question is desired, the central owner must
  refine that source-bound policy; callers must not bypass its dedup keys.
- Editor/account rebinding tests expect a second composition after restarting
  the same recipient's question. The dedup now prevents that composition; HTTP
  start returns 409. Account-scoped saves, unbound copy and first explicit account
  bindings pass. No cross-account copy was observed or new text sent on restart.
- Onboarding-copy completion checks inspect approved_message after silent save;
  the final model call is now parsing, not outgoing composition. Legacy YES tests
  expecting a Gloo failure for the former completion text similarly no longer
  exercise an outgoing call.

The actual-input reparse failure is separate from these old delivery assertions:
the real model omitted event mode and retained a stale global draft value.
Onboarding v9 and the corrected appended window schema address the conflicting
exact-fields example. The saved-context source is now supplied truthfully.
The live owner must prepare recorded pre-input context before a single audited
corrective interpretation. No model output is coerced by this release.
