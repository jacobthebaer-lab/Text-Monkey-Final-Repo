# Text Monkey brand and repository handoff

The completed shared code is pushed to the [original private repository](https://github.com/clementsnc/planning-center-but-better/tree/codex/complete-text-monkey), branch `codex/complete-text-monkey`, at `9ed9d71203ed86989455e81a6fca2717a8017682`. Repository identity: `1400920077`. This is the existing collaboration repository; no replacement or fork was created. The default branch is still `main`; the completed demo is on the linked working branch.

Jacob deferred the repository rename. Its current name is accurate in clone links and handoff instructions. The application is named **Text Monkey**. This audit is a small local follow-up for the Git integration owner to collect, not an additional remote push or deployment.

## Brand audit, October 3, 2026

- README heading, app/health name, admin page titles, public portal title, manifest names, logo text and generated preview/404 labels use Text Monkey.
- npm package and lock metadata agree on `text-monkey-admin`; the package description now identifies Text Monkey explicitly.
- A fresh static build preserves all 22 brand files byte-for-byte. Generated configuration names Text Monkey and disables connected accounts, live AI, live SMS and scheduling.
- Read-only HTTPS verification confirms the production alias [text-monkey-demo.pages.dev](https://text-monkey-demo.pages.dev/) matches all six current core asset files and the synthetic configuration. Its backend roster endpoint returns 404. No upload, account change or real message occurred.
- Focused brand/static-asset/preview/publication checks: **10 backend tests passed**. Actual public-demo and session controllers: **2 frontend tests passed**. Metadata, generated-output, preserved-header and whitespace checks passed.

The public site is a static fictional preview. This audit does not establish current Messages delivery, scheduler uptime, live Gloo readiness or connected administrator access.

## Screenshot and runbook boundaries

The inspected desktop/mobile Home captures in `docs/evidence/portal-polish/` display the Text Monkey logo and palette. They still show historical simulated-texting status from the earlier interface. The inspected sign-in and legacy-admin captures in `docs/evidence/text-monkey/` also use Text Monkey, but include earlier preview/simulator controls. Preserve these as historical visual evidence; they do not depict the current removal of those controls.

Use [current-release.json](evidence/portal-polish/current-release.json) for the current public release and the [publication runbook](CLOUDFLARE_DEMO.md) for verification. The runbook now states the simulator, sample-send controls and fake texting toggles have been removed. Earlier coordination entries are work history; their pending-push language no longer describes the completed checkpoint.

## Compatibility identifiers

Existing `web/texty/`, `/texty`, schema and storage keys, scoped session markers, deployed Worker identifiers and `X-Texty-Bridge` are compatibility contracts. They are retained. The backend and Worker both use that exact bridge header; descriptive prose must not rename it. Third-party Planning Center integration references identify the external service, not Text Monkey's product name. Historical commits and evidence remain unchanged.

## Optional later rename: repository owner/admin only

No rename is needed to use the completed shared branch. The connected account has push access but no repository-admin access, and GitHub rejected its earlier rename request. Do not repeat that request with the same access or change repository visibility.

When the repository owner/admin chooses to perform Jacob's deferred rename, rename this same repository to `text-monkey` in [repository settings](https://github.com/clementsnc/planning-center-but-better/settings), or use their authorized GitHub CLI connection:

```sh
gh repo rename text-monkey --repo clementsnc/planning-center-but-better
gh api repos/clementsnc/text-monkey --jq '{id,full_name,private}'
```

Verify the returned ID remains `1400920077`, the name is `clementsnc/text-monkey`, private visibility is retained, and collaborators/branches remain present. The resulting canonical URL should then be `https://github.com/clementsnc/text-monkey`. Only after verification, update each collaborator's existing clone:

```sh
git remote set-url origin https://github.com/clementsnc/text-monkey.git
```

Preserve the repository, history, branches and collaborator access. Do not create a similarly named replacement repository. GitHub integration/remote pushes remain with the GitHub handoff for Clyde owner.
