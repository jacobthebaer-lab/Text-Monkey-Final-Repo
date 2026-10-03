# Text Monkey brand integration

This change applies Jacob’s official Text Monkey kit to the existing coordinator
application, including sign-in/account creation/recovery, Overview, church setup,
settings, CSV/XLSX/vCard import presentation, roster, schedule, text lab and the
older FastAPI coordinator pages. There is no separate replacement product.

## Source and design boundaries

Inspected `BRAND.md` and the supplied PNG pixels in
`/Users/jacob/Downloads/Chrome/Text Monkey Brand Kit` before implementation.
All 17 original kit files are copied byte-for-byte into
`web/texty/public/brand/`: six stacked/horizontal logos, three wordmarks, one
monkey mark, two app icons, two favicons, one reference sheet and the guidelines.
The original sunglasses, phone screen, colors and proportions remain intact.

Palette: Banana Yellow `#FFD23F`, Cocoa `#3B2314`, Monkey Brown `#6B3E26`, Face Tan
`#F2C49B`, Screen Lime `#9BE564`, Shade Black `#141414`, Cream `#FFF8E6`.
Cream/cocoa keep the operational workspace readable; yellow welcome areas and
lime accents carry the brand. Secondary warm tints and semantic error/success
colors support readable status labels. Bagel Fat One is reserved for display
headings; DM Sans 400/600 serves controls, tables, care reviews and body copy.
The official tagline appears at sign-in, with occasional friendly website humor.
Sensitive care, consent, errors and exact-send approvals remain plain.

Official Google Fonts are self-hosted, with their OFL licenses in `brand/fonts/`.
There are no browser requests to a font CDN and no CSP relaxation. Sources:
[Bagel Fat One](https://github.com/google/fonts/tree/main/ofl/bagelfatone) and
[DM Sans](https://github.com/google/fonts/tree/main/ofl/dmsans).
Favicons, touch icon and a simple manifest use the supplied artwork; no service
worker, offline storage of live data or auth caching was added.

Auth/session helpers, backend identity checks, account-owned setup/import queries,
consent, qualifications, approval handlers and send gates are unchanged. Existing
Text Monkey signup handling already guarantees a single full-body `🐒` suffix;
its tests still pass. Database/schema names, compatibility URLs, storage keys,
Cloudflare deployment identity and bridge headers retain their existing names.
The FastAPI public app title/health label and the frontend APP_NAME now match the
product. No database migration is introduced or applied.

## Verification

- Full backend suite: **401 passed** with `DATABASE_URL=sqlite://` and
  `PYTHON_DOTENV_DISABLED=1`; mock SMS and synthetic fixtures only.
- Frontend suite: **23 passed**, including actual admin-composer holds, session
  restore/logout, exact approvals, setup/import ownership behavior and signup.
- Final targeted public-asset and loopback-launcher checks: **3 passed**.
- `node --check` for app/setup modules and `git diff --check`: passed.
- Added a narrow API-boundary regression: every allowlisted brand file is served,
  unknown/traversal paths return 404, and protected admin pages remain protected.
- Verified all 17 kit source files are byte-identical to their committed copies.

Actual Chrome QA ran the real FastAPI application on private loopback port 58124,
with an explicitly created in-memory SQLite database, mock SMS, blank external
service settings and background jobs disabled. The browser used the existing
app’s labelled synthetic mode for interaction. This did not connect to the shared
runtime, Supabase, Gloo, a live texting line or any real contact list.

Browser checks: sign-in and invited account creation presentation; all three
church setup steps and completion; synthetic import mapping and preview
(2 ready, 1 duplicate, 1 invalid), followed by two staged contacts still labelled
“Awaiting consent / cannot text”; reload/resume; plain sensitive-care review;
volunteer dialog labels and focus return; settings, schedule and roster.
Checked 320/390 phone widths, 768 tablet width and the desktop viewport. All seven
screens fit 320px after fixing a positioned hidden table-header label that
expanded the page. Wide tables retain their own horizontal scrolling.

Targeted accessibility checks: labelled inputs/selects/textareas, logo alt text,
decorative artwork hidden from assistive technology, current navigation/step,
keyboard Tab exposing a visible skip link and Enter moving focus into main,
visible focus outlines, named dialog and focus returning to its opener. Reduced
motion handling is preserved. Mobile sign-out stays reachable and coverage totals
remain visible. No console warnings/errors were captured during the final UI run.
This is targeted verification, not a formal full WCAG audit.

Calculated text contrast: cocoa/yellow 10.12:1; cocoa/cream 13.80:1;
muted/cream 7.12:1; cream/brown 8.46:1; success label 5.69:1;
warning label 6.20:1; error/white 6.91:1.

Screenshots are local repository evidence in `docs/evidence/text-monkey/`:
`dashboard-desktop.jpg`, `dashboard-mobile.jpg`, `login-desktop.jpg`,
`register-desktop.jpg`, `setup-preferences-desktop.jpg`,
`setup-checklist-desktop.jpg`, `setup-tablet.jpg`, `import-preview-desktop.jpg`,
`staged-contacts-desktop.jpg`, `settings-mobile.jpg`, `care-mobile.jpg`,
`volunteer-dialog-mobile.jpg`, and `legacy-admin-desktop.jpg`.
No native Library artifact was created.

## Parent integration

Worktree: `/Users/jacob/Documents/Codex/2026-10-02/task-4/text-monkey-brand`.
Branch: `codex/text-monkey-brand`, based on integration `e8e7eaa`.
Parent owns cherry-pick and deployment coordination after the live backend worker
finishes. The shared integration checkout/runtime was not edited or restarted.

Changes to shared existing files are narrowly located:

- `web/texty/public/app.js`: logo render helper, sign-in/welcome/copy, mobile
  logout button, main landmark and table presentation. Auth/refresh/submit/click
  handlers are untouched; retain newer worker handlers if a conflict arises.
- `web/texty/public/index.html`, `style.css`, `setup.js`: icons/fonts/palette,
  responsive presentation and accessible table-action heading.
- `app/web/texty.py`: one additive public brand router include, immediately before
  existing static routes. Retain any newer `/api/reply` capability/runtime work.
- `app/web/templates/base.html`: shared legacy visual presentation only.
- `app/main.py`: APP_NAME string only; retain newer runtime configuration.
- `tools/texty_local_demo.py`: public allowlisted brand-file serving and preview
  product label; its denied real API/write behavior remains unchanged.
- `web/texty/wrangler.jsonc`: APP_NAME string only. No deploy command was run.

Brand assets/router, evidence, documentation and the public-boundary test are
additive. Against committed integration `e8e7eaa` there are no conflicts because
this is the branch base. Recheck newer integration work at merge time. Do not
apply production SQL, change transport flags, send texts or restart the shared
app as part of integrating this presentation commit.
