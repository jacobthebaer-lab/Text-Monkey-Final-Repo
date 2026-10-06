# Text profile persistence

The Messages backend may retain its private SQLite store while mirroring sender-authorized profiles to the intended Supabase project's private `texty` schema. Enable capture only for the currently approved test recipient; Noah's texting is off, so initial activation is Clyde-only. Existing Noah and cloud records are preserved.

Private runtime configuration uses `PROFILE_SYNC_ENABLED=true`, exact `PROFILE_SYNC_PHONES`, `PROFILE_SYNC_PROJECT_REF`, the existing private `PROFILE_SYNC_DATABASE_URL`, and optional JSON `PROFILE_SYNC_ROLE_MAP` from local role names to verified cloud names. Missing or ambiguous cloud roles hold the complete update. The approved case-only map is Greeter → greeter, Usher → usher, Coffee → coffee. Production and Child Care remain unresolved; no cloud role or qualification is invented. Local IDs are never sent as cloud IDs.

An accepted `/mac/inbound` commits the business changes, receipt and additive local `profile_sync_outbox` together. Capture is disabled by default. The cloud publisher is separate: no Gloo calls, Messages sends, scheduler jobs or runtime database switch occur. Cloud failure leaves a durable failed row for retry. An obsolete queued snapshot is held when the current source profile has changed; its unfinished sections are carried into the newest captured revision so older answers cannot overwrite corrected facts. The standalone `--watch` drain retries on its own interval while general automation remains disabled; it rereads the private recipient scope each pass, so removed recipients stop publishing. `/api/profile-sync` requires the existing verified administrator login and shows pending/failed/held/synced state without conversation text or connection credentials.

Only name, phone, SMS consent/status, approved preferences and validated availability are mirrored. Recurring role/time windows retain their exact restrictions and translate role/event references to cloud IDs through verified stable names. Unknown roles, event context or time restrictions hold complete publication. Validated incomplete availability drafts remain in the local payload with explicit pending status; raw availability replies/notes are not copied. Cloud volunteer identity matches the exact normalized E.164 phone. Existing qualifications, coordinator/pastor flags, administrator authentication and other preference keys remain intact. Existing cloud opt-outs/suppressions and changed baselines are held for review; only an unchanged pending signup created by this publisher may progress automatically. A cloud receipt marker in the volunteer's preferences is committed with the allowed changes, making retries safe after a cloud commit followed by a local acknowledgment failure. No remote table or migration is required.

The validated catalog-derived `preferred_ministry` is a bounded text preference
and is included in full publication. Identity-only publication retains it locally
pending. Neither ministry nor role interest grants a qualification or placement.
Preference deltas distinguish an absent key from an explicitly supplied null:
adding `max_per_month: null` must reach the mirror rather than inherit an unrelated
global limit when the sender supplied only role-specific caps.

For a transition from identity-only to full publication, preserve the same source
database, source UUID, recipient/project scope and existing cloud identity. Verify
the latest accepted source revision and exact cloud role names first. An omitted
role mapping uses the exact original name; a differently named role needs an
explicit reviewed mapping. Run the existing publisher without `--identity-only`;
no catch-up, text replay, new volunteer seed or source reset is required when the
latest correction was captured transactionally. Older snapshots may become held
with `newer_local_profile`; the latest captured revision carries their unfinished
sections. Use `--retry-held` only after the specific held revision/mapping was
reviewed. Full `synced` means the saved validated snapshot was mirrored, not that
signup or scheduling is complete: the original onboarding stage and eligibility
holds remain authoritative. An incomplete availability draft or unresolved
role/time/context cannot become a fully published schedule.

For the current frozen runtime, initialize only the additive local queue when the live owner authorizes it. Use a private JSON scope file with `phones`, `project_ref` and optional `role_map`; keep it and target credentials ignored and outside public assets. Every invocation and watch pass checks this scope; removing a recipient prevents both publishing and catch-up for that recipient.

```sh
python tools/publish_profiles.py --source-db "$PRIVATE_SQLITE_URL" --scope-file "$PRIVATE_SCOPE_FILE"
# Explicit additive local initialization, if needed:
python tools/publish_profiles.py --source-db "$PRIVATE_SQLITE_URL" --scope-file "$PRIVATE_SCOPE_FILE" --initialize-local
# Catch up from an existing legitimate profile receipt without replaying/sending a text:
python tools/publish_profiles.py --source-db "$PRIVATE_SQLITE_URL" --scope-file "$PRIVATE_SCOPE_FILE" --catch-up-guid "$ACCEPTED_PROFILE_GUID" --phone "$APPROVED_PHONE"
# First identity/consent publication; retains unresolved preferences pending:
python tools/publish_profiles.py --source-db "$PRIVATE_SQLITE_URL" --scope-file "$PRIVATE_SCOPE_FILE" --target-env-file "$PRIVATE_TARGET_ENV_FILE" --publish --identity-only --limit 1
# One complete pending row; target env file is the already saved correct-project configuration:
python tools/publish_profiles.py --source-db "$PRIVATE_SQLITE_URL" --scope-file "$PRIVATE_SCOPE_FILE" --target-env-file "$PRIVATE_TARGET_ENV_FILE" --publish --limit 1
# Independent Clyde-only identity drain; run as its own private process:
python tools/publish_profiles.py --source-db "$PRIVATE_SQLITE_URL" --scope-file "$PRIVATE_SCOPE_FILE" --target-env-file "$PRIVATE_TARGET_ENV_FILE" --publish --identity-only --watch --interval 10 --limit 1
# After role mappings/partial source facts are reviewed, explicit held-row retry:
python tools/publish_profiles.py --source-db "$PRIVATE_SQLITE_URL" --scope-file "$PRIVATE_SCOPE_FILE" --target-env-file "$PRIVATE_TARGET_ENV_FILE" --publish --retry-held --limit 1
```

