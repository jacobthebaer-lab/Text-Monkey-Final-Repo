# Calendar audit, October 2026

Baseline: `1232893df8c03d993e5fc1fa9067162718973466`, the current integration branch when this audit began. This report covers Google Calendar import, admin OAuth, source selection, publication and their Settings UI. Other integrations belong to the project-wide audit.

## Defects repaired

| Trigger | Previous behavior | Repair and verification |
| --- | --- | --- |
| Reauthorize the same Google account | Source selection and successful import receipt disappear | Retain source, timezone and receipts; a different account still starts clean. Both cases tested. |
| Delete the app-created Google output calendar | Every publication fails against its saved ID | Recreate a missing feed with a new namespace and cleared receipt. Permission failures preserve existing progress. Tested with missing and forbidden responses. |
| A later operation fails after a successful publication/import | Success or ongoing publication text remains beside the error | Clear transient notices and preserve each direction's last successful receipt. Tested for standalone and partial-sync failure. |
| An imported upcoming event moves outside the eight-week list | The old local schedule stays active | Share a private source-ID map across admins and look up omitted active imports after pagination. Update unassigned events; hold assigned changes for review. Failure rolls back the import. Tested across admins, moved events and lookup failures. |
| A timed import becomes all-day or a special event | The stale timed local event survives a skipped import | Cancel unassigned events or preserve assigned events with a deduplicated review flag. Tracking is pruned appropriately. Tested for both assignment states and missing/deleted events. |
| Browser session storage is blocked during logout/reset | Calendar cleanup throws and can interrupt broader account cleanup | Make flow removal best effort. Connecting still requires usable flow storage and reports failure safely. Tested for reset, denied callback and connection. |
| Google's HTTP transport cannot resolve a server | Unhandled transport exception escapes as HTTP 500 | Handle `HttpLib2Error` through the existing safe Calendar HTTP 502 response. DNS failure regression tested. |

Import tests also verify explicit offsets through the DST fallback hour. No imported change queues or sends messages. Source calendars are read-only, and publication stays within the app-created calendar scope.

## Feature coverage and evidence

The existing inventory ID `gcal` covers service-account read-only import. The newer admin capabilities need separate inventory entries:

| Feature ID | Audit evidence |
| --- | --- |
| `gcal` | Existing importer plus namespaced admin import, pagination, recipe creation, duplicate prevention, assignment holds and cancellation tests |
| `gcal-admin-oauth` | PKCE/state replay and expiry, incomplete consent, coordinator isolation, same/different account reconnect tests |
| `gcal-source-selection` | Calendar validation and output-source loop rejection tests |
| `gcal-reviewed-import` | Cross-admin tracking, omitted-event lookup, all-day conversion, DST and rollback tests |
| `gcal-publication` | Deterministic IDs, retry progress, app-owned deletions, missing-feed recovery and permission failure tests |
| `gcal-session-ui` | Callback completion, session changes, stale notices, partial-sync receipts and restricted storage tests |

Validation completed in this isolated checkout:

- Backend: **64 passed**, covering `test_google_calendar.py`, `test_texty_public_modules.py`, `test_texty_setup_assets.py`, `test_app_boots.py` and `test_texty.py`.
- Frontend: **173 passed**, the complete `web/texty` Node test suite, including eight Calendar UI tests.
- Regression tests reproduced the affected behavior before the fixes. All Google API responses and identities in these checks are synthetic.

## Current status and remaining acceptance

These repairs are ready for integration review. This audit made no runtime deployment, OAuth configuration change or live Google Calendar mutation. The master coordinator owns release integration and deployment.

Live imports still require the admin to choose the intended church source calendar. A live round trip using a dedicated test church calendar remains needed to verify edits, cancellations, assigned-event review and output-feed recovery. Existing account/publication evidence does not establish those import cases.

Older imports that already moved outside the list before source tracking existed cannot recover their raw Google IDs from the hashed local keys. They need coordinator review; imports visible in a subsequent successful sync gain tracking automatically. The legacy service-account caller retains its bounded-list behavior unless it supplies tracking. Explicit disconnect forgets publication IDs, so a later connection creates a new output feed as documented. Google testing-mode authorization may require reconnecting.

The focused backend checks do not constitute a complete backend test run or proof that every project feature works. Transport and delivery acceptance remain outside this Calendar audit.
