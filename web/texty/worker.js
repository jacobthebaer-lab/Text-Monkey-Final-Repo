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
    // API namespaces are literal route names. Reject ambiguous encodings before
    // proxying; downstream path IDs may still use ordinary percent encoding.
    const apiNamespace = url.pathname.startsWith("/api/") ? url.pathname.slice(5).split("/")[0] : null;
    if (apiNamespace !== null && (!apiNamespace || apiNamespace.includes("%")))
      return Response.json({error:"An unencoded API namespace is required."}, {status:404,headers:{"Cache-Control":"no-store"}});
    if (/^\/api\/planning-center(?:$|\/)/.test(url.pathname)) {
      const preview = url.pathname === "/api/planning-center/held-previews" && req.method === "POST";
      const review = /^\/api\/planning-center\/frequency-reviews\/[a-f0-9]{64}$/.test(url.pathname) && ["GET", "POST"].includes(req.method);
      const roleMapping = url.pathname === "/api/planning-center/role-bindings/catalogue" && req.method === "GET"
        || ["/api/planning-center/role-bindings/proposal", "/api/planning-center/role-bindings"].includes(url.pathname) && req.method === "POST";
      if ((!preview && !review && !roleMapping) || url.search)
        return Response.json({error:"Planning Center supports scoped comparison and local review only."}, {status:404,headers:{"Cache-Control":"no-store"}});
      if (!/^Bearer \S+$/i.test(req.headers.get("Authorization") || ""))
        return Response.json({error:"An authenticated admin session is required."}, {status:401,headers:{"Cache-Control":"no-store"}});
      if (req.method === "POST" && !/^application\/json(?:\s*;|$)/i.test(req.headers.get("Content-Type") || ""))
        return Response.json({error:"JSON review data is required."}, {status:415,headers:{"Cache-Control":"no-store"}});
    }
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
