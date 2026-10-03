import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Connector, Store, Hold, hash, validateSend, validateCookies, authorized } from '../core.mjs';
import { absoluteTimestamp, normalizeBubbles, VoiceBrowser } from '../browser.mjs';
import { apiServer, readConfig } from '../server.mjs';

const email = 'demo@example.test', number = '+12025550101', recipient = '+12025550102';
const request = { idempotency_key: 'session:message-001', to: recipient, body: 'An authorized synthetic test.', not_after: '2026-10-03T12:00:30Z' };
const baseline = '2026-10-03T12:00:00.000Z';
async function fixture(t, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'text-monkey-voice-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = new Store(directory); await store.load();
  const browser = { messages: [], sends: 0, prepares: 0, clear: 0,
    async identity() { return { email, phone: number }; },
    async scan() { return this.messages; },
    async prepareSend() { this.prepares++; },
    async submitSend() { this.sends++; return { status: 'submitted', reason_code: 'visible_in_google_voice' }; },
    async importSession() {}, async clearSession() { this.clear++; },
  };
  const config = { store, browser, expectedEmail: email, expectedPhone: number, allowedPhones: [recipient], now: () => baseline, ...options };
  const connector = new Connector(config);
  return { directory, store, browser, connector, config };
}
test('startup history is baselined; new intake deduplicates across restart and rejects late history', async t => {
  const { connector, browser, store, config, directory } = await fixture(t);
  browser.messages = [{ id: 'history', phone: recipient, body: 'Old', received_at: '2026-10-01T12:00:00.000Z' }];
  assert.equal((await connector.poll()).ready, true);
  assert.deepEqual(connector.inbound().messages, []);
  browser.messages.push({ id: 'new', phone: recipient, body: 'New', received_at: '2026-10-03T12:00:10.000Z' });
  await connector.poll(); await connector.poll();
  assert.equal(connector.inbound().messages.length, 1);
  assert.equal(connector.inbound().cursor, '1');
  const restartedStore = new Store(directory); await restartedStore.load();
  const restarted = new Connector({ ...config, store: restartedStore });
  browser.messages.push({ id: 'late-history', phone: recipient, body: 'Older', received_at: '2026-09-01T12:00:00.000Z' });
  browser.messages.push({ id: 'unallowed', phone: '+12025550103', body: 'Private', received_at: '2026-10-03T12:00:10.000Z' });
  await restarted.poll();
  assert.equal(restarted.inbound().messages.length, 1);
  assert.equal(restarted.inbound('1').messages.length, 0);
  assert.equal((await stat(store.path)).mode & 0o777, 0o600);
});
test('parallel duplicate sends reserve once, survive restart and reject changed payload', async t => {
  const { connector, browser, directory, config } = await fixture(t);
  await connector.poll();
  assert.equal((await connector.prepare(request)).status, 'prepared');
  const results = await Promise.all([connector.send(request), connector.send(request)]);
  assert.deepEqual(results[0], results[1]); assert.equal(browser.sends, 1);
  const store = new Store(directory); await store.load();
  const restarted = new Connector({ ...config, store });
  assert.deepEqual(await restarted.send(request), results[0]); assert.equal(browser.sends, 1);
  await assert.rejects(() => restarted.send({ ...request, body: 'Changed body' }), { code: 'idempotency_conflict' });
  const raw = await readFile(store.path, 'utf8');
  assert.equal(raw.includes(request.body), false); assert.equal(raw.includes(request.to), false);
});
test('crash reservation and ambiguous browser errors never auto-resend', async t => {
  const { store, browser, config, directory } = await fixture(t);
  store.data.sends[hash(request.idempotency_key)] = {
    digest: hash(`${request.to}\0${request.body}\0${request.not_after}`), status: 'pending', created_at: baseline,
  };
  await store.save();
  const restartedStore = new Store(directory); await restartedStore.load();
  const connector = new Connector({ ...config, store: restartedStore });
  const result = await connector.send(request);
  assert.deepEqual(result, { status: 'uncertain', reason_code: 'restart_during_send' });
  assert.equal(browser.sends, 0);
  await connector.poll();
  browser.submitSend = async () => { browser.sends++; throw new Error('secret cookie, private body'); };
  const other = { ...request, idempotency_key: 'session:message-002' };
  await connector.prepare(other);
  assert.equal((await connector.send(other)).status, 'uncertain');
  assert.equal((await connector.send(other)).status, 'uncertain');
  assert.equal(browser.sends, 1);
  assert.equal(connector.health().ready, false);
});
test('account identity mismatch rejects before message access or send and health is masked', async t => {
  const { connector, browser } = await fixture(t);
  await connector.poll();
  const health = connector.health();
  assert.equal(health.identity_fingerprint, hash(`${email}\n${number}`));
  assert.equal(health.number, '***0101'); assert.equal(health.account_email, 'd***@example.test');
  browser.identity = async () => ({ email: 'other@example.test', phone: number });
  const result = await connector.prepare(request);
  assert.equal(result.status, 'rejected'); assert.equal(result.reason_code, 'account_mismatch');
  assert.equal(browser.prepares, 0); assert.equal(browser.sends, 0);
  assert.equal(connector.health().identity_fingerprint, null);
});
test('cookie import verifies identity and clears a mismatched session', async t => {
  const { connector, browser } = await fixture(t);
  browser.identity = async () => ({ email, phone: '+12025550104' });
  await assert.rejects(() => connector.session({ cookies: [{ name: 'SID', value: 'secret', domain: '.google.com', path: '/' }] }), { code: 'account_mismatch' });
  assert.equal(browser.clear, 1); assert.equal(connector.health().ready, false);
});
test('validation limits and allowlist prevent malformed, foreign and unapproved requests', async t => {
  assert.throws(() => validateSend({ ...request, to: '+442071234567' }), { code: 'invalid_send_request' });
  assert.throws(() => validateSend({ ...request, body: ' ' }), { code: 'invalid_send_request' });
  assert.throws(() => validateSend({ ...request, body: 'a'.repeat(1601) }), { code: 'invalid_send_request' });
  assert.throws(() => validateSend({ ...request, not_after: undefined }), { code: 'invalid_send_request' });
  assert.throws(() => validateSend({ ...request, not_after: '2026-10-03T12:01:00' }), { code: 'invalid_send_request' });
  assert.throws(() => validateCookies({ cookies: [{ name: 'SID', value: 'x', domain: 'evil.google.com.attacker.test', path: '/' }] }), { code: 'invalid_cookie' });
  assert.throws(() => validateCookies({ cookies: [], password: 'secret' }), { code: 'invalid_session_request' });
  assert.equal(authorized('Bearer ' + 'x'.repeat(32), 'x'.repeat(32)), true);
  assert.equal(authorized('Bearer wrong', 'x'.repeat(32)), false);
  const { connector, browser } = await fixture(t);
  await connector.poll();
  assert.equal((await connector.prepare({ ...request, to: '+12025550103' })).reason_code, 'recipient_not_allowed');
  assert.equal(browser.prepares, 0);
  assert.throws(() => connector.inbound('-1'), { code: 'invalid_cursor' });
  assert.throws(() => connector.inbound('9'), { code: 'invalid_cursor' });
});
test('absolute timestamps and repeated same-minute messages produce stable distinct IDs', () => {
  assert.throws(() => absoluteTimestamp(['Today 9:12 AM']), { code: 'message_timestamp_unavailable' });
  assert.equal(absoluteTimestamp(['2026-10-03T12:00:00Z']), baseline);
  const row = { incoming: true, directionKnown: true, text: 'Yes', timestamps: [baseline], providerId: '' };
  const messages = normalizeBubbles([row, row], `t.${recipient}`, recipient);
  assert.notEqual(messages[0].id, messages[1].id);
  assert.deepEqual(messages, normalizeBubbles([row, row], `t.${recipient}`, recipient));
  assert.equal(normalizeBubbles([{ ...row, text: '  exact body\n' }], `t.${recipient}`, recipient)[0].body, '  exact body\n');
});
test('HTTP API requires bearer and never returns exception secrets', async t => {
  const { connector, browser } = await fixture(t);
  const token = 'x'.repeat(32), server = apiServer(connector, token);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const base = `http://127.0.0.1:${server.address().port}`;
  assert.equal((await fetch(`${base}/health`)).status, 401);
  const headers = { authorization: `Bearer ${token}`, 'content-type': 'application/json' };
  await connector.poll();
  const prepare = await fetch(`${base}/prepare`, { method: 'POST', headers, body: JSON.stringify(request) });
  assert.equal(prepare.status, 200); assert.equal((await prepare.json()).status, 'prepared');
  const send = await fetch(`${base}/send`, { method: 'POST', headers, body: JSON.stringify(request) });
  assert.equal(send.status, 200); assert.equal((await send.json()).status, 'submitted');
  browser.importSession = async () => { throw new Error('SID=DO-NOT-EXPOSE'); };
  const response = await fetch(`${base}/session`, { method: 'POST', headers,
    body: JSON.stringify({ cookies: [{ name: 'SID', value: 'DO-NOT-EXPOSE', domain: '.google.com', path: '/' }] }) });
  assert.equal(response.status, 503); assert.equal((await response.text()).includes('DO-NOT-EXPOSE'), false);
  const health = await fetch(`${base}/health`, { headers });
  assert.equal(health.headers.get('cache-control'), 'no-store');
  assert.equal((await health.json()).ready, false);
});
test('configuration requires identity and private API token, empty allowlist is supported', () => {
  assert.throws(() => readConfig({}), { code: 'invalid_configuration' });
  const config = readConfig({ VOICE_API_TOKEN: 'x'.repeat(32), VOICE_EXPECTED_EMAIL: email, VOICE_EXPECTED_NUMBER: number });
  assert.deepEqual(config.allowedPhones, []);
  assert.equal(config.port, 8765);
});
test('authorization expiry before or during preparation is durable and never submits', async t => {
  let currentTime = baseline;
  const { connector, browser } = await fixture(t, { now: () => currentTime });
  await connector.poll();
  const expired = { ...request, not_after: baseline };
  assert.equal((await connector.prepare(expired)).reason_code, 'authorization_expired');
  assert.equal(browser.prepares, 0);
  await assert.rejects(() => connector.send(request), { code: 'idempotency_conflict' });
  browser.prepareSend = async () => { browser.prepares++; currentTime = '2026-10-03T12:02:00.000Z'; };
  const expiresDuringPrepare = { ...request, idempotency_key: 'session:message-003' };
  assert.equal((await connector.prepare(expiresDuringPrepare)).reason_code, 'authorization_expired');
  assert.equal(browser.sends, 0);
  assert.equal((await connector.send(expiresDuringPrepare)).reason_code, 'authorization_expired');
  assert.equal(browser.prepares, 1);
});
test('browser checks authorization deadline immediately before clicking', async () => {
  const browser = new VoiceBrowser({ directory: '/unused', allowedPhones: [] });
  let clicks = 0;
  browser.page = { url: () => `https://voice.google.com/u/0/messages?itemId=${encodeURIComponent(`t.${recipient}`)}`,
    locator: () => ({ count: async () => 1, inputValue: async () => request.body,
      isEnabled: async () => true, click: async () => { clicks++; } }) };
  browser.prepared = { to: recipient, body: request.body, before: 0 };
  const result = await browser.submitSend(recipient, request.body, '2000-01-01T00:00:00Z');
  assert.equal(result.reason_code, 'authorization_expired');
  assert.equal(clicks, 0);
});
test('all em-dash forms reject before preparation, idempotency replay or click without rewriting', async t => {
  const { connector, browser, store } = await fixture(t);
  await connector.poll();
  for (const punctuation of ['\u2014', '\ufe31', '\ufe58', '\u2e3a', '\u2e3b']) {
    const bad = Object.freeze({ ...request, body: `Hello${punctuation}there` });
    store.data.sends[hash(bad.idempotency_key)] = { status: 'submitted',
      digest: hash(`${bad.to}\0${bad.body}\0${bad.not_after}`) };
    assert.throws(() => validateSend(bad), { code: 'outbound_em_dash_forbidden' });
    await assert.rejects(() => connector.prepare(bad), { code: 'outbound_em_dash_forbidden' });
    await assert.rejects(() => connector.send(bad), { code: 'outbound_em_dash_forbidden' });
    const native = new VoiceBrowser({ directory: '/unused', allowedPhones: [] });
    native.prepared = { to: bad.to, body: bad.body, before: 0 };
    await assert.rejects(() => native.prepareSend(bad.to, bad.body), { code: 'outbound_em_dash_forbidden' });
    await assert.rejects(() => native.submitSend(bad.to, bad.body, bad.not_after), { code: 'outbound_em_dash_forbidden' });
    assert.equal(bad.body, `Hello${punctuation}there`);
  }
  assert.equal(browser.prepares, 0); assert.equal(browser.sends, 0);
  const permitted = { ...request, body: 'Hyphen-separated, 10–11. Exact text.' };
  assert.equal(validateSend(permitted).body, permitted.body);
});
test('two-phase preparation preserves the UI, requires exact authorization and expires safely', async t => {
  let currentTime = baseline;
  const { connector, browser } = await fixture(t, { now: () => currentTime });
  await connector.poll();
  assert.equal((await connector.send(request)).reason_code, 'send_not_prepared');
  assert.equal(browser.prepares, 0); assert.equal(browser.sends, 0);
  assert.equal((await connector.prepare({ ...request, not_after: '2026-10-03T12:02:00Z' })).reason_code, 'authorization_window_too_long');
  assert.equal((await connector.prepare(request)).status, 'prepared');
  browser.identity = async () => { throw new Error('poll must not navigate while prepared'); };
  assert.equal((await connector.poll()).ready, true);
  assert.equal((await connector.prepare(request)).status, 'prepared');
  const next = { ...request, idempotency_key: 'session:message-002' };
  assert.equal((await connector.prepare(next)).reason_code, 'preparation_in_progress');
  await assert.rejects(() => connector.send({ ...request, body: 'Changed' }), { code: 'idempotency_conflict' });
  assert.equal((await connector.send(request)).status, 'submitted');
  assert.equal(browser.prepares, 1); assert.equal(browser.sends, 1);
  browser.identity = async () => ({ email, phone: number });
  await connector.prepare(next);
  currentTime = '2026-10-03T12:00:31Z';
  assert.equal((await connector.send(next)).reason_code, 'authorization_expired');
  assert.equal(browser.sends, 1);
  await connector.poll();
  assert.equal(connector.preparation, null);
});
test('submission rereads the current recipient route and exact composer text before clicking', async () => {
  const browser = new VoiceBrowser({ directory: '/unused', allowedPhones: [] });
  let clicks = 0, currentRecipient = recipient, currentBody = request.body;
  browser.page = {
    url: () => `https://voice.google.com/u/0/messages?itemId=${encodeURIComponent(`t.${currentRecipient}`)}`,
    locator: () => ({ count: async () => 1, inputValue: async () => currentBody,
      isEnabled: async () => true, click: async () => { clicks++; } }),
  };
  const check = async () => {
    browser.prepared = { to: recipient, body: request.body, before: 0 };
    return browser.submitSend(recipient, request.body, new Date(Date.now() + 30000).toISOString());
  };
  currentRecipient = '+12025550103';
  assert.equal((await check()).reason_code, 'recipient_not_verified');
  currentRecipient = recipient; currentBody = 'Unexpected replacement text';
  assert.equal((await check()).reason_code, 'composer_changed');
  currentBody = `${request.body}\u2014`;
  assert.equal((await check()).reason_code, 'composer_changed');
  assert.equal(clicks, 0);
  currentBody = request.body;
  browser.page.waitForFunction = async () => {};
  browser.rows = async () => [{ incoming: false, text: request.body, failed: false }];
  assert.equal((await check()).status, 'submitted');
  assert.equal(clicks, 1);
});
