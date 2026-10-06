# Private event acceptance candidate

This source adds a separate **Private event test** panel to Schedule. It is
hidden when `TEXT_MONKEY_ACCEPTANCE_SCOPE_FILE` is absent. No scope, database
records, reviewed texts, timer or live transport is enabled by installing it.
It does not change the repository's provider policy or authorize live sends.
Independent review and the runtime owner's existing authorization are required
before any activation. Keep `AUTOMATION_ENABLED=false`; never use `process_jobs`
or `reminders.process` for this test.

## Private runtime configuration

The optional `deploy/cloud/compose.acceptance.yaml` overlay mounts a JSON file
read-only into the backend. Supply `ACCEPTANCE_SCOPE_FILE` privately, outside
Git and public frontend assets. The file must be a regular file, not a symlink,
owned by the backend process UID (1000 in the current container), mode 0600.
It contains exactly these fields; populate actual values only in private runtime:

```json
{
  "phone": "+15555550101",
  "admin_email": "owner@example.test",
  "sender_email": "owner@example.test",
  "sender_number": "+15555550202",
  "timer_available": false
}
```

Numbers above are synthetic. Sender identity must match the existing private
Google Voice settings; the logged-in account must match `admin_email` and the
verified superadmin allowlist. The restored provider scope must contain exactly
this participant. An additional registered participant holds this path rather
than widening it. The scoped workflow persists in the private database; changing
sender, participant or admin identity selects a different workflow. Flipping
`timer_available` alone does not discard the saved workflow.

Backend deployment requires the new Python modules and app wiring. Frontend
publication requires `acceptance-workflow.js` and the updated `app.js`. Existing
publisher/profile configuration is untouched. The overlay requires no public
connector port and changes no signup or transport switches.

## Short UI sequence for the runtime owner

1. Finish signup with the sender's self-reported first and last name and approved preferences through the
   existing signup path. Verify the Supabase result separately. This path does
   not publish profiles or grant qualifications.
2. In **Schedule → Private event test**, choose an existing role and a clearly
   fictional `Demo: ...` event. Dates are interpreted in the displayed church
   timezone, independent of the Chrome computer's timezone. Skipped/repeated DST
   times are rejected. Choose tomorrow for a genuine day-before reminder test.
3. Review and approve the exact event, then its one role slot. Click **Review
   participant assignment**, then approve that exact record. The existing
   eligibility, occupancy, availability, qualifications, global frequency and
   role frequency checks run before proposal and again before approval. A stated
   one-off availability must satisfy the existing rules; this path never overrides
   recurring windows, Sunday preferences, exclusions or a clearance requirement.
4. Click **Prepare due Gloo reminder** during the real local day before the
   event, outside quiet hours. The existing literal day-before wording is sent
   through Gloo. Review that exact body and approve it to queue one record.
   Gloo failure holds preparation; no substitute is used. Repeated preparation
   does not duplicate the reminder.
5. **Submit this exact reminder** performs a fresh inbox check before one exact
   queued ID/body-hash submission. STOP, changed/cancelled assignment, stale
   review, quiet hours or any unknown Google submission holds delivery. This
   is not generic manual composition or queue draining.
6. To prove background timing instead of clicking Submit, only after independent
   review permit the private file's `timer_available=true`. In the panel choose
   a real due time about one minute ahead and click **Arm this reminder only**.
   The exact event, assignment, message ID and body hash are persisted. Due time
   must precede both review expiry and the queued-message age cutoff (15 minutes
   by default, with 30 seconds reserved for the inbox check). Close the admin
   page/local backend and verify the cloud job and actual phone receipt. The
   15-second cloud tick uses `RealClock`, performs fresh intake and existing native
   preflight, attempts only this message and records its outcome. No clock
   fast-forward, automatic approval, unrelated reminder or admin digest runs.
7. **Stop this reminder job** disables it. A successful or held attempt disarms
   it. Restart restores a still-armed job, but submitted/dispatching/uncertain
   records are never repeated. Silence leaves the approved assignment in place.
   Fresh cancellation/STOP goes through existing inbound routing and makes its
   reminder ineligible. No replacement ranking algorithm is introduced here;
   any existing replacement proposal remains subject to its own review and scope.

A queued/submitted record is not proof of phone delivery or persistent Google
sign-in. A disconnected laptop backend is not proof that the laptop was powered
off. Collect each real-world receipt privately and report those limits honestly.

## Verification

`tests/test_acceptance_workflow.py` uses only synthetic phones, a fake connector,
a fake Gloo client and temporary SQLite/scope files. It checks exact record/text
review, eligibility and changed-source guards, default-off timer, real-clock
requirement, durable arm/restore, due gating, one submission, STOP, quiet hours,
cancellation, expired review and uncertain outcome. Its deterministic subclass
of `RealClock` is explicitly a test fixture, not a live timing demonstration.
Frontend tests exercise timezone/DST conversion and exact ID/hash requests.
