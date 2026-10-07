import test from 'node:test';
import assert from 'node:assert/strict';
import {createPlanningCenterReview} from '../public/planning-center-review.js';
const intent='1'.repeat(64),hashes={preview_hash:'2'.repeat(64),source_hash:'3'.repeat(64),remote_hash:'4'.repeat(64),operation_hash:'5'.repeat(64)};
const operation=()=>({kind:'membership_frequency',logical_key:'role:1',method:'PATCH',state:'held',intent_key:intent});
const preview=()=>({...hashes,operations:[operation()],holds:[{reason:'no_supported_global_frequency_write'}],release_holds:['native_notification_silence_unverified'],execution_enabled:false});
const proposal=()=>({...hashes,intent_key:intent,membership:{role_name:'Greeter'},native_snapshot:{schedule_preference:'Every week',saved_at:'2026-10-03T18:00:00Z'},operation:{...operation(),body:{data:{type:'PersonTeamPositionAssignment',attributes:{schedule_preference:'Twice a month'}}}},release_holds:['unowned_remote_frequency_preserved'],execution_enabled:false});
const receipt=()=>({receipt_id:'synthetic-receipt',receipt_hash:'6'.repeat(64),state:'reviewed_held',expires_at:'2026-10-03T18:10:00Z',release_holds:['fresh_native_preflight_required'],execution_enabled:false});
function fixture(overrides={}) {
  const calls=[],native=proposal(),stage=preview();
  const flow=createPlanningCenterReview({api:async(path,body)=>{calls.push({path,body});return path.endsWith('held-previews')?stage:body?receipt():native;},getMode:()=> 'live',getToken:()=> 'fixture-token',getVolunteers:()=>[{id:'7',first_name:'Casey',last_name:'Example'}],render(){},...overrides});
  return{flow,calls,native,stage};
}

test('explicit server volunteer loads saved native comparison and records only four exact hashes, still held',async()=>{
  const {flow,calls}=fixture();flow.select('999');await flow.load();assert.equal(calls.length,0);
  flow.select('7');await flow.load();assert.deepEqual(calls[0],{path:'/api/planning-center/held-previews',body:{volunteer_id:7}});
  assert.equal(calls[1].path,`/api/planning-center/frequency-reviews/${intent}`);
  assert.match(flow.panel(),/Every week/);assert.match(flow.panel(),/Twice a month/);assert.match(flow.panel(),/Native snapshot saved/);
  assert.match(flow.panel(),/person-wide frequency limit stays in Text Monkey/);assert.doesNotMatch(flow.panel(),/operation_hash|\/services\/|PersonTeamPositionAssignment/);
  await flow.record(intent);assert.deepEqual(calls[2].body,hashes);assert.match(flow.panel(),/Review recorded, still held/);
  assert.match(flow.panel(),/Nothing was applied, scheduled or sent/);assert.doesNotMatch(flow.panel(),/Record review/);
  await flow.record(intent);assert.equal(calls.length,3);assert.ok(calls.every(call=>!call.path.includes('execute')));
});

test('no-op uses saved native value and never invents a replacement or creates a receipt',async()=>{
  const {flow,calls,stage,native}=fixture();stage.operations[0].method='NONE';native.operation.method='NONE';native.operation.state='noop';native.operation.body=null;
  flow.select('7');await flow.load();assert.match(flow.panel(),/Already matches the saved native value/);
  assert.doesNotMatch(flow.panel(),/Twice a month|Record review/);await flow.record(intent);assert.equal(calls.length,2);
});

test('missing native preference and unsupported operations stay held without a review action',async()=>{
  const {flow,calls,native}=fixture();native.native_snapshot.schedule_preference=null;
  flow.select('7');await flow.load();assert.match(flow.panel(),/Unavailable, comparison held/);assert.doesNotMatch(flow.panel(),/Record review/);
  await flow.record(intent);assert.equal(calls.length,2);
  const unsupported=fixture();unsupported.stage.operations=[{kind:'blockout',method:'POST',intent_key:intent}];unsupported.stage.holds=[{reason:'native_frequency_not_exactly_representable'}];
  unsupported.flow.select('7');await unsupported.flow.load();assert.match(unsupported.flow.panel(),/cannot be represented exactly/);
  assert.match(unsupported.flow.panel(),/No reviewable role-frequency proposal/);assert.equal(unsupported.calls.length,1);
});

