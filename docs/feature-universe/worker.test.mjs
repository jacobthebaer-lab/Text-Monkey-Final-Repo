import test from 'node:test';
import assert from 'node:assert/strict';
import { timingSafeEqual } from 'node:crypto';
import worker from './worker.mjs';

// Cloudflare extends WebCrypto with this native constant-time operation.
// Node's native equivalent exercises the same production comparison contract.
crypto.subtle.timingSafeEqual = (a, b) => timingSafeEqual(new Uint8Array(a), new Uint8Array(b));

const now = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
function feed() {
  return {
    schemaVersion: 1, checkedAt: now, generatedAt: now,
    repository: { revision: 'a681bb8', branch: 'codex/complete-text-monkey' },
    features: {
      'quiet-signup': {
        status: 'implemented', progress: 'verified', summary: 'Preferences save silently.',
        checkedAt: now, sourceRevision: 'a681bb8',
        links: [{ title: 'Source', url: 'https://github.com/example/project/commit/a681bb8' }],
      },
    },
    sync: { mode: 'repository-events', intervalMinutes: 60 },
  };
}
function request(path = '/api/status', method = 'GET') {
  return new Request(`https://universe.example.test${path}`, { method });
}
function bindings(value = feed()) {
  const calls = [];
  const env = {
    FEATURE_STATUS: {
      async get(...args) { calls.push(['get', ...args]); return value === null ? null : JSON.stringify(value); },
      async put() { assert.fail('Public requests must not write KV'); },
      async delete() { assert.fail('Public requests must not delete KV'); },
    },
    ASSETS: { async fetch(req) { calls.push(['asset', req]); return new Response('static file'); } },
  };
  return { env, calls };
}
function noStore(response) {
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('cdn-cache-control'), 'no-store');
  assert.equal(response.headers.get('cloudflare-cdn-cache-control'), 'no-store');
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
  assert.match(response.headers.get('content-type'), /^application\/json/);
}

test('GET returns the shared feed and reads only the fixed key', async () => {
  const { env, calls } = bindings();
  const response = await worker.fetch(request('/api/status?key=private-account&fresh=1'), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), feed());
  assert.deepEqual(calls, [['get', 'feature-status', { type: 'text', cacheTtl: 60 }]]);
  noStore(response);
});

test('HEAD checks availability with the same headers and no response body', async () => {
  const { env } = bindings();
  const response = await worker.fetch(request('/api/status', 'HEAD'), env);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), '');
  noStore(response);
});

test('public mutation and preflight methods never read, write or forward', async () => {
  for (const path of ['/api/status', '/index.html', '/']) {
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
      const { env, calls } = bindings();
      const response = await worker.fetch(request(path, method), env);
      assert.equal(response.status, 405, `${method} ${path}`);
      assert.equal(response.headers.get('allow'), 'GET, HEAD');
      assert.deepEqual(await response.json(), { error: 'method_not_allowed' });
      assert.deepEqual(calls, []);
      noStore(response);
    }
  }
});

test('unknown API paths do not fall back to assets or another backend', async () => {
  for (const path of ['/api', '/api/private', '/api/status/']) {
    const { env, calls } = bindings();
    const response = await worker.fetch(request(path), env);
    assert.equal(response.status, 404);
    assert.deepEqual(await response.json(), { error: 'not_found' });
    assert.deepEqual(calls, []);
    noStore(response);
  }
});

test('missing binding, missing feed and KV failures return a generic unavailable response', async () => {
  const missing = bindings(null).env;
  const error = { FEATURE_STATUS: { async get() { throw new Error('private binding diagnostic'); } } };
  for (const env of [{}, missing, error]) {
    const response = await worker.fetch(request(), env);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: 'status_unavailable' });
    noStore(response);
  }
  const head = await worker.fetch(request('/api/status', 'HEAD'), {});
  assert.equal(head.status, 503);
  assert.equal(await head.text(), '');
  noStore(head);
});

test('malformed or oversized KV JSON fails closed without exposing stored content', async () => {
  for (const raw of ['{private:broken}', 'null', '[]', 'x'.repeat(1024 * 1024 + 1)]) {
    const response = await worker.fetch(request(), { FEATURE_STATUS: { async get() { return raw; } } });
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: 'status_unavailable' });
    noStore(response);
  }
});

