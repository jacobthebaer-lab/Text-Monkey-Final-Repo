import test from 'node:test';
import assert from 'node:assert/strict';
import { seed } from '../public/domain.js';

test('coordinator reload keeps a verified tab session; logout and invalid sessions return to sign-in', async () => {
  const keys = ['document', 'localStorage', 'sessionStorage', 'location', 'history', 'fetch', 'setTimeout', 'setInterval'];
  const saved = Object.fromEntries(keys.map(k => [k, globalThis[k]]));
  const elements = new Map(['#app', '#modal', '#toast'].map(k => [k, {
    innerHTML: '', textContent: '', classList: { add(){}, remove(){} }, showModal(){}, close(){}
  }]));
  const listeners = new Map(), storage = new Map(), calls = [];
  const state = seed();
  let poll, configName = "Texty";
  let invalid = false, completed = true, setupUnavailable = false;
  globalThis.document = { querySelector: k => elements.get(k), addEventListener: (event, cb) => listeners.set(event, cb) };
  globalThis.localStorage = { getItem: () => null, setItem(){} };
  globalThis.sessionStorage = { getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) };
  globalThis.location = { hash: '#access_token=synthetic-session-token', pathname: '/texty' };
  globalThis.history = { replaceState(){ globalThis.location.hash = ''; } };
  globalThis.setTimeout = () => 0;
  globalThis.setInterval = cb => {poll=cb;return 0;};
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config') return { ok: true, json: async () => ({ connected: true, name: configName }) };
    if (path === '/api/logout') return { ok: true, json: async () => ({}) };
    if (path === '/api/setup') return setupUnavailable
      ? {ok:false,status:503,json:async()=>({detail:'Church setup storage needs its reviewed migration.'})}
      : {ok:true,json:async()=>({details:{church_name:'Synthetic fixture church'},completed,revision:1})};
    if (path === '/api/setup/admin-texts') return {ok:true,json:async()=>({enabled:false,phone:'',issues:[]})};
    if (path === '/api/setup/contacts') return {ok:true,json:async()=>({contacts:[]})};
    assert.equal(path, '/api/state');
    assert.equal(options.headers.Authorization, 'Bearer synthetic-session-token');
    return invalid
      ? { ok: false, status: 401, json: async () => ({ detail: 'Session expired. Sign in again.' }) }
      : { ok: true, json: async () => state };
  };
  try {
    await import('../public/app.js?session-confirmed');
    assert.equal(storage.size, 1);
    assert.match(elements.get('#app').innerHTML, /Text Monkey/);
    configName = "Texty from retained backend";
    await poll();
    assert.match(elements.get('#app').innerHTML, /Text Monkey/);
    assert.doesNotMatch(elements.get('#app').innerHTML, /Texty from retained backend/);
    const before = calls.filter(c => c.path === '/api/state').length;
    await import('../public/app.js?session-reloaded');
    assert.equal(calls.filter(c => c.path === '/api/state').length, before + 1);
    assert.match(elements.get('#app').innerHTML, /data-page="overview" aria-current="page"/);
    const navigation = elements.get('#app').innerHTML.match(/<nav[^>]*>(.*?)<\/nav>/s)[1];
    assert.deepEqual([...navigation.matchAll(/data-page="([^"]+)"/g)].map(m=>m[1]), ['overview','volunteers','schedule']);
    assert.doesNotMatch(elements.get('#app').innerHTML, /Text Lab|Human confirmation is active/);
    completed = false;
    await import('../public/app.js?session-reloaded-unfinished-setup');
    assert.match(elements.get('#app').innerHTML, /Tell us about your church/);
    assert.equal(storage.size, 1);
    setupUnavailable = true;
    await import('../public/app.js?session-reloaded-setup-migration-unavailable');
    assert.match(elements.get('#app').innerHTML, /Setup is awaiting a storage update/);
    assert.equal(storage.size, 1);
    setupUnavailable = false;
    await listeners.get('click')({ target: { closest: () => ({ dataset: { action: 'logout' } }) } });
    assert.equal(storage.size, 0);
    const after = calls.filter(c => c.path === '/api/state').length;
    await import('../public/app.js?session-logged-out');
    assert.equal(calls.filter(c => c.path === '/api/state').length, after);
    assert.match(elements.get('#app').innerHTML, /Welcome back/);
    storage.set('texty.coordinator.session.v1', 'synthetic-session-token');
    invalid = true;
    await import('../public/app.js?session-rejected-by-server');
    assert.equal(storage.size, 0);
    assert.match(elements.get('#app').innerHTML, /Welcome back/);
    assert.equal(elements.get('#toast').textContent, 'Session expired. Sign in again.');
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete globalThis[k];
      else globalThis[k] = v;
    }
  }
});
