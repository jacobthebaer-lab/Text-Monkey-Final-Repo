import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { Hold, authorized } from './core.mjs';

export function readConfig(env = process.env) {
  const enabledValue = env.VOICE_ENABLED ?? 'false';
  const token = env.VOICE_API_TOKEN || '';
  const expectedEmail = env.VOICE_EXPECTED_EMAIL || '';
  const expectedPhone = env.VOICE_EXPECTED_NUMBER || '';
  const allowedPhones = [...new Set((env.GOOGLE_VOICE_ALLOWED_PHONES || '').split(',').map(s => s.trim()).filter(Boolean))];
  const pollSeconds = Number(env.VOICE_POLL_SECONDS || 30);
  const port = Number(env.PORT || 8765);
  if (!['true', 'false'].includes(enabledValue) || token.length < 32 ||
      allowedPhones.length > 20 || allowedPhones.some(p => !/^\+1[2-9]\d{9}$/.test(p)) ||
      !Number.isInteger(pollSeconds) || pollSeconds < 15 || pollSeconds > 3600 || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Hold('invalid_configuration');
  }
  // Google Voice's AUP prohibits script/automatic messaging. Legacy enablement
  // flags cannot bypass this policy hold, even after identity verification.
  return { enabled: false, token, expectedEmail, expectedPhone, allowedPhones, pollSeconds, port,
    directory: env.VOICE_DATA_DIR || '/data', executablePath: env.VOICE_BROWSER_PATH };
}
async function jsonBody(request) {
  if (!/^application\/json(?:;|$)/i.test(request.headers['content-type'] || '')) throw new Hold('json_required', 415);
  let size = 0; const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new Hold('request_too_large', 413);
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new Hold('invalid_json', 400); }
}
export function apiServer(connector, token) {
  return createServer(async (request, response) => {
    response.setHeader('Content-Type', 'application/json');
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    const reply = (status, value) => { response.statusCode = status; response.end(JSON.stringify(value)); };
    try {
      if (!authorized(request.headers.authorization, token)) throw new Hold('unauthorized', 401);
      const url = new URL(request.url, 'http://connector.invalid');
      if (request.method === 'GET' && url.pathname === '/health') return reply(200, connector.health());
      if (connector.policyHeld && ['/inbound', '/prepare', '/send', '/session'].includes(url.pathname)) {
        // Reject before even reading a session or message body.
        throw new Hold('provider_policy_hold', 503);
      }
      if (request.method === 'GET' && url.pathname === '/inbound') return reply(200, connector.inbound(url.searchParams.get('cursor') ?? '0'));
      if (request.method === 'POST' && url.pathname === '/prepare') return reply(200, await connector.prepare(await jsonBody(request)));
      if (request.method === 'POST' && url.pathname === '/send') return reply(200, await connector.send(await jsonBody(request)));
      if (request.method === 'POST' && url.pathname === '/session') {
        const result = await connector.session(await jsonBody(request));
        // Session route confirms identity; the next poll establishes readiness.
        void connector.poll();
        return reply(200, result);
      }
      throw new Hold('not_found', 404);
    } catch (error) {
      // Playwright errors can contain page text/cookie input. Never serialize or
      // log exceptions, request bodies, URLs, phone numbers, or message text.
      reply(error instanceof Hold ? error.status : 503, { error: error instanceof Hold ? error.code : 'connector_unavailable' });
    }
  });
}
function policyHeldConnector() {
  const reject = () => { throw new Hold('provider_policy_hold', 503); };
  return { policyHeld: true, health: () => ({ ready: false, state: 'policy_hold', reason_code: 'provider_policy_hold',
    account_email: null, number: null, identity_verified: false, expected_identity_match: false,
    identity_fingerprint: null, baseline_at: null, inbound_cursor: '0', delivery_verified: false }),
    inbound: reject, prepare: reject, send: reject, session: reject };
}

export async function startServer(config) {
  // This is the only production startup path. Browser/Store/polling are not
  // instantiated; lower-level connector fixtures remain offline tests only.
  const server = apiServer(policyHeldConnector(), config.token);
  server.requestTimeout = 120000;
  server.headersTimeout = 10000;
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(config.port, '0.0.0.0', resolve);
  });
  return { server, close: async () => {
    await new Promise(resolve => server.close(resolve));
  } };
}
export async function main() {
  const config = readConfig();
  const runtime = await startServer(config);
  const shutdown = async () => {
    await runtime.close();
    process.exit(0);
  };
  process.once('SIGTERM', shutdown); process.once('SIGINT', shutdown);
  process.stdout.write('Google Voice provider policy hold. Private health endpoint available; no browser or account activity.\n');
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch(() => { process.stderr.write('Google Voice connector failed to start. Check configuration and private volume access.\n'); process.exitCode = 1; });
}
