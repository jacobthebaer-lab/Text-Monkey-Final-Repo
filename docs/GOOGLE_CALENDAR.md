# Admin Google Calendar integration

Admins can connect their own Google account in **Settings → Google Calendar**, select a church source calendar, and choose **Sync now**. Each sync imports timed events from the next eight weeks into the shared church schedule, then publishes Text Monkey events and staffing coverage to a separate Google calendar named **Text Monkey**. **Publish schedule** exports the schedule without an import. Sync is operator initiated, not a background job.

The source calendar stays unchanged. The app-created output calendar contains event titles, start/end times and filled/total staffing counts. It contains no volunteer phone numbers, conversations, guest lists or invitations. Repeating a sync updates existing publications. Removing or cancelling a previously published local event removes its app publication. Do not select the output calendar as the source: Settings rejects that loop. Each admin owns their own Google authorization and output calendar, while imported events enter the existing shared church schedule.

## Owner configuration

1. Enable the Google Calendar API in your Google Cloud project and configure its OAuth consent screen. Register a **Web application** OAuth client. In testing mode, add the intended admins as test users. External/public rollout may require Google's verification for the requested scopes.
2. Add the exact authorized redirect URI, such as `https://YOUR_ADMIN_HOST/api/google-calendar/callback`. Local development accepts `http://127.0.0.1:8000/api/google-calendar/callback` or localhost. Use the same frontend origin for the callback and `ADMIN_SITE_URL` so the original tab can complete authorization.
3. Set these private backend environment variables:

   ```dotenv
   GOOGLE_CALENDAR_CLIENT_ID=your-web-client-id
   GOOGLE_CALENDAR_CLIENT_SECRET=your-web-client-secret
   GOOGLE_CALENDAR_REDIRECT_URI=https://YOUR_ADMIN_HOST/api/google-calendar/callback
   GOOGLE_CALENDAR_STATE_DIR=/private/persistent/google-calendar
   ADMIN_SITE_URL=https://YOUR_ADMIN_HOST/texty
   ```

4. Use a persistent private host volume with encryption at rest for the state directory. It is not the public assets directory. Files use mode 0600, their directory uses 0700, and a process-shared lock serializes changes. Refresh tokens live only in these private files, not in the application database or browser. Backups must preserve the same privacy. The default `.google-calendar-state/` is Git-ignored. The host must support POSIX file locks; do not share this directory between unrelated deployments.
5. Keep coordinator Supabase login and its existing admin email allowlist configured. With Cloudflare, the existing `/api/` proxy forwards the callback and applies the backend bridge secret. Do not require a dashboard bearer token on the public Google callback; it verifies a short-lived single-use state and PKCE instead. Only the original authenticated browser tab can finalize the pending connection. Keep callback query strings out of proxy/access logs; the Python callback removes its query from the backend access log scope.
6. Restart the backend, sign in as the intended coordinator, connect Google Calendar, verify the displayed Google account and select the church calendar before importing. First publication creates the separate output calendar. Google secrets must never be entered in the dashboard or committed.

Authorization requests `openid`, `userinfo.email`, `calendar.readonly` and `calendar.app.created`. Read access supports selection and event import. Write access is limited by Google to calendars created by this app, rather than all calendars in the admin's account. See [Google's server OAuth documentation](https://developers.google.com/identity/protocols/oauth2/web-server), [Calendar authorization scopes](https://developers.google.com/workspace/calendar/api/auth) and [event listing](https://developers.google.com/workspace/calendar/api/v3/reference/events/list).

## Import and review behavior

Recurring services expand to individual events. Google IDs are namespaced by source calendar, so repeated imports and multiple admins importing the same church calendar do not duplicate events. All-day and special personal events are skipped. Calendar timezones and explicit offsets are respected. Existing event-type patterns and role recipes create staffing positions. Unknown event types create an internal request for a staffing recipe.

Time/type changes or cancellations affecting proposed, approved or confirmed assignments hold the local event for coordinator review. They do not silently alter assignments or queue texts. Recipe changes with historical assignments also wait for review. Cancellation review notices deduplicate. Unassigned events can update or cancel directly. A Google API failure rolls back the import and leaves its prior successful receipt intact.

Publication has deterministic remote IDs and saves progress after each successful operation. If Google fails partway, retry **Publish schedule**; already exported events update instead of duplicating. Imported events may already be saved if the later publication fails, and Settings reports each direction's last success separately. No sync enables message transport, texting automation or Google Voice.

**Disconnect** forgets this admin's stored Google authorization and pending flows. Existing local events and published Google calendars remain. Remove Text Monkey's saved permissions in Google account settings to revoke access at Google too. Reconnecting after a disconnect creates a new output calendar; the admin can remove an old feed manually. Reconnecting an existing connection to the same Google account preserves its output calendar and publication IDs. Google's testing-mode refresh-token expiry or revoked access requires reconnecting.

## Verification and live acceptance

Automated tests use synthetic identities and mock Google services. They cover OAuth replay/expiry, incomplete consent, coordinator isolation, source selection, pagination, duplicate prevention, protected cancellations, publication retries and session changes. These do not establish live Google account connectivity.

With the owner configuration applied, verify live acceptance using a dedicated test church calendar: connect the intended admin account, import one timed event, repeat without duplication, publish it, edit its time and sync again, and cancel an unassigned event. Confirm the Google output changes and the source remains untouched. Confirm assigned changes appear for internal review. Do not use real recipient/contact data in repository evidence.
