# Admin session source investigation

Source defects can explain repeated sign-outs, but this investigation does not
prove which failure caused a particular live occurrence. No browser storage,
real token, credential, Supabase setting or live auth response was inspected.

- `/api/login` returned only the access token. The callback/storage path likewise
  dropped the refresh grant. There was no refresh implementation. Thus normal
  short-lived JWT expiry inevitably required another sign-in even when the
  underlying Supabase session could be refreshed.
- The protected-user check mapped every non-200 Supabase response to 401, including
  rate limits and outages. The frontend then deleted its session.
- Generic resource 403 responses deleted valid coordinator sessions. Only cloud
  controls had an exception. A denied superadmin/scoped action is not proof of
  revoked coordinator access.
- A restored workspace failing to load, even temporarily, unconditionally deleted
  its session. A late unauthorized response could also clear a newer login.

The fix preserves the granted access/refresh pair in the existing tab-scoped
storage, rotates once before expiry or after an explicit 401, coalesces parallel
refreshes, and rejects responses from a previous login epoch. Only an explicit
401 may retry a protected request once; network/5xx failures never replay an
ambiguous mutation. Normal resource 403 preserves login. The unchanged verified
allowlist check marks actual access revocation, which still clears the session.
Supabase-rejected refreshes still require sign-in. Temporary startup failure
shows a retry screen, preserves the grant and does not show cached roster data.
Explicit sign-out clears the local session even if upstream logout fails.

Supabase alone controls JWT lifetime, revocation, session timeout and verification.
No lifetime, cookie, allowlist, bridge, MFA or role policy changes are included.
This follows [Supabase session rotation](https://supabase.com/docs/guides/auth/sessions)
and its [refresh operation](https://supabase.com/docs/reference/javascript/auth-refreshsession).

Backend and frontend changes must ship together, with the refresh endpoint
available before the frontend depends on it. Legacy access-only tabs remain
usable until normal expiry, then need one new login to obtain the refresh grant.
There is no attempt to recover secrets from older tabs. This source candidate
is not deployed and does not touch Google Voice, Gloo, transport settings or SMS.

Verification uses only synthetic temporary sessions and fake upstream responses.
It covers verified/allowlisted access, malformed/expired grants, outage status
classification, rate limits, exact granted expiry, concurrent rotation, restored
legacy sessions, denied scopes, actual revocation, startup retry, offline signout,
and old responses arriving after signout or a different login.
