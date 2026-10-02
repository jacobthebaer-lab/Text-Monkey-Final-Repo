import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.js';

test('offline configuration never requests a backend', async () => {
  globalThis.fetch = async () => { throw new Error('Unexpected network'); };
  const r = await worker.fetch(new Request('https://texty.example.test/api/config'), {});
  assert.equal(r.status, 200);
  assert.equal((await r.json()).connected, false);
  assert.equal(r.headers.get('cache-control'), 'no-store');
});

test('backend proxy preserves bearer identity, strips cookies, and injects the private bridge key', async () => {
  let observed;
  globalThis.fetch = async (url, options) => {
    observed = {url: url.toString(), options};
    return Response.json({ok:true});
  };
  const r = await worker.fetch(new Request('https://texty.example.test/api/state?view=calendar', {
    headers: {Authorization: 'Bearer synthetic-token', Cookie: 'session=synthetic', Host: 'texty.example.test'}
  }), {BACKEND_URL:'https://backend.example.test', BACKEND_BRIDGE_KEY:'synthetic-bridge-key'});
  assert.equal(r.status,200);
  assert.equal(observed.url,'https://backend.example.test/api/state?view=calendar');
  assert.equal(observed.options.headers.get('authorization'),'Bearer synthetic-token');
  assert.equal(observed.options.headers.get('cookie'),null);
  assert.equal(observed.options.headers.get('host'),null);
  assert.equal(observed.options.headers.get('x-texty-bridge'),'synthetic-bridge-key');
  assert.equal(r.headers.get('cache-control'),'no-store');
});

test('cross-origin mutations never reach the backend', async () => {
  let calls=0;
  globalThis.fetch=async()=>{calls++; return Response.json({});};
  const r=await worker.fetch(new Request('https://texty.example.test/api/volunteers', {
    method:'POST',headers:{Origin:'https://different.example.test'},body:'{}'
  }),{BACKEND_URL:'https://backend.example.test'});
  assert.equal(r.status,403);
  assert.equal(calls,0);
});

test('backend outage becomes a useful 503 response', async () => {
  globalThis.fetch=async()=>{throw new TypeError('Synthetic outage');};
  const r=await worker.fetch(new Request('https://texty.example.test/api/state'),{BACKEND_URL:'https://backend.example.test'});
  assert.equal(r.status,503);
  assert.match((await r.json()).error,/offline/);
});

test('the Cloudflare frontend cannot accept a Twilio webhook', async () => {
  globalThis.fetch=async()=>{throw new Error('Unexpected network');};
  const r=await worker.fetch(new Request('https://texty.example.test/sms/inbound',{method:'POST'}),{});
  assert.equal(r.status,404);
});

test('static assets include the intended browser security headers', async () => {
  globalThis.fetch=async()=>{throw new Error('Unexpected network');};
  const r=await worker.fetch(new Request('https://texty.example.test/'),{ASSETS:{fetch:async()=>new Response('synthetic HTML')}});
  assert.equal(r.status,200);
  assert.match(r.headers.get('content-security-policy'),/connect-src 'self'/);
  assert.equal(r.headers.get('x-content-type-options'),'nosniff');
});
