import test from 'node:test';
import assert from 'node:assert/strict';
import {createPlanningCenterBlockouts,blockoutPresentation} from '../public/planning-center-blockouts.js';
const status=(overrides={})=>({organization_id:'fixture-org',volunteer_id:7,person_id:'fixture-person',policy_enabled:false,runtime_enabled:true,state:'disabled',reason:'disabled',
  desired_revision:'revision-a',verified_revision:null,owned_count:0,unknown_attempt:false,notification_mode:'provider_managed',conflict_strategy:'preserve_existing_bookings',effect_scope:'all_services_teams',
  acceptance:{available:true,evidence_hash:'a'.repeat(64),verified_at:'2026-10-07T14:00:00Z',expires_at:'2026-11-06T14:00:00Z',notification_mode:'provider_managed',conflict_strategy:'preserve_existing_bookings',date_contract:'finite_full_days',timezone:'America/Denver'},
  readiness:{signing_ready:true,acceptance_ready:true,mapping_ready:true,consent_ready:true,authority_ready:overrides.policy_enabled===true,journal_ready:true},global_unavailable_dates:['2026-12-01'],...overrides});
function fixture(){
  let token='account-a',epoch=1,mode='live',person={id:7,first_name:'Casey',last_name:'<Example> [Fictional]'},next=status(),failure=null,wait=null;
  const calls=[];
  const ui=createPlanningCenterBlockouts({api:async(path,body,options)=>{
    calls.push({path,body,options});const result=structuredClone(body?{...next,policy_enabled:body.enabled,state:body.enabled?'pending':'disabled',readiness:{...next.readiness,authority_ready:body.enabled && ['signing_ready','acceptance_ready','mapping_ready','consent_ready'].every(k=>next.readiness[k])}}:next),pause=wait,failed=failure;
    if(pause)await pause;if(failed)throw failed;return result;
  },getMode:()=>mode,getToken:()=>token,getSessionEpoch:()=>epoch,getVolunteer:()=>person,render(){}});
  return {ui,calls,response:v=>{next=v;},fail:v=>{failure=v;},wait:v=>{wait=v;},select:v=>{person=v;},switchUser:()=>{epoch++;token='account-b';},rotate:()=>{token='rotated-account-a';},offline:()=>{mode='demo';token=null;}};
}

test('selected volunteer status is an explicit authenticated read with clean labels, scope and timezone',async()=>{
  const f=fixture();assert.equal(f.calls.length,0);
  assert.match(f.ui.panel(),/Casey &lt;Example&gt;/);assert.doesNotMatch(f.ui.panel(),/Fictional|<Example>/);
  assert.match(f.ui.panel(),/normal leader notification preferences/);assert.match(f.ui.panel(),/does not cancel existing bookings/);
  await f.ui.load();assert.deepEqual(f.calls,[{path:'/api/planning-center/blockouts/7',body:undefined,options:undefined}]);
  assert.match(f.ui.panel(),/all Planning Center Services teams/);assert.match(f.ui.panel(),/America\/Denver/);
  assert.doesNotMatch(f.ui.panel(),/fixture-org|fixture-person|evidence_hash|revision-a|2026-10-07/);
});

test('missing, non-server and disconnected people cannot fetch or expose sync controls',async()=>{
  const f=fixture();for(const person of [null,{id:'local-demo'},{id:0},{id:'9007199254740993'}]){
    f.select(person);await f.ui.load();assert.doesNotMatch(f.ui.panel(),/data-pco-blockout-action/);
  }
  f.offline();await f.ui.load();assert.equal(f.ui.panel(),'');assert.equal(f.calls.length,0);
});

for(const change of ['selection','account'])test(`${change} change discards old results and cannot unlock a newer check`,async()=>{
  const f=fixture();let releaseOld,releaseNew;
  f.response(status({policy_enabled:true,state:'verified',verified_revision:'revision-a'}));f.wait(new Promise(resolve=>{releaseOld=resolve;}));
  const old=f.ui.load();await f.ui.load();assert.equal(f.calls.length,1);
  if(change==='selection')f.select({id:8,name:'Other Person'});else f.switchUser();
  f.ui.panel();f.response(status({volunteer_id:change==='selection'?8:7}));f.wait(new Promise(resolve=>{releaseNew=resolve;}));
  const fresh=f.ui.load();releaseOld();await old;
  assert.match(f.ui.panel(),/Checking availability sync/);assert.doesNotMatch(f.ui.panel(),/Synced/);
  await f.ui.load();assert.equal(f.calls.length,2);
  releaseNew();await fresh;assert.match(f.ui.panel(),/<strong>Off<\/strong>/);
});

test('malformed, mismatched and server-error status remains private without false verification',async()=>{
  for(const response of [{state:'verified'},status({volunteer_id:8}),status({state:'impossible'}),status({owned_count:-1}),status({unknown_attempt:'PRIVATE internal attempt key'})]){
    const f=fixture();f.response(response);await f.ui.load();assert.match(f.ui.panel(),/Could not check availability sync/);assert.doesNotMatch(f.ui.panel(),/Enable automatic sync|Synced/);
  }
  const f=fixture();f.fail(Object.assign(Error('PRIVATE credentials and remote payload'),{status:503}));await f.ui.load();
  assert.doesNotMatch(f.ui.panel(),/PRIVATE|credentials|remote payload/);
});

