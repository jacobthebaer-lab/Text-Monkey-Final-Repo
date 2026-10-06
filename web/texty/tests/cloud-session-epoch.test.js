import test from 'node:test';
import assert from 'node:assert/strict';
import {createCoordinatorSession} from '../public/coordinator-session.js';
import {createCloudTexting} from '../public/cloud-texting.js';

const status=enabled=>({demo_mode:true,connection:{connected:true},continuous_signup:{available:true,enabled,active:enabled,state:enabled?'enabled':'off'}});
const response=(value,status=200)=>({ok:status===200,status,json:async()=>structuredClone(value)});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
function fixture() {
  let token,time=1000000,server=status(true),fault=null,hold=null;
  const calls=[],entries=new Map();
  const client=createCoordinatorSession({now:()=>time,storage:()=>({getItem:k=>entries.get(k),setItem:(k,v)=>entries.set(k,v),removeItem:k=>entries.delete(k)}),onChange:value=>token=value,
    fetch:async(path,options)=>{
      calls.push({path,options});
      if(path==='/api/session/refresh')return response({access_token:'synthetic-rotated',refresh_token:'synthetic-next',expires_at:4000});
      if(path==='/api/auth/me')return response({superadmin:!token?.includes('other')});
      if(path==='/api/cloud-texting')return response(server);
      if(path==='/api/logout')return response({});
      if(path==='/api/cloud-texting/signup/enable'){
        const captured=status(JSON.parse(options.body).enabled);
        if(hold){const wait=hold;hold=null;await wait.promise;}
        if(fault)return response({detail:'Synthetic step failed.'},fault);
        server=captured;return response(captured);
      }
      throw new Error('Unexpected synthetic request '+path);
    }});
  client.set({access_token:'synthetic-initial',refresh_token:'synthetic-parent',expires_at:2000});
  const ui=createCloudTexting({api:(...args)=>client.request(...args),getMode:()=> 'live',getToken:()=>token,
    getSessionEpoch:()=>client.getEpoch(),getConfig:()=>({cloudTextingAvailable:true}),render(){}});
  return {client,ui,calls,nearExpiry(){time=1950000;},setFault:value=>fault=value,setHold:value=>hold=value};
}
const mutations=f=>f.calls.filter(c=>c.path==='/api/cloud-texting/signup/enable');

test('actual refresh plus pause accepts same-session result and releases busy for the next action',async()=>{
  const f=fixture();await f.ui.load();assert.match(f.ui.screen(),/Running in the cloud/);
  const epoch=f.client.getEpoch();f.nearExpiry();await f.ui.action('signup-stop');
  assert.equal(f.client.getEpoch(),epoch);
  assert.equal(f.calls.filter(c=>c.path==='/api/session/refresh').length,1);
  assert.equal(mutations(f).length,1);
  assert.equal(mutations(f)[0].options.headers.Authorization,'Bearer synthetic-rotated');
  assert.match(f.ui.screen(),/Enable cloud signup/);assert.doesNotMatch(f.ui.screen(),/Running in the cloud|data-cloud-action="refresh" disabled/);
  await f.ui.load();await f.ui.action('signup-enable');assert.equal(mutations(f).length,2);
  assert.match(f.ui.screen(),/Running in the cloud/);
});

test('identity/status load survives same-session refresh without losing role',async()=>{
  const f=fixture();f.nearExpiry();await f.ui.load();
  assert.match(f.ui.screen(),/Pause signup/);assert.equal(f.calls.filter(c=>c.path==='/api/cloud-texting').length,1);
});

test('failure after refresh reports a hold and releases its operation, without automatic mutation replay',async()=>{
  const f=fixture();await f.ui.load();f.nearExpiry();f.setFault(503);await f.ui.action('signup-stop');
  assert.equal(mutations(f).length,1);assert.match(f.ui.screen(),/role="alert"/);
  assert.doesNotMatch(f.ui.screen(),/data-cloud-action="refresh" disabled/);
  f.setFault(null);await f.ui.action('signup-stop');assert.equal(mutations(f).length,2);
});

test('late pause response after actual signOut cannot restore role/status or permit an action',async()=>{
  const f=fixture();await f.ui.load();const hold=deferred();f.setHold(hold);
  const pending=f.ui.action('signup-stop');await new Promise(r=>setImmediate(r));
  await f.client.signOut();assert.equal(f.ui.screen(),'');hold.resolve();await pending;
  assert.equal(f.ui.screen(),'');await f.ui.action('signup-enable');assert.equal(mutations(f).length,1);
});

test('user switch hides cached privilege and old response cannot overwrite new user role',async()=>{
  const f=fixture();await f.ui.load();const hold=deferred();f.setHold(hold);
  const pending=f.ui.action('signup-stop');await new Promise(r=>setImmediate(r));
  f.client.set({access_token:'synthetic-other',refresh_token:'synthetic-other-parent',expires_at:4000});
  assert.equal(f.ui.screen(),'');await f.ui.load();assert.equal(f.ui.screen(),'');
  hold.resolve();await pending;await f.ui.action('signup-enable');assert.equal(mutations(f).length,1);
});

test('old cleanup cannot release the new session operation busy flag',async()=>{
  const f=fixture();await f.ui.load();const old=deferred();f.setHold(old);
  const prior=f.ui.action('signup-stop');await new Promise(r=>setImmediate(r));
  f.client.set({access_token:'synthetic-new-login',refresh_token:'synthetic-new-parent',expires_at:4000});
  await f.ui.load();const next=deferred();f.setHold(next);
  const current=f.ui.action('signup-stop');await new Promise(r=>setImmediate(r));
  assert.match(f.ui.screen(),/data-cloud-action="refresh" disabled/);
  old.resolve();await prior;assert.match(f.ui.screen(),/data-cloud-action="refresh" disabled/);
  await f.ui.action('signup-enable');assert.equal(mutations(f).length,2);
  next.resolve();await current;assert.doesNotMatch(f.ui.screen(),/data-cloud-action="refresh" disabled/);
});