test('invalid version, header, status or progress never reaches the browser as a valid feed', async () => {
  const mutations = [
    value => { value.schemaVersion = 2; },
    value => { value.checkedAt = 'not a date'; },
    value => { delete value.generatedAt; },
    value => { value.repository = null; },
    value => { value.features = []; },
    value => { value.features = {}; },
    value => { value.checkedAt = new Date(Date.now() + 10 * 60 * 1000).toISOString(); },
    value => { value.generatedAt = new Date(Date.now() + 10 * 60 * 1000).toISOString(); },
    value => { value.features['quiet-signup'].status = 'done'; },
    value => { value.features['quiet-signup'].progress = '100%'; },
    value => { value.features['quiet-signup'].checkedAt = 'tomorrow'; },
    value => { value.features['quiet-signup'].summary = { private: true }; },
    value => { value.sync.mode = 'live-backend'; },
    value => { value.sync.intervalMinutes = 0; },
  ];
  for (const mutate of mutations) {
    const value = feed(); mutate(value);
    const response = await worker.fetch(request(), bindings(value).env);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: 'status_unavailable' });
  }
});

test('only whitelisted fields are public, including in nested feature links', async () => {
  const value = feed();
  value.privateToken = 'synthetic-private-token';
  value.repository.credentials = 'synthetic-credential';
  value.sync.internalLog = 'synthetic-private-log';
  value.features['quiet-signup'].rawConversation = 'synthetic-private-conversation';
  value.features['quiet-signup'].links[0].privateReceipt = 'synthetic-private-receipt';
  const response = await worker.fetch(request(), bindings(value).env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), feed());
});

test('unsafe or malformed public links are rejected', async () => {
  for (const url of ['javascript:alert(1)', 'data:text/plain,private', 'codex://threads/synthetic',
    'https://user:password@example.test/', '/relative']) {
    const value = feed(); value.features['quiet-signup'].links[0].url = url;
    const response = await worker.fetch(request(), bindings(value).env);
    assert.equal(response.status, 503, url);
  }
});

test('all supported statuses/progress values and absent optional links remain valid', async () => {
  for (const status of ['implemented', 'partial', 'planned', 'historical']) {
    for (const progress of ['unchanged', 'changed', 'in_progress', 'blocked', 'verified']) {
      const value = feed();
      Object.assign(value.features['quiet-signup'], { status, progress });
      delete value.features['quiet-signup'].links;
      const response = await worker.fetch(request(), bindings(value).env);
      assert.equal(response.status, 200);
      assert.deepEqual(await response.json(), value);
    }
  }
});

test('unchanged or old feeds keep their actual timestamps rather than claiming a fresh review', async () => {
  const value = feed();
  value.checkedAt = value.generatedAt = value.features['quiet-signup'].checkedAt = '2026-09-01T12:00:00Z';
  value.features['quiet-signup'].progress = 'unchanged';
  const response = await worker.fetch(request(), bindings(value).env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), value);
});

test('static GET and HEAD go only to ASSETS with request and asset response preserved', async () => {
  for (const method of ['GET', 'HEAD']) {
    const req = request('/index.html?version=1', method);
    const expected = new Response(method === 'HEAD' ? null : 'asset body', {
      status: 200, headers: { 'Content-Type': 'text/html', 'ETag': 'synthetic' },
    });
    let seen;
    const response = await worker.fetch(req, {
      FEATURE_STATUS: { async get() { assert.fail('Static requests must not read the feed'); } },
      ASSETS: { async fetch(value) { seen = value; return expected; } },
    });
    assert.equal(seen, req);
    assert.equal(response, expected);
    assert.equal(response.headers.get('etag'), 'synthetic');
    assert.equal(await response.text(), method === 'HEAD' ? '' : 'asset body');
  }
});

test('missing or failed asset binding returns a generic unavailable response', async () => {
  for (const env of [{}, { ASSETS: { async fetch() { throw new Error('private asset error'); } } }]) {
    const response = await worker.fetch(request('/'), env);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: 'assets_unavailable' });
    noStore(response);
  }
});

