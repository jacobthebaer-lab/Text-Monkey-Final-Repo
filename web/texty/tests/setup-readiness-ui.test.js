import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

for (const refreshFails of [false, true]) {
  test(`finishing church setup awaits fresh server readiness${refreshFails ? ' and clears stale status on failure' : ''}`, async () => {
    const keys = ['document', 'localStorage', 'sessionStorage', 'location', 'history', 'fetch', 'setTimeout', 'setInterval', 'FormData'];
    const saved = Object.fromEntries(keys.map(key => [key, globalThis[key]]));
    const elements = new Map(['#app', '#modal', '#toast'].map(key => [key, {innerHTML:'', textContent:'', classList:{add(){}, remove(){}}}]));
    const listeners = new Map(), calls = [];
    const form = {id:'church-setup-form', data:{church_name:'Fictional church', coordinator_phone:'+12025550111'}};
    let completed = false, readinessReads = 0, releaseReadiness;
    const pendingReadiness = new Promise(resolve => {releaseReadiness = resolve;});
    globalThis.document = {
      querySelector: key => key === '#church-setup-form' ? form : elements.get(key),
      addEventListener(type, callback) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(callback);
      },
    };
    globalThis.localStorage = {getItem:()=>null, setItem(){}, removeItem(){}};
    globalThis.sessionStorage = {getItem:()=>null, setItem(){}, removeItem(){}};
    globalThis.location = {hash:'#access_token=synthetic-token', pathname:'/'};
    globalThis.history = {replaceState(){}};
    globalThis.setTimeout = globalThis.setInterval = () => 0;
    globalThis.FormData = class {
      constructor(value) {this.data = value.data;}
      [Symbol.iterator]() {return Object.entries(this.data)[Symbol.iterator]();}
    };
    globalThis.fetch = async (path, options) => {
      calls.push({path, options});
      let data;
      if (path === '/api/config') data = {connected:true, aiReady:false, automationEnabled:false};
      else if (path === '/api/state') data = seed();
      else if (path === '/api/setup') {
        if (options.body) completed = JSON.parse(options.body).complete;
        data = {details:form.data, completed, revision:completed ? 1 : 0};
      } else if (path === '/api/setup/contacts') data = {contacts:[]};
      else if (path === '/api/setup/admin-texts') {
        readinessReads++;
        if (readinessReads === 2) await pendingReadiness;
        if (readinessReads === 2 && refreshFails)
          return {ok:false, status:503, json:async()=>({detail:'Fictional readiness outage'})};
        data = {enabled:false, ready:false, connection_check_ready:false, checks:[
          {code:'setup', label:'Church setup', ready:completed, detail:completed ? 'Fictional setup saved.' : 'Finish the fictional profile.', action:'setup'},
          {code:'transport', label:'Texting connection', ready:false, detail:'Delivery remains held.'},
        ]};
      } else throw new Error('Unexpected request '+path);
      return {ok:true, json:async()=>data};
    };
    const dispatch = async (type, event) => {
      let stopped = false;
      event.stopImmediatePropagation = () => {stopped = true;};
      for (const callback of listeners.get(type) || []) {
        await callback(event);
        if (stopped) break;
      }
    };
    try {
      await import(`../public/app.js?setup-readiness-${refreshFails}`);
      assert.equal(readinessReads, 1);
      await dispatch('click', {target:{closest:()=>({dataset:{setupStep:'2'}})}});
      const submitting = dispatch('submit', {target:form, submitter:{value:'continue'}, preventDefault(){}});
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(readinessReads, 2, 'Completion fetches readiness again after saving setup');
      assert.doesNotMatch(elements.get('#app').innerHTML, /Admin connection checklist/, 'Settings must wait for the readiness response');
      releaseReadiness();
      await submitting;
      const html = elements.get('#app').innerHTML;
      assert.doesNotMatch(html, /Finish church setup/);
      if (refreshFails) assert.match(html, /Couldn’t check admin updates: Fictional readiness outage/);
      else {
        assert.match(html, /Fictional setup saved\./);
        assert.match(html, /Delivery remains held\./);
      }
      assert.ok(calls.filter(call => call.options.body).every(call => call.path === '/api/setup'));
    } finally {
      releaseReadiness();
      for (const [key, value] of Object.entries(saved)) {
        if (value === undefined) delete globalThis[key];
        else globalThis[key] = value;
      }
    }
  });
}
