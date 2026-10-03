# Mobile and keyboard quality proof

Reviewed in an isolated checkout based on `9ed9d71`, with fictional data only. These local screenshots establish the source changes; they do not establish Cloudflare publication. Publish only the Git owner's integrated checkpoint, then record its deployment and hosted verification separately.

- 320, 390 and 768px: Home, Volunteers, Shifts, Messages and Settings have no root horizontal overflow. All page navigation restores main-content focus. Roster and schedule scroll remain inside named, keyboard-focusable regions.
- Enter opens the preview and focuses main content. ArrowRight on the roster region scrolls it horizontally (16px observed).
- Volunteer dialog opens with the first name focused and a name matching its heading. At 320px both name fields use the full 231px form width. Invalid phone error receives focus, has alert semantics and stays visible; no volunteer is saved until valid. Successful fictional save restores the Add volunteer button.
- Changing setup step focuses its heading; the next Tab reaches Your name. Every setup field has a visible associated label. Controller regression verifies server-save errors receive focus.
- Simulator absent throughout; Settings retains accurate Texting disconnected status. No messages sent or live credentials/accounts used. No captured browser warnings/errors.

Evidence: `layout-checks.json`, `form-error-320.jpg`, `setup-keyboard-320.jpg`, `settings-390.jpg`.

Validation: 28 frontend checks passed; 7 publication checks passed (including detection of a stale additional bundled module). JavaScript syntax and whitespace checks passed. The publication verifier now checks seven core assets plus every bundled JavaScript module so future component additions cannot escape hosted verification.