const publishKey = 'synthetic-test-only-publisher-key-with-64-characters-0123456789';
function publishRequest(value = feed(), options = {}) {
  const headers = {
    Authorization: `Bearer ${publishKey}`,
    'Content-Type': 'application/json',
    ...options.headers,
  };
  if (options.noToken) delete headers.Authorization;
  return new Request('https://universe.example.test/api/status/publish', {
    method: options.method || 'POST', headers,
    ...(!['GET', 'HEAD'].includes(options.method) ? {
      body: options.raw ?? JSON.stringify(value),
    } : {}),
  });
}
function publisher(current = null) {
  const calls = [];
  const env = {
    STATUS_PUBLISH_KEY: publishKey,
    FEATURE_STATUS: {
      async get(...args) { calls.push(['get', ...args]); return current === null ? null : JSON.stringify(current); },
      async put(key, value) { calls.push(['put', key, JSON.parse(value)]); },
    },
    ASSETS: { async fetch() { assert.fail('Publisher must not access assets'); } },
  };
  return { env, calls };
}
function newerFeed() {
  const value = feed();
  value.checkedAt = value.generatedAt = value.features['quiet-signup'].checkedAt = new Date(Date.parse(now) + 60 * 60 * 1000).toISOString();
  value.repository.revision = value.features['quiet-signup'].sourceRevision = 'b681bb8';
  return value;
}

test('authenticated publishing stores only the sanitized shared contract at the fixed key', async () => {
  const expected = newerFeed(), submitted = structuredClone(expected);
  submitted.privateToken = 'never-store-this';
  submitted.features['quiet-signup'].rawMessage = 'never-store-this-either';
  const { env, calls } = publisher(feed());
  const response = await worker.fetch(publishRequest(submitted), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, checkedAt: expected.checkedAt, revision: expected.repository.revision });
  assert.deepEqual(calls, [
    ['get', 'feature-status', { type: 'text', cacheTtl: 60 }],
    ['put', 'feature-status', expected],
  ]);
  noStore(response);
  assert.equal(response.headers.get('access-control-allow-origin'), null);
});

test('a valid authenticated first publication can initialize an empty namespace', async () => {
  const { env, calls } = publisher();
  const response = await worker.fetch(publishRequest(), env);
  assert.equal(response.status, 200);
  assert.deepEqual(calls.at(-1), ['put', 'feature-status', feed()]);
});

test('missing, partial, wrong-length and wrong bearer tokens never touch KV', async () => {
  for (const options of [
    { noToken: true },
    { headers: { Authorization: 'Bearer wrong' } },
    { headers: { Authorization: `Bearer ${publishKey.slice(0, -1)}` } },
    { headers: { Authorization: `Bearer ${publishKey.slice(0, -1)}X` } },
    { headers: { Authorization: publishKey } },
    { headers: { Authorization: `Basic ${publishKey}` } },
  ]) {
    const { env, calls } = publisher();
    const response = await worker.fetch(publishRequest(feed(), options), env);
    assert.equal(response.status, 401);
    assert.deepEqual(await response.json(), { error: 'unauthorized' });
    assert.deepEqual(calls, []);
    noStore(response);
  }
});

test('publishing accepts POST only and provides no unauthenticated CORS preflight', async () => {
  for (const method of ['GET', 'HEAD', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
    const { env, calls } = publisher();
    const response = await worker.fetch(publishRequest(feed(), { method }), env);
    assert.equal(response.status, 405);
    assert.equal(response.headers.get('allow'), 'POST');
    assert.equal(response.headers.get('access-control-allow-origin'), null);
    if (method === 'HEAD') assert.equal(await response.text(), '');
    else assert.deepEqual(await response.json(), { error: 'method_not_allowed' });
    assert.deepEqual(calls, []);
    noStore(response);
  }
});

test('publisher is unavailable when its scoped secret or required KV operations are missing', async () => {
  for (const mutate of [
    env => { delete env.STATUS_PUBLISH_KEY; },
    env => { env.STATUS_PUBLISH_KEY = 'short'; },
    env => { delete env.FEATURE_STATUS; },
    env => { delete env.FEATURE_STATUS.put; },
  ]) {
    const { env, calls } = publisher(); mutate(env);
    const response = await worker.fetch(publishRequest(), env);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: 'publish_unavailable' });
    assert.deepEqual(calls, []);
  }
});

test('authenticated cross-origin browser mutation is rejected before any storage access', async () => {
  const { env, calls } = publisher();
  const response = await worker.fetch(publishRequest(feed(), {
    headers: { Origin: 'https://different.example.test' },
  }), env);
  assert.equal(response.status, 403);
  assert.deepEqual(await response.json(), { error: 'origin_not_allowed' });
  assert.deepEqual(calls, []);
  assert.equal(response.headers.get('access-control-allow-origin'), null);
});

