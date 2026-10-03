import { createServer } from 'node:http';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { Connector, Store, Hold, authorized } from './core.mjs';
import { VoiceBrowser } from './browser.mjs';

export function readConfig(env = process.env) {
  const token = env.VOICE_API_TOKEN || '';
  const expectedEmail = env.VOICE_EXPECTED_EMAIL || '';
  const expectedPhone = env.VOICE_EXPECTED_NUMBER || '';
  const allowedPhones = [...new Set((env.GOOGLE_VOICE_ALLOWED_PHONES || '').split(',').map(s => s.trim()).filter(Boolean))];
  const pollSeconds = Number(env.VOICE_POLL_SECONDS || 30);
  const port = Number(env.PORT || 8765);
  if (token.length < 32 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(expectedEmail) || !/^\+1[2-9]\d{9}$/.test(expectedPhone) ||
      allowedPhones.length > 20 || allowedPhones.some(p => !/^\+1[2-9]\d{9}$/.test(p)) ||
      !Number.isInteger(pollSeconds) || pollSeconds < 15 || pollSeconds > 3600 || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Hold('invalid_configuration');
  }
  return { token, expectedEmail, expectedPhone, allowedPhones, pollSeconds, port,
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
export async function main() {
  const config = readConfig();
  await mkdir(config.directory, { recursive: true, mode: 0o700 });
  const browser = new VoiceBrowser(config);
  // Chromium owns an exclusive persistent-profile lock before state is read.
  // Running a second container against this volume fails before mutating it.
  await browser.start();
  const store = new Store(config.directory);
  try { await store.load(); } catch (error) { await browser.close(); throw error; }
  const connector = new Connector({ ...config, store, browser });
  const server = apiServer(connector, config.token);
  server.requestTimeout = 120000;
  server.headersTimeout = 10000;
  server.listen(config.port, '0.0.0.0');
  let polling = false;
  const poll = async () => {
    if (polling) return;
    polling = true;
    try { await connector.poll(); } finally { polling = false; }
  };
  void poll();
  const timer = setInterval(() => void poll(), config.pollSeconds * 1000);
  const shutdown = async () => {
    clearInterval(timer); server.close();
    await browser.close();
    process.exit(0);
  };
  process.once('SIGTERM', shutdown); process.once('SIGINT', shutdown);
  process.stdout.write('Google Voice connector started. Session verification required.\n');
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch(() => { process.stderr.write('Google Voice connector failed to start. Check configuration and private volume access.\n'); process.exitCode = 1; });
}
