# Coordinator onboarding and safe contact staging MVP

The invited admin signs up with the existing Supabase confirmation flow. After
verified sign-in, unfinished accounts land in **Church setup**. Three short
steps collect church name, affiliation, street address, country, timezone,
coordinator name/role, optional attendance/services/ministries/website/office
phone, and preferred quiet hours/monthly invitation limit. **Save draft** and
**Save & continue** persist the draft; the final save opens a guided checklist.
Settings and Overview provide a return path to setup and contact import.

## Scope and activation

This is an account-owned setup workspace and a separate contact staging area.
It does **not** add multi-church scheduling tenancy to the existing single-church
roster. Scheduling screens explicitly identify that existing church roster.
Preferences are saved for coordinator review; they do not change live policy,
transport, background work, qualifications or delivery. The checklist explains
line verification, volunteer JOIN/name/YES signup and schedule review.

All setup APIs reuse the existing Supabase `/auth/v1/user` verification,
confirmed-email requirement, administrator allowlist and backend bridge. Owner
UUIDs come only from the verified user response, never request parameters or
editable auth metadata. Every workspace/contact/batch query is owner-scoped.
Admins sharing the existing roster do not share their setup/staging workspace.

The additive migration is
`supabase/migrations/20261002215700_admin_setup_workspace.sql`. **It has not been
applied.** It defines three tables in the private `texty` schema, enables RLS,
revokes browser/public access, and grants the existing private backend role
CRUD through backend-only policies. Account isolation is enforced in backend
queries, since the backend database login does not carry the browser JWT.
No anonymous/authenticated client may access these tables directly.

Setup models use separate SQLAlchemy metadata. Isolated SQLite apps initialize
that metadata; Postgres app startup does **not** create these tables. The church
owner must review/apply the migration through their established management path
before activating this feature. A missing migration returns a clear 503 with a
retry option; existing login and scheduling remain available. No production
migration, permission change, credential creation or deployment was performed.

## Contact import

1. Save a setup draft, then select the file explicitly.
2. CSV supports UTF-8/BOM, quoted fields/newlines and comma/semicolon/tab delimiters.
   XLSX supports workbook/worksheet selection, shared/inline strings, numeric
   phones and sparse optional cells. Python's standard library handles ZIP/XML;
   no dependencies were added. Older XLS files must be exported as CSV/XLSX.
   Formula/error cells require plain text values; macros/external links never run.
3. Map full name or first/last name, phone, optional email and ministry. Record
   the list's source separately. Imported consent columns are ignored.
4. Preview the normalized values and all row outcomes. Preview and staged-list
   tables paginate every 100 records. Download the skipped-row report to fix
   invalid rows/duplicates and re-import. Report row numbers refer to parsed
   nonempty records, including the header, rather than physical Excel/CSV lines.
5. Save only valid new rows explicitly. The server recomputes the preview against
   current staged phones and requires the preview hash; changed previews return
   409. Submission UUIDs make repeat saves idempotent. Database uniqueness and
   workspace locking protect concurrent imports. Existing contacts are preserved.
6. Staged contacts always show **Awaiting consent / cannot text**. Imports create
   no `Volunteer`, `Assignment`, `Outreach`, `Approval` or `Message` records and
   call no SMS/Gloo provider. There is no promotion/send endpoint in this MVP.
   Remove mistakenly staged records using their account-scoped Remove action.

Phone normalization handles US/Canada local formatting and explicit international
`+country code`; it rejects ambiguous local numbers, extensions and malformed
numbers. This verifies formatting only, not ownership, mobile type or delivery.
File limits: 5 MB, 2,000 contact rows, 80 columns, 500 characters per cell;
XLSX expanded contents are bounded at 20 MB/400 entries. Each workspace supports
up to 10,000 staged contacts. Raw uploads are not retained after parsing.

## Phone contacts and privacy

The UI checks whether this browser exposes the Contacts Picker API, but does not
request address-book access. Its supported phone path is an explicitly selected
vCard (.vcf) or CSV export. iPhone: share a chosen contact or export a chosen
Contacts list to Files. Android/Google Contacts: export selected contacts to
vCard/CSV. Device/browser support varies; no universal phone access is promised.
The vCard reader supports UTF-8 folded lines and multiple exported numbers per
contact; encoded legacy fields require a UTF-8 re-export. Review multiple numbers
carefully. Actual personal contacts were never selected or accessed in this task.

