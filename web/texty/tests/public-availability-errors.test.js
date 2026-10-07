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

async function withUpstream(upstream, check, {path = '/api/setup', method = 'POST'} = {}) {
  const saved = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; return upstream(); };
  try {
    const response = await worker.fetch(new Request('https://console.example.test' + path, {
      method, headers: {'Content-Type': 'application/json'},
      ...(!['GET', 'HEAD'].includes(method) ? {body: '{}'} : {}),
    }), {BACKEND_URL: 'https://backend.example.test'});
    await check(response);
    assert.equal(calls, 1, 'failed writes must never be replayed by the proxy');
  } finally { globalThis.fetch = saved; }
}

test('upstream HTTP failures and redirects become private no-store JSON without replay', async () => {
  for (const status of [301, 302, 307, 500, 502, 503, 530]) {
    await withUpstream(() => new Response('private upstream account detail', {
      status, headers: {'Content-Type': 'text/html', Location: 'https://private.example.test'},
    }), async response => {
      assert.equal(response.status, 503, `upstream ${status}`);
      assert.deepEqual(await response.json(), {error: offline});
      assert.equal(response.headers.get('cache-control'), 'no-store');
      assert.equal(response.headers.get('location'), null);
      assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
    });
  }
  await withUpstream(() => Response.json({error: 'private provider account detail'}, {status: 500}),
    async response => assert.deepEqual(await response.json(), {error: offline}));
});

test('non-JSON, falsely labelled JSON and unreadable bodies cannot masquerade as auth or success', async () => {
  for (const status of [200, 400, 401, 403]) {
    for (const type of ['text/html', 'application/json']) {
      await withUpstream(() => new Response('private upstream not JSON', {
        status, headers: {'Content-Type': type, 'X-Texty-Auth-Invalid': '1'},
      }), async response => {
        assert.equal(response.status, 503);
        assert.deepEqual(await response.json(), {error: offline});
        assert.equal(response.headers.get('x-texty-auth-invalid'), null);
      });
    }
  }
  await withUpstream(() => new Response(new ReadableStream({start(controller) {
    controller.error(new Error('private read failure'));
  }}), {headers: {'Content-Type': 'application/json'}}), async response => {
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), {error: offline});
  });
});

test('valid API JSON preserves exact rejection status, body and auth-invalid marker', async () => {
  for (const status of [200, 400, 401, 403, 404, 409, 422, 429]) {
    const body = {error: 'Synthetic reviewed API result', reason: 'synthetic-scope'};
    await withUpstream(() => Response.json(body, {
      status, headers: {'X-Texty-Auth-Invalid': '1', 'Retry-After': '30'},
    }), async response => {
      assert.equal(response.status, status);
      assert.deepEqual(await response.json(), body);
      assert.equal(response.headers.get('x-texty-auth-invalid'), '1');
      assert.equal(response.headers.get('retry-after'), '30');
      assert.equal(response.headers.get('cache-control'), 'no-store');
    });
  }
  await withUpstream(() => new Response('{"detail":"Synthetic conflict"}', {
    status: 409, headers: {'Content-Type': 'application/problem+json; charset=utf-8'},
  }), async response => assert.equal(response.status, 409));
});

test('successful bodyless API responses retain their HTTP semantics', async () => {
  for (const status of [204, 205]) {
    await withUpstream(() => new Response(null, {status}), async response => {
      assert.equal(response.status, status);
      assert.equal(await response.text(), '');
    });
  }
});

test('HEAD keeps API headers but cannot pass through a non-JSON authentication error', async () => {
  const saved = globalThis.fetch;
  try {
    for (const [upstreamStatus, type, expected] of [
      [200, 'application/json', 200], [401, 'application/json', 401],
      [403, 'text/plain', 503], [530, 'text/plain', 503],
    ]) {
      globalThis.fetch = async () => new Response(null, {
        status: upstreamStatus, headers: {'Content-Type': type},
      });
      const response = await worker.fetch(new Request('https://console.example.test/api/config', {
        method: 'HEAD',
      }), {BACKEND_URL: 'https://backend.example.test'});
      assert.equal(response.status, expected);
    }
  } finally { globalThis.fetch = saved; }
});

test('only the exact Calendar callback can redirect to a safe completed or denied result', async () => {
  for (const origin of ['https://console.example.test', 'https://text-monkey-demo.pages.dev']) {
    for (const path of ['', '/', '/texty']) {
      for (const result of ['ready', 'denied']) {
        const location = origin + path + '#google-calendar=' + result;
        await withUpstream(() => new Response('private upstream detail', {
          status: 303, headers: {Location: location, 'Set-Cookie': 'private-cookie'},
        }), async response => {
          assert.equal(response.status, 303);
          const expected = origin === 'https://text-monkey-demo.pages.dev' && path === '/texty'
            ? origin + '/#google-calendar=' + result : location;
          assert.equal(response.headers.get('location'), expected);
          assert.equal(response.headers.get('cache-control'), 'no-store');
          assert.equal(response.headers.get('referrer-policy'), 'no-referrer');
          assert.equal(response.headers.get('set-cookie'), null);
          assert.equal(await response.text(), '');
        }, {path: '/api/google-calendar/callback?state=synthetic&code=synthetic', method: 'GET'});
      }
    }
  }
});

test('Calendar redirect exception rejects wrong method, route, status and malformed destinations', async () => {
  const valid = 'https://console.example.test/#google-calendar=ready';
  const cases = [
    {method: 'POST', location: valid}, {path: '/api/google-calendar/finish', location: valid},
    {path: '/api/google-calendar/callback/', location: valid}, {status: 302, location: valid},
    {status: 307, location: valid}, {status: 500, location: valid},
    ...[
      'https://other.example.test/#google-calendar=ready',
      'http://console.example.test/#google-calendar=ready',
      '//console.example.test/#google-calendar=ready',
      '/#google-calendar=ready',
      'https://user:secret@console.example.test/#google-calendar=ready',
      'https://console.example.test/?private=secret#google-calendar=ready',
      'https://console.example.test/private#google-calendar=ready',
      'https://console.example.test/other/../texty#google-calendar=ready',
      'https://console.example.test/%74exty#google-calendar=ready',
      'https://console.example.test/#google-calendar=ready&private=secret',
      'https://console.example.test/#google-calendar=unknown',
      'https://console.example.test/#google-calendar=%72eady',
      'https://console.example.test//#google-calendar=ready',
      'https://console.example.test:444/#google-calendar=ready',
    ].map(location => ({location})),
  ];
  for (const {location, status = 303, path = '/api/google-calendar/callback', method = 'GET'} of cases) {
    await withUpstream(() => new Response('private redirect detail', {status, headers: {Location: location}}),
      async response => {
        assert.equal(response.status, 503, location);
        assert.deepEqual(await response.json(), {error: offline});
        assert.equal(response.headers.get('location'), null);
      }, {path, method});
  }
});

test('public Calendar callbacks migrate the legacy destination to the working Pages root', async () => {
  for (const result of ['ready', 'denied']) {
    await withUpstream(() => new Response(null, {status: 303, headers: {
      Location: `https://text-monkey-demo.pages.dev/texty#google-calendar=${result}`,
    }}), async response => {
      assert.equal(response.status, 303);
      assert.equal(response.headers.get('location'), `https://text-monkey-demo.pages.dev/#google-calendar=${result}`);
    }, {path: '/api/google-calendar/callback', method: 'GET'});
  }
});