test('waiting, verified and uncertain status never overstate success',()=>{
  assert.equal(blockoutPresentation(status({policy_enabled:true,state:'pending'})).label,'Waiting');
  assert.equal(blockoutPresentation(status({policy_enabled:true,state:'verified',verified_revision:'older'})).label,'Waiting');
  assert.equal(blockoutPresentation(status({policy_enabled:true,state:'verified',verified_revision:'revision-a',runtime_enabled:false})).label,'Waiting');
  assert.equal(blockoutPresentation(status({policy_enabled:true,state:'verified',verified_revision:'revision-a',readiness:{...status().readiness,authority_ready:true}})).label,'Synced');
  for(const value of [{state:'unknown'},{state:'verified',unknown_attempt:true},{state:'held',policy_enabled:true}])assert.equal(blockoutPresentation(status(value)).label,'Needs attention');
});

test('enable is one strict PUT after current server-owned readiness, then waiting rather than false success',async()=>{
  const f=fixture();await f.ui.action('enable','7');assert.equal(f.calls.length,0);
  await f.ui.load();let release;f.wait(new Promise(resolve=>{release=resolve;}));
  const saving=f.ui.action('enable','7');await f.ui.action('enable','7');assert.equal(f.calls.length,2);
  assert.deepEqual(f.calls[1],{path:'/api/planning-center/blockouts/7/policy',body:{enabled:true},options:{method:'PUT'}});
  release();await saving;assert.match(f.ui.panel(),/Automatic sync is enabled/);assert.match(f.ui.panel(),/<strong>Waiting<\/strong>/);assert.doesNotMatch(f.ui.panel(),/Synced/);
  assert.deepEqual(Object.keys(f.calls[1].body),['enabled']);
});

for(const key of ['signing_ready','acceptance_ready','mapping_ready','consent_ready'])test(`missing ${key} prevents enabling but never prevents disabling`,async()=>{
  const f=fixture();const value=status();value.readiness[key]=false;f.response(value);await f.ui.load();
  assert.match(f.ui.panel(),/data-pco-blockout-action="enable"[^>]*disabled/);await f.ui.action('enable','7');assert.equal(f.calls.length,1);
  f.response({...value,policy_enabled:true,state:'held',unknown_attempt:true});await f.ui.load();
  assert.match(f.ui.panel(),/data-pco-blockout-action="disable"[^>]*>Turn automatic sync off/);
  await f.ui.action('disable','7');assert.deepEqual(f.calls.at(-1).body,{enabled:false});assert.match(f.ui.panel(),/Automatic updates are off/);
});

test('unavailable proof, invalid timezone and uncertain attempts block enable without technical input',async()=>{
  for(const mutate of [s=>s.acceptance.available=false,s=>s.acceptance.evidence_hash='invalid',s=>s.acceptance.timezone='not-a-zone',s=>s.unknown_attempt=true,s=>s.readiness.journal_ready=false,s=>s.acceptance.expires_at='invalid']){
    const f=fixture(),value=status();mutate(value);f.response(value);await f.ui.load();await f.ui.action('enable','7');assert.equal(f.calls.length,1);
    assert.doesNotMatch(f.ui.panel(),/name="evidence|name="timezone|fixture-person/);
  }
});

test('uncertain policy result requires a fresh read, without automatic or blind mutation retries',async()=>{
  const f=fixture();await f.ui.load();f.fail(Error('PRIVATE lost acknowledgment'));
  await f.ui.action('enable','7');await f.ui.action('enable','7');assert.equal(f.calls.length,2);
  assert.match(f.ui.panel(),/Could not confirm the setting change/);assert.doesNotMatch(f.ui.panel(),/PRIVATE|Automatic sync is enabled|data-pco-blockout-action="enable"/);
  f.fail(null);f.response(status({policy_enabled:true,state:'pending'}));await f.ui.load();assert.equal(f.calls.length,3);
  assert.equal(f.calls[2].body,undefined);assert.match(f.ui.panel(),/<strong>Waiting<\/strong>/);
});

test('selection and account changes discard a saved policy result; stale person buttons cannot mutate',async()=>{
  for(const change of ['selection','account']){
    const f=fixture();await f.ui.load();let release;f.wait(new Promise(resolve=>{release=resolve;}));
    const saving=f.ui.action('enable','7');if(change==='selection')f.select({id:8,name:'Other Person'});else f.switchUser();
    f.ui.panel();release();await saving;assert.doesNotMatch(f.ui.panel(),/Automatic sync is enabled|Synced/);
    await f.ui.action('enable','7');assert.equal(f.calls.length,2);
  }
});

test('access-token rotation within the same account preserves the loaded setting',async()=>{
  const f=fixture();await f.ui.load();f.rotate();assert.match(f.ui.panel(),/Enable automatic sync/);
  await f.ui.action('enable','8');assert.equal(f.calls.length,1);
});


test('unconfigured and stale authority can always be explicitly turned off with unknown ownership count',async()=>{
  for(const unknown of [false,true]){
    const f=fixture();f.response(status({state:'held',unknown_attempt:unknown,owned_count:null,notification_mode:null,conflict_strategy:null,
      acceptance:{available:false},readiness:{signing_ready:false,acceptance_ready:false,mapping_ready:false,consent_ready:false,authority_ready:false,journal_ready:false}}));
    await f.ui.load();const html=f.ui.panel();assert.match(html,/data-pco-blockout-action="disable"[^>]*>Turn automatic sync off/);
    assert.doesNotMatch(html,/private-internal|name="evidence|name="timezone/);
    if(unknown)assert.match(html,/<strong>Needs attention<\/strong>/);
    await f.ui.action('disable','7');assert.deepEqual(f.calls.at(-1).body,{enabled:false});
  }
});


test('unauthenticated journal or authority cannot produce a Synced status',()=>{
  for(const key of ['authority_ready','journal_ready']){
    const value=status({policy_enabled:true,state:'verified',verified_revision:'revision-a'});
    value.readiness={...value.readiness,authority_ready:true,journal_ready:true,[key]:false};
    assert.equal(blockoutPresentation(value).label,'Needs attention');
  }
});
