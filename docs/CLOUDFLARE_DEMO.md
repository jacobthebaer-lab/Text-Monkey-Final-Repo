# Shareable Text Monkey demo

The Cloudflare Pages demo packages the same `web/texty/public` app. Visitors
open a clearly labeled synthetic preview without an account. Sample volunteers,
text signup, cancellation review and bookings use browser-local sample rules.
No real AI, messages, account creation, database or backend is connected.

Build a new upload directory:

```sh
python tools/build_cloudflare_demo.py /tmp/text-monkey-pages-upload
```

Deploy that directory to the dedicated `text-monkey-demo` Pages project, branch
`demo`, using the existing Cloudflare account. Only static files are uploaded;
there are no Pages Functions, worker bindings, secrets, paid services, or links
to the Mac backend. Existing connected Workers deployments remain separate.

The build adds a public synthetic `/api/config`, security headers and a real
404 page for unavailable routes. It refuses existing output directories and
hidden files or symlinks. Credentials and server files are outside the upload.

A hosted connected administrator app still needs a supported always-on HTTPS
backend, its private bridge configuration, reviewed setup storage, and exact
Supabase confirmation/recovery redirects for the hosted origin. Those settings
are not changed by the static demo deployment. Existing confirmed administrator
identity and the allowlist must remain enforced; public signup metadata never
grants access or text consent.
