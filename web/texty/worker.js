const safeHeaders = {
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};
const offlineResponse = () => Response.json(
  {error: "Text Monkey is temporarily offline. Please try again shortly."},
  {status: 503, headers: {...safeHeaders, "Cache-Control": "no-store"}},
);
function calendarCallbackLocation(req, url, response) {
  if (req.method !== "GET" || url.pathname !== "/api/google-calendar/callback" || response.status !== 303)
    return null;
  const location = response.headers.get("Location");
  const origins = new Set(["https://text-monkey-demo.pages.dev"]);
  if (url.protocol === "https:") origins.add(url.origin);
  // Compare exact destinations, rejecting credentials, queries, encoded paths,
  // whitespace and URL-parser normalization before forwarding this one redirect.
  for (const origin of origins)
    for (const path of ["", "/", "/texty"])
      for (const result of ["ready", "denied"])
        if (location === `${origin}${path}#google-calendar=${result}`)
          return origin === "https://text-monkey-demo.pages.dev" && path === "/texty"
            ? `${origin}/#google-calendar=${result}` : location;
  return null;
}
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
          // A tunnel error page is not an API authentication rejection. Do not
          // expose upstream details or cause the client to replay a failed write.
          if (response.status >= 500 || (response.status >= 300 && response.status < 400)) {
            const location = calendarCallbackLocation(req, url, response);
            response = location
              ? new Response(null, {status: 303, headers: {Location: location}})
              : offlineResponse();
          } else if (![204, 205].includes(response.status)) {
            if (!/^application\/(?:[\w.+-]+\+)?json(?:\s*;|$)/i.test(response.headers.get("Content-Type") || "")) {
              response = offlineResponse();
            } else if (req.method !== "HEAD") {
              // Validate without consuming or rewriting legitimate API JSON,
              // including exact 401/403 statuses and auth-invalid headers.
              await response.clone().json();
            }
          }
        } catch {
          response = offlineResponse();
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
      else response = offlineResponse();
    } else if (url.pathname.startsWith("/sms/"))
      return Response.json(
        {
          error:
            "This texting route is unavailable.",
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
