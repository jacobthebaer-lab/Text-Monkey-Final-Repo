import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { readConfig, startServer } from '../server.mjs';
import { VoiceBrowser } from '../browser.mjs';
import { Store } from '../core.mjs';

test('default startup serves private policy-hold health without browser, state or account polling', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'text-monkey-disabled-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const token = 'synthetic-disabled-token'.padEnd(32, '0');
  const config = readConfig({ VOICE_API_TOKEN: token, VOICE_DATA_DIR: join(directory, 'unused-profile') });
  // Ephemeral test binding; deployed configuration still requires port 1–65535.
  t.mock.method(VoiceBrowser.prototype, 'start', () => assert.fail('Policy hold must not launch a browser'));
  t.mock.method(VoiceBrowser.prototype, 'identity', () => assert.fail('Policy hold must not read an account'));
  t.mock.method(Store.prototype, 'load', () => assert.fail('Policy hold must not read persistent data'));
  const runtime = await startServer({ ...config, port: 0 });
  t.after(() => runtime.close());
  const base = `http://127.0.0.1:${runtime.server.address().port}`;
  assert.equal((await fetch(`${base}/health`)).status, 401);
  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
  const response = await fetch(`${base}/health`, { headers });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  const health = await response.json();
  assert.equal(health.ready, false);
  assert.equal(health.state, 'policy_hold');
  assert.equal(health.reason_code, 'provider_policy_hold');
  assert.equal(health.identity_verified, false);
  assert.equal(health.delivery_verified, false);
  assert.equal(health.baseline_at, null);
  for (const path of ['/prepare', '/send', '/session']) {
    const blocked = await fetch(base + path, { method: 'POST', headers, body: '{}' });
    assert.equal(blocked.status, 503);
    assert.deepEqual(await blocked.json(), { error: 'provider_policy_hold' });
    const unparsed = await fetch(base + path, { method: 'POST', headers, body: 'not JSON' });
    assert.equal(unparsed.status, 503);
    assert.deepEqual(await unparsed.json(), { error: 'provider_policy_hold' });
  }
  const inbound = await fetch(`${base}/inbound`, { headers });
  assert.equal(inbound.status, 503);
  assert.deepEqual(await inbound.json(), { error: 'provider_policy_hold' });
  assert.deepEqual(await readdir(directory), [], 'Policy hold must not read or create a browser profile/ledger');
  assert.deepEqual(await (await fetch(`${base}/health`, { headers })).json(), health);
});

test('enable flags and configured identities cannot bypass the policy hold or access existing state', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'text-monkey-enabled-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const config = readConfig({ VOICE_API_TOKEN: 'synthetic-enabled-token'.padEnd(32, '0'), VOICE_ENABLED: 'true',
    GOOGLE_VOICE_ENABLED: 'true', LIVE_SMS: 'true',
    VOICE_EXPECTED_EMAIL: 'enabled-proof@example.invalid', VOICE_EXPECTED_NUMBER: '+12025550100',
    VOICE_DATA_DIR: directory, VOICE_BROWSER_PATH: '/nonexistent/policy-hold-chromium' });
  assert.equal(config.enabled, false);
  const existing = 'Synthetic unreadable state. A policy hold must leave it alone.\n';
  await writeFile(join(directory, 'state.json'), existing);
  t.mock.method(VoiceBrowser.prototype, 'start', () => assert.fail('Flags must not launch a browser'));
  t.mock.method(VoiceBrowser.prototype, 'identity', () => assert.fail('Flags must not read an account'));
  t.mock.method(Store.prototype, 'load', () => assert.fail('Flags must not read persistent data'));
  // Even bypassing readConfig with a direct enabled property cannot activate it.
  const runtime = await startServer({ ...config, enabled: true, port: 0 });
  t.after(async () => { if (runtime.server.listening) await runtime.close(); });
  const response = await fetch(`http://127.0.0.1:${runtime.server.address().port}/health`, {
    headers: { Authorization: `Bearer ${config.token}` },
  });
  assert.equal(response.status, 200);
  const health = await response.json();
  assert.equal(health.ready, false);
  assert.equal(health.reason_code, 'provider_policy_hold');
  const rejected = await fetch(`http://127.0.0.1:${runtime.server.address().port}/send`, {
    method: 'POST', headers: { Authorization: `Bearer ${config.token}` }, body: 'not parsed',
  });
  assert.equal(rejected.status, 503);
  assert.deepEqual(await rejected.json(), { error: 'provider_policy_hold' });
  assert.deepEqual(await readdir(directory), ['state.json']);
  assert.equal(await readFile(join(directory, 'state.json'), 'utf8'), existing);
  await runtime.close();
});