The synthetic preview keeps only synthetic CSV data in browser-local storage;
XLSX/vCard parsing requires verified sign-in. Live setup/staged contacts are
server-backed and are never copied to demo localStorage. Entering the synthetic
preview reloads its separate synthetic roster instead of retaining live state.

## Verification and evidence

On October 2, 2026, in the isolated onboarding worktree:

- `DATABASE_URL=sqlite:// .venv/bin/pytest -o addopts='' -q`: **366 passed**.
- `npm test --prefix web/texty`: **19 passed, 0 failed**.
- `node --check` for app/setup modules and `git diff --check`: passed.
- 31 new backend cases cover draft/resume, validation, stale revision/hash,
  two-owner isolation, idempotency, no outreach/consent grant, CSV/XLSX/vCard,
  formula/XML rejection, sparse Excel rows and missing migration behavior.
- Frontend tests exercise the production setup controller with synthetic APIs,
  parsing/mapping/deduplication, formula-safe row reports and existing approvals.
- Manual browser: completed all three setup steps, saved checklist, previewed
  four synthetic rows (2 ready, 1 duplicate, 1 invalid), staged two contacts,
  then reloaded and verified church details and staged contacts resumed.
- Integration browser fixture (loopback-only, synthetic API responses): callback
  entered unfinished onboarding; reload restored it; completing setup followed by
  reload opened the existing roster Overview; server 401 returned to sign-in and
  the following reload stayed signed out. This does not verify real Supabase
  confirmation or the live Chrome runtime issue.
- Screenshots: `docs/evidence/admin-setup/church-details.jpg`, `checklist.jpg`,
  `import-preview.jpg`, `staged-contacts.jpg`, `session-resumed-onboarding.jpg`,
  `session-resumed-overview.jpg`, `session-expired-signin.jpg`.

No final failures. Not executed: real-account signup, actual phone file selection,
real delivery, deployment, production SQL/RLS advisors or migration activation.
The migration requires local/staging Postgres verification and explicit owner
review before any production application. SQLite verifies the API ownership
boundary; it cannot prove Postgres RLS execution. No native Library artifact
was created; deliverables are committed repository code, docs and screenshots.

Current reference documentation used:
[Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security),
[Supabase getUser](https://supabase.com/docs/reference/python/auth-getuser),
[Supabase changelog](https://supabase.com/changelog),
[MDN Contacts Picker](https://developer.mozilla.org/en-US/docs/Web/API/Contact_Picker_API).

## Integration boundaries

Original onboarding commit: `8e5eb7ad87f5919f9d423586251888913a303f87` on
`codex/church-admin-onboarding`. The tested integration candidate is on
`codex/church-admin-onboarding-integrated`, based on verified integration
`3c72bfd` and prepared entirely in the isolated onboarding worktree. The parent
owns final integration after the auth worker's Chrome diagnosis is stable.

The `app.js` conflict is resolved in the candidate. Existing `rememberSession`,
`savedSession`, 401/403 handling and recovery behavior are preserved. A shared
`openCoordinatorWorkspace()` verifies `/api/state` first, then loads the account's
setup for login, confirmation callback and saved-session restore. Unfinished
accounts land in setup; completed accounts land in Overview. Missing setup
migration shows a clear storage-update state without clearing a valid session.
Logout keeps `rememberSession(null)` and clears the live roster/setup. Tests
exercise completed/unfinished reload, missing migration, logout and expired
server-rejected sessions. No unresolved merge conflict remains against `3c72bfd`.
The auth worker's subsequent fixes still need parent-coordinated integration.

Other changed existing files are `app/main.py` (SQLite-only setup initialization
and router mount), `web/texty/public/style.css` (responsive setup styles), and
`web/texty/tests/confirmation-ui.test.js` (synthetic setup API fixture). All
remaining files are additive. No `app/web/texty.py`, auth URL/config, bridge,
provider, send-gate, runtime, shared-checkout or production-store edits were made.

## Delete a volunteer and restart signup

Open the volunteer’s profile, select **Delete volunteer**, and confirm their name.
This removes the profile from the roster and cancels its queued texts and pending
reviews. Historical records stay attached to the removed identity. Active shift
assignments, open care follow-ups and unsettled delivery must be resolved first.

Re-add the same phone with verified text consent to create a fresh profile and
prepare a new Gloo welcome. In exact-review mode the welcome awaits review; other
connection, consent and scheduling holds remain visible. If preparation is held,
the profile is still saved and its welcome action can retry. Re-adding does not
inherit old qualifications or availability, erase phone opt-outs or authorize
new transport recipients. This does not remove the conversation from Messages.
