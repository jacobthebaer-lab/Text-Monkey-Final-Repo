# Text Monkey coordinator portal

The coordinator portal lives in `web/texty/` and is served locally at `/texty`.
These internal compatibility names are retained; the visible product is Text Monkey.
See the [README](../README.md) for current runnable setup and [Clyde handoff](CLYDE_HANDOFF.md)
for checkpoint validation and outstanding connections.

## Preview and connected modes

The [public static preview](CLOUDFLARE_DEMO.md) uses fictional browser-local data and
coverage, roster and settings views. It has
no Gloo, real accounts, backend mutation or message transport. Incoming-text simulation, sample-send/admin-preview controls and fake texting toggles
have been removed from the current UI. Real checks use the connected app only.

The connected backend uses Gloo for interpreting and composing messages. Hard
eligibility, affirmative acceptance, consent, quiet hours and approval checks stay
in code. A qualified affirmative reply can fill an offer; the model cannot grant
qualifications. Text signup collects name, explicit consent, interests and availability.
Serving requests wait for review where required.

## Accounts and persistence

Administrator signup/sign-in, email confirmation, password recovery and sign-out
use the configured Supabase project. Set the intended organization, private database
login and `ADMIN_EMAIL_ALLOWLIST` locally. No public signup field grants administrator
access or texting consent. The backend verifies confirmed identity and the allowlist.
Use account-owned setup records for church preferences, event settings and recipients;
imports are staged for review and cannot contact people or grant qualifications.

Migrations are under `supabase/`. Review and apply them to your chosen database;
a committed migration is not proof that it was applied. Never run destructive seeding
against a connected database. Do not copy developer accounts, credentials or personal
test profiles into a demo. Exact Supabase redirect URLs must match your hosted origin.

## Runtime boundaries

Connected texting depends on the Mac, Messages, backend and connector, with selected
receiving line, recipient scope and private transport/session configuration. A temporary
tunnel is not an always-on host. Scheduler and connection status must reflect actual
runtime state. Queue submission and native delivery are separate evidence.

Historical Gloo/device checks are recorded separately from the synthetic portal.
They do not establish a currently running scheduler, renewed test window, carrier SMS
verification or production readiness. Do not renew a past personal test from these notes.

See [administrator setup](ADMIN_SETUP.md), [Mac Messages](MAC_MESSAGES.md),
[response windows](RESPONSE_WINDOWS.md) and [model verification](GLOO_VERIFICATION.md).