Identity-only creates a cloud-allocated volunteer ID with name, normalized phone, recorded SMS consent and `signup_source`/`consent_pending`/`consent_at`/`consent_source`, plus the private idempotency marker. A newly created partial profile stays `inactive` for staffing until its full preferences are verified. Existing cloud eligibility is preserved except an actual opt-out may deactivate it. The queue remains `pending` with detail `identity_synced_preferences_pending`, never fully `synced`. Watch skips already published identity revisions so later incoming changes can drain. It cannot run Gloo, Messages or other scheduler jobs. Status reflects queue receipts, not a claim that a worker is alive.

Catch-up checks the actual Mac receipt, its accepted profile route and matching original inbound fingerprint. It requires an SMS-origin profile whose name appeared in that sender's history. It does not invent an inbound message or copy numeric IDs. Keys include the source UUID, original receipt GUID and validated snapshot digest: a legitimate corrected extraction from the same original receipt is captured as a new revision without fabricating a text. A correction owner should call `capture` with the original GUID, accepted route, pre-correction `safe_snapshot`, and correction timestamp in the same transaction as saving the validated correction, then commit. If capture was disabled, run catch-up after the sole runtime owner verifies that corrected source revision. `onboarding_review` can provide provenance only when the saved validated profile actually changed. Review the private catch-up profile and cloud role mappings before approving the first publication. Do not seed/reset either database. Do not enable another recipient, restart a runtime or perform a real cloud write merely by following this document.

First cloud verification should publish exactly one approved profile, query the same cloud volunteer/availability, confirm preserved privilege flags and qualifiers, and confirm a retry creates no second volunteer or availability row. Preserve the local queue and existing SQLite records. Cloud metadata/permissions were checked separately; they are not proof this publisher is active or that a new write has succeeded.


## Event-relative windows and role caps

Requires the shared contract released as `d3193356b1c3f9902e1c97cf2331e7ae07495076`
(canonical integration `c7685ea3bfd2962b1cf0e2ec5dac7b48084fc077`).
Capture uses the shared window/cap validators rather than interpreting sender text.
Optional `time_mode=event` retains a named role, weekday and mapped group with
null hours and `all_day=false`; a resolved event window follows that exact group's
actual interval. Omitted mode remains the existing clock rule, so unspecified
clock hours still hold. Neither mode turns Wednesday availability into Thursday
availability or supplies a synthetic event.

`role_frequency_caps` retain exact validated names and individual limits. Queue
payloads replace source role/type IDs with stable names; cloud publication resolves
its own IDs, including each cap's exact cloud role name. Ambiguous role translations
or unknown group mappings hold the entire full-profile transaction. A Greeter cap
of two per month does not create a Coffee cap or global limit: absent and explicit
null `max_per_month` remain distinct, with no default synthesized by the mirror.
All explicit date exclusions, including a full December month, retain their dates.
Incomplete drafts preserve both additions locally and remain preferences pending
after identity publication. A valid correction can capture a fresh revision of the
original receipt even if the prior serializer/source revision was held; it never
inserts a fictitious inbound message or grants qualifications.

## Calendar patterns and paired roles

`calendar_patterns` uses the canonical `planning_patterns.normalize_patterns`
contract: weekday ordinals and explicitly stated annual unavailable months.
Capture and full publication both validate this structured value. Existing dated
December exclusions remain one-time dates; the mirror never infers annual absence
from dates, text or serving history. Empty pattern lists explicitly clear the
restriction; null or malformed patterns hold publication.

`same_day_role_pairs` uses `paired_planning.normalize`: at most four disjoint pairs
of two existing roles. Nonempty source pairs also require that module's current
exact coordinator approval and role-source receipt. Queue snapshots retain stable
role names, then full publication resolves the intended cloud roles and normalizes
the pairs again using cloud IDs. Missing mappings, merged aliases, malformed rules
or changed/revoked source approval hold the complete transaction. Source role and
approval IDs are never copied into cloud authority. The cloud planner still needs
its own applicable approval; mirroring a preference grants no qualification,
assignment or serving permission.

Explicit empty pairs and removal of either preference retain their distinct
meaning, using the existing publisher baseline before clearing cloud values.
Absent and explicit null monthly limits remain distinct. Identity-only publication
omits both planning fields. These additions depend on the canonical calendar and
paired-planning modules being integrated together; they do not change publisher
activation or create a cloud schema.
