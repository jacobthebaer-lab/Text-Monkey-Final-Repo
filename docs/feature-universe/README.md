# Text Monkey feature universe

A 3D product map with 183 sourced feature records in 15 systems and ten explorable decision paths. The inventory consolidates implementation, requirements, the original project brief and 16 accessible related chats. Status labels distinguish code presence, partial or held work, requested capabilities and superseded directions. Code presence never certifies production readiness or live texting.

## Live development site

https://text-monkey-universe-dev.jacobthebaer.workers.dev/

The dedicated Cloudflare Worker is `text-monkey-universe-dev`. Its static upload contains only the generated HTML and security headers. A separate KV namespace stores sanitized feature status; no messaging databases, account credentials, volunteer data or raw chat content are deployed.

The browser checks `/api/status` every 30 seconds and applies updates without resetting the current view, camera, selection or search. The display records when repository activity was checked. Network failure retains last-known information and shows a visible warning; data older than 90 minutes is stale.

GitHub Actions checks the integration branch on code pushes and pull-request lifecycle events, with an hourly fallback at minute 17. Scheduled jobs may be delayed by GitHub. The publisher runs in GitHub, so updates do not require the Mac or Codex to remain open. Live data means repository status, not remote access to other Codex chats or runtime delivery verification.

The workflow executes only trusted `codex/complete-text-monkey` code, never a pull request head. GitHub's read-only repository token stays inside GitHub. A separate secret authorizes only status-feed publication to this new Worker. The public site has no write controls or repository credential.

## Meaning of progress

- **Unchanged:** relevant source files match the recorded audit. The original implementation label remains.
- **Changed:** relevant source changed since its audit; verification is needed. A file change does not automatically promote a feature to complete.
- **In progress:** an open pull request touches the feature's source.
- **Blocked / verified:** explicitly recorded evidence-backed reviews, accepted only while their source revision remains applicable.

Implementation status and progress are separate. To change a feature's implementation label, update its evidence in `model.json` or add a reviewed entry to `reviews.json` after inspecting the implementation. Review notes are public summaries and must never include private conversations, credentials, phones or delivery receipts. Binding reviews to a source commit prevents later changes from silently inheriting stale verification.

## Reconciled inventory

[RECONCILIATION.md](RECONCILIATION.md) and [reconciliation.json](reconciliation.json) resolve all 183 records against immutable integration `8d8d0e4`. [AUDIT.md](AUDIT.md) and [audit.json](audit.json) record every feature's owner, repository paths, directly referenced baseline module results and explicit pending or not-applicable behavior checks. A source inventory check or passing module does not establish full feature acceptance.

Mounted signup pair review, learned-pattern tools, seasonal reports and local reviewed split coverage replace the old missing-hook descriptions. Actual credentials, configuration, reviews, qualifications, due timers, native PCO and delivery remain separate. Historical/future scope and human facts are preserved. All IDs, 347 mappings and the original change baseline stay unchanged; `sourceRevision` selects current source links. Static publication remains held for the deployment owner.

## Explore

Drag to orbit, scroll or use + / − to zoom. Choose **Fly**, then drag or use arrow keys to look, WASD to move, Q/E to descend/ascend, and Shift to boost. In flight mode + / − moves forward/backward. Click stars or use the searchable directory and feature matrix. **Decision paths** follows application rules without operating the actual application. **About this map** explains evidence and history-coverage limits. Press `/` to search, `H` for overview, or Escape to close details. Motion can be disabled and system reduced-motion preferences are respected.

[BUG_REPORT.md](BUG_REPORT.md) records concrete Universe fixes and scoped retests. Publication remains held.

## Build and validate

```sh
python3 docs/feature-universe/build.py
node --check docs/feature-universe/explorer.js
python3 -m unittest discover -s docs/feature-universe -p 'test_*.py'
node --test docs/feature-universe/worker.test.mjs docs/feature-universe/test_live_status.cjs docs/feature-universe/test_explorer.cjs
```

`model.json` is the editable feature/decision inventory. `source-coverage.json` accounts for all 347 original audit entries. The build verifies IDs, statuses, evidence, related-feature targets, branches, reachability, terminal outcomes and audit mappings. `index.html` remains an offline-capable snapshot; only the Cloudflare URL supplies live updates. Serve locally for preview with `python3 -m http.server 58139 --bind 127.0.0.1 --directory docs/feature-universe/dist`. A static local server correctly reports that live status is unavailable.

## Publish and configure

Deploy this Worker only, using the existing authenticated Cloudflare account:

```sh
npx wrangler@4.146.0 deploy --config docs/feature-universe/wrangler.jsonc
```

The repository secret `TEXT_MONKEY_UNIVERSE_PUBLISH_KEY` must match the Worker's `STATUS_PUBLISH_KEY`. The repository variable `TEXT_MONKEY_UNIVERSE_PUBLISH_URL` identifies the exact HTTPS `/api/status/publish` route. These are configured externally, never committed. Authenticated publication validates the feed and rejects older timestamps; the workflow's single concurrency group serializes publication. KV propagation can add a short delay after successful publication.

After deployment, verify the exact HTML bytes, response/security headers, feed feature count, and a successful GitHub status-publishing run. The existing Text Monkey demo and connected texting Worker remain separate.
