import test from 'node:test';
import assert from 'node:assert/strict';
import {createCoordinatorSession} from '../public/coordinator-session.js';

const reply=(status,data={},headers={})=>({ok:status>=200&&status<300,status,json:async()=>data,headers:{get:key=>headers[key]}});
const session=(name,extra={})=>({access_token:`synthetic-${name}-access`,refresh_token:`synthetic-${name}-refresh`,expires_at:2000,...extra});
function fixture(send) {
  const entries=new Map(),calls=[];let invalid=0,access=null,time=1000000;
  const client=createCoordinatorSession({fetch:async(...args)=>{calls.push(args);return send(...args);},storage:()=>({getItem:k=>entries.get(k),setItem:(k,v)=>entries.set(k,v),removeItem:k=>entries.delete(k)}),now:()=>time,
    onChange:value=>{access=value;},onInvalid:()=>{invalid++;}});
  return {client,entries,calls,setTime:t=>time=t,get access(){return access;},get invalid(){return invalid;}};
}

test('standard expiry triggers one refresh, preserves granted expiry, and sends the rotated bearer',async()=>{
  const f=fixture(async(path)=>path==='/api/session/refresh'?reply(200,session('new',{expires_at:5000})):reply(200,{ok:true}));
  f.client.set(session('old',{expires_at:1030}));
  await f.client.request('/api/state');
  assert.deepEqual(f.calls.map(c=>c[0]),['/api/session/refresh','/api/state']);
  assert.deepEqual(JSON.parse(f.calls[0][1].body),{refresh_token:'synthetic-old-refresh'});
  assert.equal(f.calls[1][1].headers.Authorization,'Bearer synthetic-new-access');
  assert.equal(JSON.parse([...f.entries.values()][0]).expires_at,5000);
});

test('parallel 401 responses coalesce refresh, retry once and never race token rotation',async()=>{
  let count=0,release;
  const wait=new Promise(resolve=>{release=resolve;});
  const f=fixture(async(path,options)=>{
    if(path==='/api/session/refresh'){count++;await wait;return reply(200,session('new'));}
    return reply(options.headers.Authorization==='Bearer synthetic-new-access'?200:401,{detail:'expired'});
  });
  f.client.set(session('old'));
  const first=f.client.request('/api/state'),second=f.client.request('/api/setup');
  await new Promise(resolve=>setImmediate(resolve));release();await Promise.all([first,second]);
  assert.equal(count,1);assert.equal(f.invalid,0);assert.equal(f.access,'synthetic-new-access');
});

test('an expired tab restores the refresh grant; legacy access-only sessions remain compatible',async()=>{
  const f=fixture(async(path)=>path==='/api/session/refresh'?reply(200,session('new')):reply(200,{}));
  f.client.set(session('old',{expires_at:900}));
  assert.ok(f.client.restore());await f.client.request('/api/state');assert.equal(f.access,'synthetic-new-access');
  f.client.set('synthetic-legacy-access');assert.ok(f.client.restore());await f.client.request('/api/state');
  assert.equal([...f.entries.values()][0],'synthetic-legacy-access');
});

test('normal resource 403 denies the operation without ending a valid coordinator login',async()=>{
  const f=fixture(async()=>reply(403,{detail:'Not a superadmin.'}));f.client.set(session('old'));
  await assert.rejects(f.client.request('/api/cloud-texting'),{status:403});assert.equal(f.invalid,0);assert.ok(f.client.hasSession());
});

test('actual coordinator access revocation remains terminal',async()=>{
  const f=fixture(async()=>reply(403,{detail:'Coordinator access revoked.'},{'X-Texty-Auth-Invalid':'1'}));f.client.set(session('old'));
  await assert.rejects(f.client.request('/api/state'),{status:403});assert.equal(f.invalid,1);assert.equal(f.entries.size,0);
});

for(const status of [429,500,503]) test(`refresh ${status} preserves the tab session and never grants or sends the original action`,async()=>{
  const f=fixture(async()=>reply(status,{detail:'Service unavailable.'}));f.client.set(session('old',{expires_at:900}));
  await assert.rejects(f.client.request('/api/reply',{body:'Synthetic action'}),{status});
  assert.equal(f.invalid,0);assert.ok(f.client.hasSession());assert.equal(f.calls.length,1);assert.equal(f.calls[0][0],'/api/session/refresh');
});

for(const status of [401,403]) test(`rejected refresh ${status} clears the session without executing the action`,async()=>{
  const f=fixture(async()=>reply(status));f.client.set(session('old',{expires_at:900}));
  await assert.rejects(f.client.request('/api/reply',{body:'Synthetic action'}),{status:401});
  assert.equal(f.invalid,1);assert.equal(f.entries.size,0);assert.equal(f.calls.length,1);
});

test('late refresh after explicit signout cannot resurrect the session or send a mutation',async()=>{
  let release;const wait=new Promise(resolve=>{release=resolve;});
  const f=fixture(async()=>{await wait;return reply(200,session('new'));});f.client.set(session('old',{expires_at:900}));
  const request=f.client.request('/api/reply',{body:'Synthetic action'});f.client.set(null);release();
  await assert.rejects(request,{status:409});assert.equal(f.entries.size,0);assert.equal(f.access,null);assert.equal(f.calls.length,1);
});

test('older unauthorized response cannot sign out or replay under a newer login',async()=>{
  let release;const wait=new Promise(resolve=>{release=resolve;});
  const f=fixture(async()=>{await wait;return reply(401);});f.client.set(session('old'));
  const request=f.client.request('/api/reply',{body:'Synthetic action'});f.client.set(session('other'));release();
  await assert.rejects(request,{status:409});assert.equal(f.access,'synthetic-other-access');assert.equal(f.invalid,0);assert.equal(f.calls.length,1);
});

test('network/5xx failures never replay an ambiguous mutation, and preserve sign-in',async()=>{
  for(const response of [new Error('Synthetic network failure'),reply(503)]){
    const f=fixture(async()=>{if(response instanceof Error)throw response;return response;});f.client.set(session('old'));
    await assert.rejects(f.client.request('/api/reply',{body:'Synthetic action'}));
    assert.equal(f.calls.length,1);assert.ok(f.client.hasSession());assert.equal(f.invalid,0);
  }
});

test('a second explicit 401 ends the session; no refresh/retry loop',async()=>{
  const f=fixture(async(path)=>path==='/api/session/refresh'?reply(200,session('new')):reply(401));f.client.set(session('old'));
  await assert.rejects(f.client.request('/api/state'),{status:401});assert.equal(f.calls.length,3);assert.equal(f.invalid,1);assert.equal(f.entries.size,0);
});

test('signOut clears synchronously and uses only captured bearer, with no refresh/retry or late logout reset',async()=>{
  let release;const wait=new Promise(resolve=>{release=resolve;});
  const f=fixture(async(path)=>{assert.equal(path,'/api/logout');await wait;return reply(401);});
  f.client.set(session('old',{expires_at:900}));
  const logout=f.client.signOut();
  assert.equal(f.access,null);assert.equal(f.entries.size,0);assert.equal(f.client.hasSession(),false);
  f.client.set(session('other'));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(f.calls[0][1].headers.Authorization,'Bearer synthetic-old-access');
  release();await assert.rejects(logout,{status:401});
  assert.equal(f.access,'synthetic-other-access');assert.equal(f.calls.length,1);assert.equal(f.invalid,0);
});
