const safeHeaders = {
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};
export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    let response;
    if (url.pathname.startsWith("/api/")) {
      if (env.BACKEND_URL) {
        const origin = req.headers.get("Origin");
        if (origin && origin !== url.origin)
          return Response.json(
            { error: "Cross-origin changes are not allowed." },
            { status: 403 },
          );
        const target = new URL(env.BACKEND_URL);
        target.pathname = url.pathname;
        target.search = url.search;
        const headers = new Headers(req.headers);
        headers.delete("cookie");
        headers.delete("host");
        headers.set("X-Texty-Bridge", env.BACKEND_BRIDGE_KEY || "");
        try {
          response = await fetch(target, {
            method: req.method,
            headers,
            body: ["GET", "HEAD"].includes(req.method) ? undefined : req.body,
            redirect: "manual",
            signal: AbortSignal.timeout(55000),
          });
        } catch {
          return Response.json(
            {
              error:
                "The demo backend is offline. Try the synthetic preview or restart the backend.",
            },
            { status: 503 },
          );
        }
      } else if (url.pathname === "/api/config")
        response = Response.json({
          name: "Text Monkey",
          connected: false,
          provider: "gloo",
          aiReady: false,
          liveSms: false,
          allowTextSignup: true,
        });
      else
        response = Response.json(
          {
            error:
              "The dashboard is published. Real sign-in and Gloo text processing need the backend connection.",
          },
          { status: 503 },
        );
    } else if (url.pathname.startsWith("/sms/"))
      return Response.json(
        {
          error:
            "Configure Twilio to use the Python backend /sms/inbound webhook.",
        },
        { status: 404 },
      );
    else response = await env.ASSETS.fetch(req);
    const out = new Response(response.body, response);
    for (const [k, v] of Object.entries(safeHeaders)) out.headers.set(k, v);
    if (url.pathname.startsWith("/api/"))
      out.headers.set("Cache-Control", "no-store");
    return out;
  },
};