test('publisher requires JSON and rejects invalid contracts without storage reads or writes', async () => {
  const invalid = feed(); invalid.features['quiet-signup'].status = 'finished';
  for (const [value, options, expectedStatus, error] of [
    [feed(), { headers: { 'Content-Type': 'text/plain' } }, 415, 'json_required'],
    [feed(), { raw: '{secret-invalid-json}' }, 400, 'invalid_feed'],
    [invalid, {}, 400, 'invalid_feed'],
  ]) {
    const { env, calls } = publisher();
    const response = await worker.fetch(publishRequest(value, options), env);
    assert.equal(response.status, expectedStatus);
    assert.deepEqual(await response.json(), { error });
    assert.deepEqual(calls, []);
    noStore(response);
  }
});

test('publisher enforces the 512 KiB actual byte limit with and without Content-Length', async () => {
  for (const options of [
    { raw: 'x'.repeat(512 * 1024 + 1) },
    { raw: '界'.repeat(180000) },
    { headers: { 'Content-Length': String(512 * 1024 + 1) } },
  ]) {
    const { env, calls } = publisher();
    const response = await worker.fetch(publishRequest(feed(), options), env);
    assert.equal(response.status, 413);
    assert.deepEqual(await response.json(), { error: 'body_too_large' });
    assert.deepEqual(calls, []);
  }
});

test('older global review/publication timestamps cannot overwrite a newer KV snapshot', async () => {
  const changes = [
    value => { value.checkedAt = new Date(Date.parse(now) - 60 * 60 * 1000).toISOString(); },
    value => { value.generatedAt = new Date(Date.parse(now) - 60 * 60 * 1000).toISOString(); },
    value => { value.checkedAt = now; }, // Changed revision with no later review.
  ];
  for (const change of changes) {
    const value = newerFeed(); change(value);
    const { env, calls } = publisher(feed());
    const response = await worker.fetch(publishRequest(value), env);
    assert.equal(response.status, 409);
    assert.deepEqual(await response.json(), { error: 'stale_feed' });
    assert.deepEqual(calls, [['get', 'feature-status', { type: 'text', cacheTtl: 60 }]]);
  }
});

test('a newer feed may restore an older meaningful feature review without disguising its audit age', async () => {
  const value = newerFeed();
  value.features['quiet-signup'].checkedAt = new Date(Date.parse(now) - 24 * 60 * 60 * 1000).toISOString();
  value.features['quiet-signup'].sourceRevision = 'older-reviewed-commit';
  const { env, calls } = publisher(feed());
  const response = await worker.fetch(publishRequest(value), env);
  assert.equal(response.status, 200);
  assert.deepEqual(calls.at(-1), ['put', 'feature-status', value]);
});

test('empty inventories and feed timestamps over five minutes in the future cannot poison freshness', async () => {
  for (const change of [
    value => { value.features = {}; },
    value => { value.checkedAt = new Date(Date.now() + 10 * 60 * 1000).toISOString(); },
    value => { value.generatedAt = new Date(Date.now() + 10 * 60 * 1000).toISOString(); },
  ]) {
    const value = newerFeed(); change(value);
    const { env, calls } = publisher(feed());
    const response = await worker.fetch(publishRequest(value), env);
    assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { error: 'invalid_feed' });
    assert.deepEqual(calls, []);
  }
});

test('replaying the same publication is idempotent and performs no KV write', async () => {
  const { env, calls } = publisher(feed());
  const response = await worker.fetch(publishRequest(), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, unchanged: true, checkedAt: now });
  assert.equal(calls.length, 1);
});

test('publisher storage and corrupted-current-feed failures expose no diagnostics', async () => {
  for (const mutate of [
    env => { env.FEATURE_STATUS.get = async () => { throw new Error('private read detail'); }; },
    env => { env.FEATURE_STATUS.put = async () => { throw new Error('private write detail'); }; },
    env => { env.FEATURE_STATUS.get = async () => '{private invalid value}'; },
  ]) {
    const { env } = publisher(); mutate(env);
    const response = await worker.fetch(publishRequest(), env);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: 'publish_unavailable' });
    noStore(response);
  }
});
