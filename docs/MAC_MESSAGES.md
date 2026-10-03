# Text Monkey's first-party Mac Messages connector

The local signup test uses a private, gitignored configuration with one
approved sender and a selected receiving line. The reader filters that line
before fetching content, and delivery binds to its existing direct chat without
changing Messages' default. Gloo writes signup replies when
`GLOO_SIGNUP_REPLIES=true`; the consent state remains enforced by code. A private
`MAC_TEST_SIGNUP_REPLY_UNTIL` can permit signup replies during quiet hours for
at most two hours, only to approved test phones. It does not allow outreach.
The signed-in dashboard refreshes shared Supabase data every ten seconds while
visible and idle, including its roster and Text Monkey calendar.

Volunteers message the iPhone number registered in Messages. Our Mac connector
reads new direct texts from configured demo phones and selected services, calls the existing Gloo
parser and scheduling core, and sends queued replies with Messages' native
AppleScript command. No BlueBubbles, private API injection or volunteer login.

Regular **SMS is the intended volunteer transport**. The connector supports
explicit SMS selection through iPhone text forwarding, including a Google
Voice volunteer texting the church's carrier number. iMessage remains an
optional compatibility mode; RCS is excluded. SMS code has synthetic coverage;
real-device SMS delivery is not yet verified. Keep the Mac and church iPhone
awake/online and verify the actual round trip before relying on it.

## Google Voice volunteer / carrier SMS church test

