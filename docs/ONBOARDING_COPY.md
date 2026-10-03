# Editable onboarding messages

Open **Settings → Edit onboarding texts**, or `/onboarding-copy.html` on the
connected dashboard. The separate editor contains role selection, availability,
availability clarification and completion fields, with Save, Reset, Reload and
a live copy preview. Current canonical copy is shown; Jacob's missing revision
has not been invented. `{first_name}` and `{roles}` remain personalized.

Save on the signed-in dashboard persists an administrator-scoped wording draft
on the server. Reset changes only the boxes; Save persists the reset. Revision
checks reject concurrent overwrites, and failed saves retain the edits. Without
a connected backend, the fictional preview saves only in browser storage.
Previewing and saving do not invoke Gloo or any message transport.

## Account-to-conversation mapping

- `/api/setup/onboarding-copy` authenticates with the existing verified,
  allowlisted Supabase administrator dependency. `owner(user)` normalizes the
  server-provided UUID. Client-supplied owner or workspace fields are rejected.
- Drafts are stored in the existing `policies` table under
  `onboarding_copy:<verified owner UUID>` with messages, revision and timestamp.
  The API scopes every read and write to that exact key; responses are not cached.
- Authenticated `/api/volunteers/{id}/text-setup` passes that verified owner to
  `onboarding.start(copy_owner=...)`. The volunteer's `onboarding_copy_owner`
  preference binds the operational conversation. A later authenticated restart
  by administrator B replaces administrator A's binding with B. Existing
  in-progress onboarding still cannot be restarted by this endpoint.
- Stage replies look up only the exact bound owner's draft. Ordinary inbound
  signups are unbound: they use canonical copy, even if other administrators have
  saved drafts. A new unbound start clears any previous binding. There is no
  first-owner, latest-owner or global fallback selection. Missing, invalid or
  nonexistent bindings provide no draft wording to Gloo.
- A role-stage clarification uses the interests field. An availability-stage
  clarification uses the separate clarification field. Successful interests
  use availability; successful availability uses completion.

Draft wording reaches Gloo only as `preferred_wording`, separate from the
canonical code-owned `approved_message` facts. Reply writer v6 treats draft
text as data and preserves current stage, consent, clearance and booking facts.
Personalization uses the current volunteer and role catalogue; history remains
scoped by the existing Messages test-session checks. Every onboarding reply
requires Gloo, even if the older optional-reply setting is disabled: unavailable
Gloo never causes a literal saved template to send. SendGate still owns delivery,
consent, quiet hours and scheduling constraints. Emoji remain optional, spaced
and varied under the existing composition checks.

## Integration and verification

Feature JS, HTML, CSS, defaults and API are separate modules. `app.js` is untouched.
One additional module script in `index.html` adds the settings link after the
existing settings panel renders. `app/main.py` registers the dedicated router.
The only existing operational endpoint edit passes the authenticated owner to
the text-setup start call. The onboarding parser and `prompts/onboarding.md`
are unchanged; any simultaneous natural-availability work should preserve the
composition calls when merging `app/core/onboarding.py`.

No dependency, schema migration, hosted SMS service or credential was added.
The original single-church operational roster is still shared. This feature
isolates administrator copy, not the entire roster into multi-church tenancy.
Edits become wording preferences for explicitly bound conversations; saving a
draft alone does not bind existing/unbound volunteers or restart onboarding.
Gloo may paraphrase; the displayed copy preview does not assert actual AI output
or delivery. No real texts, Gloo calls, scheduler activation or deployment was
performed during this work.

Validation on the isolated baseline:

- Full Python suite: **575 passed, 1 existing expected failure** for the immutable
  earlier quiet-hours eval (`docs/EVALUATION.md`). Includes 19 new editor checks.
- Full Node suite: **32 passed**, including 4 copy-state checks.
- Headless Chromium with fabricated account and mock transport: four fields,
  live preview, Save/reload persistence, Reset, Reload saved copy, no JS errors,
  and a 360px-wide layout without horizontal overflow. Screenshots are in
  `docs/evidence/onboarding-copy/`.
- The older `ProfileGloo` test fixture now separates extraction responses from
  composition responses; its journey checks three extraction calls plus three
  Gloo-written onboarding replies rather than relying on literal fallback.

Worktree: `managed local worktree`.
Base: `9ed9d71203ed86989455e81a6fca2717a8017682` from
`codex/complete-text-monkey`. Local feature commit is recorded in the chat
handoff. Remote integration/push remains owned by the GitHub handoff chat.
