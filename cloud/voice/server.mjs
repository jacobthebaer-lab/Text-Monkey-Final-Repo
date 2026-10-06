import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { Hold, authorized, Store, Connector } from './core.mjs';

export function readConfig(env = process.env) {
  const demoValue = env.GOOGLE_VOICE_DEMO_MODE ?? 'false';
  const enabledValue = env.VOICE_ENABLED ?? 'false';
  const signupValue = env.GOOGLE_VOICE_SIGNUP_ENABLED ?? 'false';
  const token = env.VOICE_API_TOKEN || '';
  const expectedEmail = env.VOICE_EXPECTED_EMAIL || '';
  const expectedPhone = env.VOICE_EXPECTED_NUMBER || '';
  const allowedPhones = [...new Set((env.GOOGLE_VOICE_ALLOWED_PHONES || '').split(',').map(s => s.trim()).filter(Boolean))];
  const pollSeconds = Number(env.VOICE_POLL_SECONDS || 30);
  const port = Number(env.PORT || 8765);
  if (!['true', 'false'].includes(signupValue) || !['true', 'false'].includes(demoValue) || !['true', 'false'].includes(enabledValue) || token.length < 32 ||
      allowedPhones.length > 20 || allowedPhones.some(p => !/^\+1[2-9]\d{9}$/.test(p)) ||
      !Number.isInteger(pollSeconds) || pollSeconds < 15 || pollSeconds > 3600 || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Hold('invalid_configuration');
  }
  const demoMode = demoValue === 'true';
  let testSessions = {};
  if (demoMode) {
    if (enabledValue !== 'true') throw new Hold('demo_transport_disabled');
    try { testSessions = JSON.parse(env.GOOGLE_VOICE_TEST_SESSIONS || '{}'); }
    catch { throw new Hold('invalid_demo_sessions'); }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(expectedEmail) || !/^\+1[2-9]\d{9}$/.test(expectedPhone) ||
        allowedPhones.includes(expectedPhone) ||
        !testSessions || typeof testSessions !== 'object' || Array.isArray(testSessions) ||
        Object.keys(testSessions).length !== allowedPhones.length ||
        Object.keys(testSessions).some(phone => !allowedPhones.includes(phone))) throw new Hold('invalid_demo_scope');
    const ids = new Set();
    for (const spec of Object.values(testSessions)) {
      if (!spec || !/^[a-f0-9]{32}$/.test(spec.id || '') || ids.has(spec.id) ||
          typeof spec.starts_at !== 'string' || typeof spec.expires_at !== 'string' ||
          !/(?:Z|[+-]\d{2}:\d{2})$/.test(spec.starts_at) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(spec.expires_at) ||
          !Number.isFinite(Date.parse(spec.starts_at)) || !Number.isFinite(Date.parse(spec.expires_at)) ||
          Date.parse(spec.expires_at) <= Date.parse(spec.starts_at) ||
          Date.parse(spec.expires_at) - Date.parse(spec.starts_at) > 7200000) throw new Hold('invalid_demo_sessions');
      ids.add(spec.id);
    }
  }
  // Ordinary and production startup retain the policy hold. Demo never polls itself.
  return { enabled: demoMode, demoMode, signupEnabled: demoMode && signupValue === 'true', testSessions, token, expectedEmail, expectedPhone, allowedPhones, pollSeconds, port,
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
      if (connector.policyHeld && ['/inbound', '/prepare', '/send', '/session', '/demo/intake', '/demo/recipients', '/demo/verify-profile', '/demo/signup-input'].includes(url.pathname)) {
        // Reject before even reading a session or message body.
        throw new Hold('provider_policy_hold', 503);
      }
      if (request.method === 'POST' && url.pathname === '/demo/verify-profile' && connector.demoMode) {
        const body = await jsonBody(request);
        if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length) throw new Hold('invalid_profile_verification_request', 400);
        return reply(200, await connector.verifyProfile());
      }
      if (request.method === 'POST' && url.pathname === '/demo/recipients' && connector.demoMode) {
        return reply(200, await connector.registerRecipient(await jsonBody(request)));
      }
      if (request.method === 'POST' && url.pathname === '/demo/reconcile' && connector.demoMode) {
        return reply(200, await connector.reconcile(await jsonBody(request)));
      }
      if (request.method === 'POST' && url.pathname === '/demo/recipient-probe' && connector.demoMode) {
        return reply(200, await connector.recipientProbe(await jsonBody(request)));
      }
      if (request.method === 'POST' && url.pathname === '/demo/recipient-observation' && connector.demoMode) {
        return reply(200, await connector.recipientObservation(await jsonBody(request)));
      }
      if (request.method === 'POST' && url.pathname === '/demo/presend-absence' && connector.demoMode) {
        return reply(200, await connector.presendAbsence(await jsonBody(request)));
      }
      if (request.method === 'POST' && url.pathname === '/demo/signup-input' && connector.demoMode) {
        return reply(200, connector.storedSignupInput(await jsonBody(request)));
      }
      if (request.method === 'POST' && url.pathname === '/demo/intake' && connector.demoMode) {
        const body = await jsonBody(request);
        if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => !['phone','phones'].includes(key)) ||
            ('phone' in body && (!/^\+1[2-9]\d{9}$/.test(body.phone) || 'phones' in body)) ||
            ('phones' in body && (!Array.isArray(body.phones) || !body.phones.length || body.phones.length > 200 ||
              new Set(body.phones).size !== body.phones.length || body.phones.some(p => typeof p !== 'string' || !/^\+1[2-9]\d{9}$/.test(p))))) throw new Hold('invalid_intake_request', 400);
        return reply(200, await connector.poll(body));
      }
      if (request.method === 'GET' && url.pathname === '/inbound') return reply(200, connector.inbound(url.searchParams.get('cursor') ?? '0'));
      if (request.method === 'POST' && url.pathname === '/prepare') return reply(200, await connector.prepare(await jsonBody(request)));
      if (request.method === 'POST' && url.pathname === '/send') return reply(200, await connector.send(await jsonBody(request)));
      if (request.method === 'POST' && url.pathname === '/session') {
        const result = await connector.session(await jsonBody(request));
        // Session route confirms identity; the next poll establishes readiness.
        if (!connector.demoMode) void connector.poll();
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
  let browser = null;
  let connector = policyHeldConnector();
  if (config.demoMode === true) {
    // No account navigation, inbox scan, reconnect resume or polling at startup.
    const store = new Store(config.directory);
    await store.load();
    const { VoiceBrowser } = await import('./browser.mjs');
    browser = new VoiceBrowser(config);
    try { await browser.start(); }
    catch (error) { await browser.close(); throw error; }
    connector = new Connector({ store, browser, ...config, demoMode: true });
  }
  const server = apiServer(connector, config.token);
  server.requestTimeout = 120000;
  server.headersTimeout = 10000;
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(config.port, '0.0.0.0', resolve);
  });
  let closing = null;
  return { server, close: () => closing ||= (async () => {
    const closed = new Promise(resolve => server.close(resolve));
    server.closeIdleConnections();
    // Closing Chromium first releases its persistent profile even if an HTTP
    // operation is still waiting on the page. Its durable pending send record
    // cannot be retried. Do not let request draining outlast profile cleanup.
    try { await browser?.close(); }
    finally { server.closeAllConnections(); await closed; }
  })() };
}
export function installShutdown(runtime, signals = process, exit = code => process.exit(code)) {
  let stopping = false;
  const shutdown = async () => {
    if (stopping) return;
    stopping = true;
    try { await runtime.close(); }
    finally { exit(0); }
  };
  signals.once('SIGTERM', shutdown); signals.once('SIGINT', shutdown);
}
export async function main() {
  const config = readConfig();
  const runtime = await startServer(config);
  installShutdown(runtime);
  process.stdout.write(config.demoMode ? 'Bounded Google Voice demo. Waiting for explicit private steps; no background polling.\n' : 'Google Voice provider policy hold. Private health endpoint available; no browser or account activity.\n');
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch(() => { process.stderr.write('Google Voice connector failed to start. Check configuration and private volume access.\n'); process.exitCode = 1; });
}
