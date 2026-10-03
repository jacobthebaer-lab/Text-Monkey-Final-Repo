// Invoked only by tools/cloud_voice_container_proof.sh in two separate offline
// containers sharing its disposable private volume. This is not a Google test.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { VoiceBrowser } from '../browser.mjs';
import { Connector, Store, hash } from '../core.mjs';

assert.equal(process.env.CLOUD_PROOF_RESTART_FIXTURE, 'synthetic');
assert.notEqual(process.getuid(), 0);
const phase = process.argv[2];
assert.ok(['reserve', 'recover'].includes(phase));
const baseline = '2026-10-03T12:00:00.000Z';
const email = 'restart-proof@example.invalid', phone = '+12025550100', recipient = '+12025550102';
const request = { idempotency_key: 'restart-proof:reserved-001', to: recipient,
  body: 'Synthetic restart proof only.', not_after: '2026-10-03T12:00:30.000Z' };
const historical = { id: 'synthetic-history', phone: recipient, body: 'Old synthetic intake.',
  received_at: '2026-10-03T11:00:00.000Z' };
const incoming = { id: 'synthetic-new', phone: recipient, body: 'New synthetic intake.',
  received_at: '2026-10-03T12:00:01.000Z' };
const browser = new VoiceBrowser({ directory: '/data', executablePath: '/usr/bin/chromium', allowedPhones: [] });

try {
  await browser.start();
  assert.equal(browser.page.url(), 'about:blank');
  assert.deepEqual(await browser.context.cookies(), []);
  await browser.page.setContent('<textarea aria-label="Synthetic body"></textarea><button disabled>Synthetic send</button>');
  await browser.page.evaluate(() => {
    window.syntheticClicks = 0;
    document.querySelector('button').addEventListener('click', () => window.syntheticClicks++);
  });
  const store = new Store('/data');
  await store.load();
  const connector = new Connector({ store, browser, expectedEmail: email, expectedPhone: phone,
    allowedPhones: [recipient], now: () => baseline });

  if (phase === 'reserve') {
    assert.equal(store.data.baseline_at, null, 'Proof requires a newly created empty volume');
    assert.equal(Object.keys(store.data.sends).length, 0);
    // Inject synthetic identity/intake only. Real Chromium renders a local
    // about:blank fixture; no Voice navigation, login or send method is used.
    browser.identity = async () => ({ email, phone });
    browser.scan = async () => [historical];
    await connector.poll();
    assert.deepEqual(connector.inbound().messages, []);
    browser.scan = async () => [historical, incoming];
    await connector.poll();
    browser.prepareSend = async (to, body) => {
      assert.equal(to, recipient);
      await browser.page.locator('textarea').fill(body);
      assert.equal(browser.page.url(), 'about:blank');
    };
    browser.submitSend = async () => {
      // Real Connector.send has already fsynced its reservation. Interrupt the
      // process before any click or terminal outcome is recorded. Close Chromium
      // cleanly so this isolates connector recovery from browser crash recovery.
      const disk = JSON.parse(await readFile(store.path, 'utf8'));
      assert.equal(disk.sends[hash(request.idempotency_key)].status, 'pending');
      assert.equal(await browser.page.evaluate(() => window.syntheticClicks), 0);
      console.log('PASS: real connector persisted its reservation before the interrupted submission boundary; zero clicks');
      await browser.close();
      process.exit(75);
    };
    assert.equal((await connector.prepare(request)).status, 'prepared');
    await connector.send(request);
    assert.fail('Reservation fixture must terminate before Connector.send records an outcome');
  }

  const healthBefore = connector.health();
  assert.equal(healthBefore.ready, false);
  assert.equal(healthBefore.identity_verified, false);
  assert.equal(healthBefore.delivery_verified, false);
  assert.equal(healthBefore.reason_code, 'session_not_verified');
  assert.equal(healthBefore.baseline_at, baseline);
  assert.equal(healthBefore.inbound_cursor, '1');
  assert.deepEqual(connector.inbound().messages.map(message => message.body), [incoming.body]);
  assert.deepEqual(connector.inbound('1').messages, []);
  for (const method of ['identity', 'scan', 'prepareSend', 'submitSend']) {
    browser[method] = async () => assert.fail(`Recovered uncertain request must not invoke ${method}`);
  }
  const uncertain = { status: 'uncertain', reason_code: 'restart_during_send' };
  assert.deepEqual(await connector.prepare(request), uncertain);
  assert.deepEqual(await connector.send(request), uncertain);
  assert.deepEqual(await connector.send(request), uncertain);
  await assert.rejects(() => connector.prepare({ ...request, body: 'Changed synthetic body.' }), { code: 'idempotency_conflict' });
  const fresh = { ...request, idempotency_key: 'restart-proof:unverified-002' };
  assert.equal((await connector.prepare(fresh)).reason_code, 'session_not_verified');
  assert.equal((await connector.send(fresh)).reason_code, 'send_not_prepared');
  assert.deepEqual(connector.health(), healthBefore, 'Restart and retry must not activate the transport or reset intake');
  assert.equal(await browser.page.evaluate(() => window.syntheticClicks), 0);
  assert.equal(browser.page.url(), 'about:blank');
  assert.deepEqual(await browser.context.cookies(), []);
  const disk = JSON.parse(await readFile(store.path, 'utf8'));
  assert.equal(disk.sends[hash(request.idempotency_key)].status, 'uncertain');
  console.log('PASS: second container recovered the durable reservation as uncertain; no retry/click; unverified health and intake baseline preserved');
} finally {
  await browser.close();
}
