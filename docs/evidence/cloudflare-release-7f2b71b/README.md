# Verified combined Cloudflare portal release

Live URL: https://text-monkey-demo.pages.dev/.
Immutable deployment: https://b02a5ac8.text-monkey-demo.pages.dev/.
Source: `7f2b71b429bc11bc4381feb250df78513d4f2e58` on `codex/complete-text-monkey`.
Pages project `text-monkey-demo`, production branch `demo`, deployment `b02a5ac8-b3ef-4879-9aac-a8011e417d52`.

Built from a clean archive of the released, remote-verified commit. All 37 frontend tests and 10 publication tests passed in that exported source. The initial export lacked test CSVs; adding those fixtures from the same commit made the complete frontend suite pass without changing source. No local dirty files, backend bindings or private data were uploaded.

Both the production alias and immutable deployment passed exact comparison of 15 UI files: core UI, accessibility/readiness modules, onboarding editor scripts/styles/page/defaults, public configuration and manifest. Live backend route remains unavailable (404); public configuration says disconnected/no real delivery. Cloudflare alias propagation briefly returned a missing new module, then both origins passed; the success receipts are recorded here. Font/image binaries are uploaded static brand assets and are not part of the UI text-file verifier.

Actual live browser verification: edit/save/full page reload, reload saved copy after unsaved edit, reset defaults/save/reload; original sample copy restored. At 320px the editor labels all fields, fits the viewport, and Tab from the last field reaches Save. The narrow roster has a named keyboard-focusable scroll region; ArrowRight scrolls horizontally with no page overflow. At 390px Settings fits and links to the editor. Incoming simulator/sample-send controls remain removed, offline action approvals disabled, and texting/Gloo/background status remains accurately disconnected. No browser warnings or errors captured.

`hosted-verification.json` is the raw automated live verification. `deployment.json` adds source/deployment identity, checks and screenshots. The four JPEGs show synthetic public-demo content only.

This supersedes the earlier portal-polish deployment and simulated-toggle evidence. Source improvements from parallel owners are included, but this static public release does not activate the private backend, scheduler or laptop Messages transport. Saving preview copy changes only browser-local fictional preferences; no real texts were sent by this publication task.
