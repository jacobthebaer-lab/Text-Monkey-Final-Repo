import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.js';

const offline = 'Text Monkey is temporarily offline. Please try again shortly.';

test('unreachable backend returns a concise retry message without suggesting another mode', async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error('private upstream detail'); };
  try {
    const response = await worker.fetch(new Request('https://console.example.test/api/state'), {
      BACKEND_URL: 'https://backend.example.test', BACKEND_BRIDGE_KEY: 'fixture-bridge',
    });
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), {error: offline});
  } finally { globalThis.fetch = saved; }
});

test('missing backend uses the same retry message and keeps configuration disconnected', async () => {
  const state = await worker.fetch(new Request('https://console.example.test/api/state'), {});
  assert.equal(state.status, 503);
  assert.equal(state.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await state.json(), {error: offline});
  const config = await worker.fetch(new Request('https://console.example.test/api/config'), {});
  assert.equal((await config.json()).connected, false);
});

test('unsupported texting route remains unavailable without infrastructure instructions', async () => {
  const response = await worker.fetch(new Request('https://console.example.test/sms/inbound', {method: 'POST'}), {});
  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), {error: 'This texting route is unavailable.'});
});
