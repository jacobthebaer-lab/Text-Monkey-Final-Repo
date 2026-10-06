# Cloud controls across session refresh

The cloud controls previously treated a changed bearer token as a changed login.
A normal coordinator token refresh during a mutation therefore discarded its
successful response and skipped busy-state cleanup. Later actions returned
without making a request, and loading status did not release the busy state.

The controls now use the coordinator session epoch. Token rotation preserves
that epoch, while sign-out and a new login change it. Results from the current
login remain usable after refresh. Cached privileges and old responses are
discarded after a login change, and cleanup from an old operation cannot release
a newer operation's busy state.

The actual coordinator-session and cloud-control modules are exercised with
synthetic sessions and fake upstream responses. The focused tests cover refresh
during pause and load, failed mutations without automatic replay, sign-out,
account switching, and overlapping operations across a login change. Together
with the existing cloud UI and session tests, 43 targeted tests passed.

The source reproduction against deployed revision
`9cc11b3b66eabdf44af60ed7544106673bfb1948` produced disabled controls after token
rotation. It establishes this defect but does not fully explain the separately
observed enabled buttons doing nothing. Connected runtime behavior has not been
verified for this change. No deployment or transport settings were changed.
