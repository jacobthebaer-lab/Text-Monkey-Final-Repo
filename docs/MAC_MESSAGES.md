# Texty's first-party Mac Messages connector

Volunteers message the iPhone number registered in Messages. Our Mac connector
reads new direct iMessages from configured demo phones, calls the existing Gloo
parser and scheduling core, and sends queued replies with Messages' native
AppleScript command. No BlueBubbles, private API injection or volunteer login.

This version supports **iMessage only**. Android/SMS/RCS require separate
implementation and device testing. Apple's iPhone text forwarding is not proof
that this connector delivers SMS or has Apple/carrier approval for automated
service. Keep the Mac awake/online and test the actual devices before use.

## Integration

- `/mac/inbound` requires a separate secret and exact demo number. It uses
  Gloo, FillContext and handle_inbound; duplicate GUIDs are handled atomically.
- Supabase-protected Texty admin approvals queue native replies only for
  Mac-origin proposals. Simulator/legacy proposals stay simulated.
- Coordinator YES/NO texts resolve Mac-origin approvals only. Existing
  qualification, consent, pastoral holds and scheduling rules remain enforced.
- SendGate creates durable queued messages in the business transaction.
  `/mac/outbound/pull` rechecks opt-out, sensitive blocks, allowed phones and
  real delivery-time quiet hours before reserving a claim.
- The Mac journals attempts before sending. `submitted` means Messages
  accepted the command, not delivered/read. Crashes/timeouts can be `uncertain`;
  they are never automatically resent. Claims without acknowledgments require
  manual reconciliation.
- On first start, old history is skipped. Content is queried only for selected
  direct conversations. Groups, outgoing messages, SMS/RCS and email handles
  are excluded. Apple's database is opened read-only and never modified.
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
Texty project before startup. It adds two private tables with RLS enabled and
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
   same checkpoint. Changing the phone list requires a fresh checkpoint so old
   history remains skipped.

## Verification and remaining prerequisites

Synthetic tests cover authentication, service/phone filtering, rollback,
deduplication, admin approval origin, quiet hours, opt-out, pastoral holds,
simulator isolation, crash recovery and native-send failures. AppleScript
compiles against this Mac's Messages dictionary. The Swift helper compiles and
round-trips synthetic attributed text. No personal message content was read
and no actual text was sent during construction.

The current process could not open the actual Messages database, so its schema
and live access remain unverified. Exact demo numbers are still needed. OS
access, an active Gloo credential, configured Supabase and an end-to-end device
test remain prerequisites. The existing AI-ready flag means a key is configured,
not that authentication succeeded; this connector does not fix an invalid key.
