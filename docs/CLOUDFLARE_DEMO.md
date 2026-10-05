# Shareable Text Monkey demo

Live demo: https://text-monkey-demo.pages.dev/.

The Cloudflare Pages demo packages the same `web/texty/public` app. Visitors
open a clearly labeled roster and schedule preview without an account. Sample
volunteers and bookings are saved in the visitor's browser.
No real AI, messages, account creation, database or backend is connected.

Build a new upload directory:

```sh
python tools/build_cloudflare_demo.py /tmp/text-monkey-pages-upload
```

Deploy that directory to the dedicated `text-monkey-demo` Pages project, branch
`demo`, using the existing Cloudflare account. Only static files are uploaded;
there are no Pages Functions, worker bindings, secrets, paid services, or links
to the Mac backend. Existing connected Workers deployments remain separate.

From `web/texty`, publish an updated portal with:

```sh
npm run demo:build -- /tmp/text-monkey-pages-upload-NEW
npx wrangler@4.146.0 pages deploy /tmp/text-monkey-pages-upload-NEW --project-name=text-monkey-demo --branch=demo
```

Choose a fresh upload directory each time. `npm run demo:deploy -- DIRECTORY`
is the equivalent publication command after `npm ci`. The generic `deploy`
script targets the separate connected Worker; it does not update this Pages
demo. A localhost check alone does not establish publication.

After publication, verify the production alias against the exact upload:

```sh
npm run demo:verify -- --upload /tmp/text-monkey-pages-upload-NEW --receipt /tmp/text-monkey-hosted-verification.json
```

The verifier rejects localhost and unrelated sites. It compares all seven core
assets and every bundled script, stylesheet, HTML page and JSON file and public configuration byte-for-byte, checks the hosted security and
cache headers, and confirms that the public preview cannot read a live roster
API. Any stale file, redirect or changed configuration fails verification.
The receipt records asset hashes without account data or credentials.

Then check the live alias in a fresh browser tab: Home, roster filtering,
coverage changes, settings and responsive navigation. Keep the deployment ID
and immutable URL with the receipt. The incoming-text simulator, sample admin-send controls and fake texting toggles
have been removed. Real admin texts belong to the separately connected console.
The screenshots in `docs/evidence/portal-polish/` retain the earlier interface;
use `current-release.json` for the current publication receipt.

The build adds a public synthetic `/api/config`, security headers and a real
404 page for unavailable routes. It refuses existing output directories and
hidden files or symlinks. Credentials and server files are outside the upload.

A hosted connected administrator app still needs a supported always-on HTTPS
backend, its private bridge configuration, reviewed setup storage, and exact
Supabase confirmation/recovery redirects for the hosted origin. Those settings
are not changed by the static demo deployment. Existing confirmed administrator
identity and the allowlist must remain enforced; public signup metadata never
grants access or text consent.

## Connect the current Pages UI to an approved backend

To connect this same site, build with `--connected`. This preserves every
`web/texty/public` asset and packages the existing `web/texty/worker.js` as
Pages' `_worker.js`. Only `/api/*` and `/sms/*` invoke it. The static synthetic
configuration and redirect are removed so they cannot shadow the backend.

```sh
python tools/build_cloudflare_demo.py /tmp/text-monkey-pages-connected-NEW --connected
```

Save the active Pages deployment and private configuration for rollback first.
Set only `BACKEND_URL` and `BACKEND_BRIDGE_KEY` as production Pages secrets,
then upload the exact reviewed package to `text-monkey-demo`, branch `demo`.
Use the approved HTTPS backend. Update its `ADMIN_SITE_URL` and verify Supabase
confirmation/recovery redirects for `https://text-monkey-demo.pages.dev/`.
Never copy private environment files into the upload or invent an admin role.

The static `demo:verify` command intentionally rejects connected deployments.
For a connected release, compare every published UI asset against the upload,
verify `/api/config` reports the actual backend and preserved delivery holds,
and verify unauthenticated `/api/state` and `/api/auth/me` require sign-in.
Human sign-in and actual device delivery remain separate checks. The existing
synthetic preview stays available through the UI; connection grants no texting
authority and does not enable Google Voice, Planning Center or paid transport.
