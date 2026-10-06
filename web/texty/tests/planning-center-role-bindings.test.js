import test from 'node:test';
import assert from 'node:assert/strict';
import {createPlanningCenterRoleBindings} from '../public/planning-center-review.js';

const mapping={shift_id:1,local_role_id:2,team_id:'30',position_id:'90',plan_time_id:'60'};
const catalogue=()=>({native_writes:false,execution_enabled:false,
  shifts:[{id:1,title:'Synthetic Sunday',service_type_id:'20',plan_time_id:'60'}],
  roles:[{id:2,name:'Greeter',required_qualifications:[]}],
  positions:[{service_type_id:'20',position_id:'90',team_id:'30',name:'Greeter'}]});
const proposal=()=>({native_writes:false,execution_enabled:false,review_hash:'1'.repeat(64),
  review_token:{document:JSON.stringify({expires_at:new Date(Date.now()+600000).toISOString()}),signature:'2'.repeat(64)},
  snapshot:{native:{position_name:'Greeter'},local:{role:{name:'Greeter',required_qualifications:['orientation']},
    shifts:[{id:1},{id:3}],binding:{team_id:'30',position_id:'90'},
    event:{native_key:'10:20:40:60',starts_at:'2026-11-01T15:00:00Z',ends_at:'2026-11-01T16:00:00Z'}}}});
function fixture(overrides={}) {
  const calls=[],choices=catalogue(),review=proposal();
  const flow=createPlanningCenterRoleBindings({api:async(path,body)=>{calls.push({path,body});
    return path.endsWith('/catalogue')?choices:path.endsWith('/proposal')?review:
      {native_writes:false,execution_enabled:false,local_role_id:2};},
    getMode:()=> 'live',getToken:()=> 'synthetic-token',render(){},...overrides});
  return {flow,calls,choices,review};
}
async function select(flow) {await flow.perform('load');flow.select('shift','1');flow.select('role','2');flow.select('position','20:90');}

test('explicit native position and local role review submits exact signed context, once',async()=>{
  const {flow,calls,review}=fixture();assert.equal(calls.length,0);await flow.perform('apply');assert.equal(calls.length,0);
  await select(flow);await flow.perform('review');assert.deepEqual(calls[1].body,mapping);
  assert.match(flow.panel(),/Required qualifications: orientation/);assert.match(flow.panel(),/Applies to 2 imported slots/);
  await flow.perform('apply');assert.deepEqual(calls[2].body,{...mapping,review_hash:review.review_hash,review_token:review.review_token});
  assert.match(flow.panel(),/Local role mapping saved/);await flow.perform('apply');assert.equal(calls.length,3);
});

test('selection and account changes invalidate cached and in-flight reviews',async()=>{
  let token='account-a',resolve;
  const {flow,calls}=fixture({getToken:()=>token});await select(flow);await flow.perform('review');
  flow.select('role','999');await flow.perform('apply');assert.equal(calls.length,2);
  flow.select('role','2');await flow.perform('review');token='account-b';await flow.perform('apply');assert.equal(calls.length,3);
  assert.doesNotMatch(flow.panel(),/Use reviewed local role|orientation/);
  const pending=fixture({getToken:()=>token,api:()=>new Promise(r=>resolve=r)});
  const load=pending.flow.perform('load');await pending.flow.perform('load');token='account-c';resolve(catalogue());await load;
  assert.doesNotMatch(pending.flow.panel(),/Select a service slot/);
});

test('expired, malformed or executing reviews and cross-service choices cannot apply',async()=>{
  for(const mutate of [r=>r.execution_enabled=true,r=>r.review_token.signature='invalid',
    r=>r.review_token.document='invalid',r=>r.snapshot.local.role.required_qualifications=null]) {
    const {flow,calls,review}=fixture();mutate(review);await select(flow);await flow.perform('review');await flow.perform('apply');
    assert.equal(calls.length,2);assert.doesNotMatch(flow.panel(),/Use reviewed local role/);
  }
  const expired=fixture();expired.review.review_token.document=JSON.stringify({expires_at:new Date(0).toISOString()});
  await select(expired.flow);await expired.flow.perform('review');await expired.flow.perform('apply');assert.equal(expired.calls.length,2);
  const other=fixture();other.choices.positions[0].service_type_id='21';await select(other.flow);other.flow.select('position','21:90');
  await other.flow.perform('review');assert.equal(other.calls.length,1);
});

test('disconnected controls never fetch, server errors stay private, labels escape HTML',async()=>{
  const offline=fixture({getMode:()=> 'demo'});await offline.flow.perform('load');assert.equal(offline.flow.panel(),'');assert.equal(offline.calls.length,0);
  const {flow,choices}=fixture();choices.roles[0].name='<script>Greeter';await flow.perform('load');
  assert.match(flow.panel(),/&lt;script&gt;Greeter/);assert.doesNotMatch(flow.panel(),/<script>/);
  const stale=fixture({api:async path=>{if(path.endsWith('/catalogue'))return catalogue();if(path.endsWith('/proposal'))return proposal();throw Object.assign(Error('PRIVATE response'),{status:409});}});
  await select(stale.flow);await stale.flow.perform('review');await stale.flow.perform('apply');
  assert.match(stale.flow.panel(),/fresh review/);assert.doesNotMatch(stale.flow.panel(),/PRIVATE|Use reviewed local role/);
});