1. Set up a personal Google Voice number using [Google's number setup](https://support.google.com/voice/answer/115061).
   This is the volunteer's test number. The church's existing mobile number
   stays on its carrier; it is not ported to Google Voice.
2. On the church iPhone, enable this Mac under Settings → Apps → Messages →
   Text Message Forwarding, using the same Apple Account on both devices.
   See [Apple's setup instructions](https://support.apple.com/en-us/102545).
3. Set backend `MAC_MESSAGE_SERVICES=SMS`. In the private worker configuration,
   set `"services": ["SMS"]`, `receiving_number` to the church number and
   `phones` to only the Google Voice test number, all in international format.
   Remove any previous tester from the backend and worker allowlists.
4. Use a fresh checkpoint when the service, line or tester changes. It starts
   at the current database watermark and skips old conversations. Keep delivery
   paused until the test number and forwarding are ready.
5. From Google Voice, text JOIN to the church number. Verify that it appears in
   the Mac's Messages app as a direct SMS conversation on the selected line.
   Outgoing delivery binds that existing conversation; it never changes the
   default Messages sending number or starts a fallback iMessage conversation.
6. Follow name → YES → interests → availability. Verify an actual reply in
   Google Voice, the Gloo audit, native acknowledgements, Supabase profile and
   dashboard roster. A cancellation/replacement test needs assigned test slots
   and a second consenting volunteer to demonstrate a replacement.

Missing or ambiguous line metadata stops delivery rather than guessing a
sender. Only allowlisted new direct SMS on the chosen receiving line is read.
The native command accepts an existing SMS chat; real carrier delivery must
still be observed in Google Voice, since `submitted` is not a delivery receipt.

For a user-authorized test that should behave like a normal signup, set the
private worker configuration's `input_mode` to `natural`. Volunteers then reply
with ordinary text; session IDs are attached by the connector and never need
to be typed. This mode retains the exact phone, receiving line, service, direct
conversation, active session and message-time filters. A fresh checkpoint is
required when switching modes so earlier conversation history is skipped.
Outside the session it reads only explicit opt-out commands. The default
`marked` mode remains available for tests that explicitly request markers.

## Integration

- Each scheduled event gets a coordinator status update three hours before its
  start, using saved active, consenting coordinator records. The existing
  background tick queues it even when there have been no staffing changes.
  Gloo's Responses API writes the update from current staffing, replacement
  searches and approvals; an AI outage retries, then records an internal issue
  rather than sending a template substitute. A covered event says no action is
  needed; gaps and approvals explain the next step. Events without a saved
  staffing plan are reported as needing review.
- Pre-event updates persist and deduplicate across restarts. Quiet hours defer
  them and refresh the facts before composition. Cancelled, completed, started
  or rescheduled events invalidate the old update, including queued Mac
  deliveries. Existing exact-review mode and Mac recipient/session limits still
  apply. The live scheduler, real Gloo key and admin's enabled Mac transport
  must all be configured; the synthetic dashboard preview does not deliver.
- `/mac/inbound` requires a separate secret and exact demo number. It uses
  Gloo, FillContext and handle_inbound; duplicate GUIDs are handled atomically.
- Supabase-protected Text Monkey admin approvals queue native replies only for
  Mac-origin proposals. Simulator/legacy proposals stay simulated.
- Coordinator YES/NO texts resolve Mac-origin approvals only. Existing
  qualification, consent, pastoral holds and scheduling rules remain enforced.
- The current text-only signup flow also uses this transport: Gloo extracts
  the name, Text Monkey asks for explicit YES consent, and a confirmation returns by
  the selected text service. Volunteer signup does not grant administrator or role qualifications.
- SendGate creates durable queued messages in the business transaction.
  `/mac/outbound/pull` rechecks opt-out, sensitive blocks, allowed phones and
  real delivery-time quiet hours before reserving a claim.
- The Mac journals attempts before sending. `submitted` means Messages
  accepted the command, not delivered/read. Crashes/timeouts can be `uncertain`;
  they are never automatically resent. Claims without acknowledgments require
  manual reconciliation.
- On first start, old history is skipped. Content is queried only for selected
  direct conversations and selected services. Groups, outgoing messages,
  unselected services, RCS and email handles are excluded. Apple's database is
  opened read-only and never modified.
- Our Foundation helper decodes attributed text in a separate process.
  Unsupported/oversize messages pause processing rather than guessing.

## Backend setup

Use the existing backend dependencies and Gloo/Supabase settings; there are no
new Python dependencies. Privately set these values in the backend environment:

```dotenv
SMS_PROVIDER=mac_messages
MAC_BRIDGE_ENABLED=true
MAC_BRIDGE_TOKEN=<a unique random secret of at least 32 characters>
MAC_DEMO_PHONES=<comma-separated exact +country-code demo numbers>
MAC_MESSAGE_SERVICES=SMS
ADMIN_PASSWORD=<a separate strong password of at least 16 characters>
LIVE_SMS=false
```

Retain `GLOO_API_KEY`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, verified
`ADMIN_EMAIL_ALLOWLIST` and `BACKEND_BRIDGE_KEY`. The Mac token is separate from
the Cloudflare bridge key and is never exposed in browser configuration.

Include every consenting demo recipient, including coordinator/pastor phones.
Use fictional profiles and an isolated demo database. Adding a number does not
verify consent or ministry qualifications. A profile must have
`is_coordinator=true` to approve by text; web approval uses Supabase identity.
Run **one backend worker** for SQLite. Delivery checks real church-local time
even if the scheduling demonstration uses a fake clock. Database reset is
blocked while Mac mode is enabled.

For Supabase Postgres, apply `supabase/mac_messages_transport.sql` in the exact
Text Monkey project before startup. It adds two private tables with RLS enabled and
no browser/Data API access. No existing table is dropped or reset.

## Existing ngrok and admin website

Keep the existing ngrok tunnel targeting the Python backend. The Mac worker
can use `http://127.0.0.1:8001` locally, without another tunnel; remote origins
must use HTTPS. Cloudflare's `BACKEND_URL` remains the backend/ngrok origin;
its `BACKEND_BRIDGE_KEY` must match the backend. The dashboard keeps its
Supabase verified-email login and roster/history/approval endpoints.
The worker uses `/mac/*` directly on the backend, not the website's `/api/*`
proxy. No Messages database or separate send server is published.

## Mac setup

1. Copy `mac-bridge.example.json` to `.mac-bridge.json`; fill in the backend
   origin, same Mac secret and exact demo phones. Keep the file private, mode
   600. Both it and `.mac-state/` are gitignored.
2. Build and test our helper with synthetic text:

   ```sh
   mkdir -p .mac-state
   xcrun swiftc app/integrations/decode_message.swift -o .mac-state/decode-message
   .mac-state/decode-message --self-test
   ```

   Foundation's deprecated NSArchiver APIs are used intentionally for Apple's
   typedstream archive format; compiler deprecation warnings are expected.
3. Grant the process hosting the worker Full Disk Access to read Messages'
   database. macOS also requires Automation permission to control Messages.
   Restart that process after granting access. No SIP changes are required.
4. Run `python -m app.integrations.mac_messages --config .mac-bridge.json`.
   It processes new selected messages with outgoing delivery **off**. It never
   replays old history. Preserve the checkpoint and delivery attempt journal.
5. After approving a specific real-device test, add `--live-delivery` to enable
   replies to those numbers. CLI locking prevents simultaneous workers on the
   same checkpoint. Changing the phone list, service or receiving line requires
   a fresh checkpoint so old history remains skipped.

## Verification and runtime limits

Synthetic tests cover authentication, service/phone filtering, rollback,
deduplication, admin approval origin, quiet hours, opt-out, pastoral holds,
simulator isolation, crash recovery and native-send failures. AppleScript
compiles against this Mac's Messages dictionary. The Swift helper compiles and
round-trips synthetic attributed text. Initial construction used synthetic
fixtures and did not send real messages.

A separately authorized one-shot device test used real Gloo composition and the
application's Messages gate. The exact new native outgoing message was confirmed
sent and delivered with no device error; its private receipt remains outside Git.
This verifies one iMessage delivery, not completed onboarding, carrier SMS,
account enrollment or ongoing background scheduling. The public static preview
uses fictional browser-local data and has no connected backend or text transport.
Calendar cancellation/replacement behavior has synthetic integration coverage;
a real replacement needs its own authorized consenting test participants.
Connected delivery depends on the Mac, worker, backend and any required tunnel
remaining available. Exact phone values and credentials remain only in private,
gitignored configuration.