test('stale review clears cached hashes and never silently reloads or re-acknowledges',async()=>{
  const calls=[];const {flow}=fixture({api:async(path,body)=>{calls.push({path,body});if(path.endsWith('held-previews'))return preview();if(body)throw Object.assign(Error('PRIVATE native payload'),{status:409});return proposal();}});
  flow.select('7');await flow.load();await flow.record(intent);assert.match(flow.panel(),/comparison changed or requires fresh review/);
  assert.doesNotMatch(flow.panel(),/PRIVATE|Every week|Record review/);await flow.record(intent);assert.equal(calls.length,3);
});

test('mismatched hashes, missing role identity and accidental execution cannot become a comparison',async()=>{
  for(const mutate of [p=>p.remote_hash='9'.repeat(64),p=>p.execution_enabled=true,p=>delete p.membership.role_name]){
    const {flow,calls,native}=fixture();mutate(native);flow.select('7');await flow.load();assert.match(flow.panel(),/review is held/);
    assert.doesNotMatch(flow.panel(),/Record review|Comparison loaded/);await flow.record(intent);assert.equal(calls.length,2);
  }
});

test('account and selection changes discard in-flight and cached comparisons; repeated clicks issue one request',async()=>{
  let token='account-a',release,calls=0;
  const {flow}=fixture({getToken:()=>token,api:()=>{calls++;return new Promise(resolve=>release=resolve);}});
  flow.select('7');const pending=flow.load();await flow.load();assert.equal(calls,1);token='account-b';release(preview());await pending;
  assert.doesNotMatch(flow.panel(),/Every week|Record review/);
  const loaded=fixture({getToken:()=>token});loaded.flow.select('7');await loaded.flow.load();token='account-c';assert.doesNotMatch(loaded.flow.panel(),/Every week|Record review/);
  const changing=fixture({api:()=>new Promise(resolve=>release=resolve)});changing.flow.select('7');const request=changing.flow.load();changing.flow.select('');release(preview());await request;assert.doesNotMatch(changing.flow.panel(),/Every week|Record review/);
});

test('disconnected previews never expose active controls or fetch; labels escape and private hold details stay hidden',async()=>{
  const offline=fixture({getMode:()=> 'demo'});offline.flow.select('7');await offline.flow.load();await offline.flow.record(intent);
  assert.equal(offline.calls.length,0);assert.match(offline.flow.panel(),/preview is disconnected/);assert.doesNotMatch(offline.flow.panel(),/<select|data-pco-load|data-pco-record/);
  const {flow,native,stage}=fixture();native.membership.role_name='<script>Greeter';stage.holds=[{reason:'secret_native_profile',body:'PRIVATE'}];
  flow.select('7');await flow.load();assert.match(flow.panel(),/&lt;script&gt;Greeter/);assert.doesNotMatch(flow.panel(),/<script>|PRIVATE|secret_native_profile/);
});

test('schema or signing configuration errors remain sanitized holds with no assumed receipt or retry',async()=>{
  let calls=0;
  const missing=fixture({api:async()=>{calls++;throw Object.assign(Error('PRIVATE schema, signing key and backend URL'),{status:503});}});
  missing.flow.select('7');await missing.flow.load();assert.match(missing.flow.panel(),/review is held/);
  assert.doesNotMatch(missing.flow.panel(),/PRIVATE|Record review|Comparison loaded/);await missing.flow.record(intent);assert.equal(calls,1);
  const incomplete=fixture({api:async(path,body)=>path.endsWith('held-previews')?preview():body?{state:'reviewed_held',execution_enabled:false}:proposal()});
  incomplete.flow.select('7');await incomplete.flow.load();await incomplete.flow.record(intent);
  assert.match(incomplete.flow.panel(),/review is held/);assert.doesNotMatch(incomplete.flow.panel(),/Review recorded|Record review/);
});


test('comparison names hide display markers while escaping labels and preserving volunteer identity',async()=>{
  const people=[{id:'7',first_name:'Casey',last_name:'Example [Fictional]',fictional:true},
    {id:'8',name:'Synthetic <Alex> [Fake]'}];
  const before=structuredClone(people);
  const {flow,calls,native}=fixture({getVolunteers:()=>people});
  native.membership.role_name='Test Greeter [Mock]';
  flow.select('7');await flow.load();const html=flow.panel();
  assert.match(html,/<option value="7" selected>Casey Example<\/option>/);
  assert.match(html,/<option value="8" >&lt;Alex&gt;<\/option>/);
  assert.match(html,/<h3>Greeter<\/h3>/);
  assert.doesNotMatch(html,/Fictional|Synthetic|Fake|Mock|<Alex>/);
  await flow.record(intent);assert.deepEqual(calls[0].body,{volunteer_id:7});
  assert.deepEqual(calls[2].body,hashes);
  assert.deepEqual(people,before);assert.equal(native.membership.role_name,'Test Greeter [Mock]');
});
