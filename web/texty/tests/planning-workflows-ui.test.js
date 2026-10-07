import test from 'node:test';
import assert from 'node:assert/strict';
import {createPlanningWorkflows, planningAdapter, normalizeCollection, collectionCard} from '../public/planning-workflows.js';
import {seed} from '../public/domain.js';

const hash = 'a'.repeat(64);
const raw = overrides => ({id:42,month:'2026-11',status:'pending',content_hash:hash,
  expires_at:'2026-10-03T20:00:00Z',scope:{timezone:'America/Denver',recipient_count:1,
    recipients:[{volunteer_id:1,name:'Casey Example',phone:'+12025550199'}],excluded_counts:{missing_consent:2}},
  composition_status:'not_started',text_review_ids:[],...overrides});
function controller(adapter, overrides={}) {
  return createPlanningWorkflows({adapter,getMode:()=> 'live',getToken:()=> 'synthetic-token',render(){},...overrides});
}

test('request reviews the real scope; approve and reject use only parent ID and displayed hash',async()=>{
  const calls=[];let rows=[];
  const api=async(path,body)=>{
    calls.push({path,body});
    if(!body)return {collections:rows};
    if(body.month){rows=[raw({month:body.month})];return {collection:rows[0],sent:0};}
    rows=[raw({status:path.endsWith('/reject')?'rejected':'approved',composition_status:path.endsWith('/retry')?'reviews_pending':'not_started',text_review_ids:path.endsWith('/retry')?[70]:[]})];
    return {collection:rows[0],sent:0,delivery_enabled:false};
  };
  const flow=controller(planningAdapter(api));await flow.load();await flow.request('2026-11');
  assert.equal(calls.filter(call=>call.body).length,1);
  assert.equal(calls[1].body.month,'2026-11');assert.match(calls[1].body.request_id,/^[0-9a-f-]{36}$/);assert.deepEqual(Object.keys(calls[1].body).sort(),['month','request_id']);
  assert.match(flow.panel(),/Casey Example/);assert.match(flow.panel(),/missing consent: 2/);
  assert.match(flow.panel(),/Approve collection/);assert.match(flow.panel(),/review it separately/);
  await flow.decide(42,hash,'approve');
  assert.deepEqual(calls[2],{path:'/api/planning/availability-collections/42/approve',body:{content_hash:hash}});
  assert.match(flow.panel(),/0 individual text reviews prepared/);
  assert.match(flow.panel(),/Prepare texts for review/);assert.equal(calls.length,3);
  await flow.decide(42,hash,'retry');
  assert.equal(calls.at(-1).path,'/api/planning/availability-collections/42/retry');
  assert.match(flow.panel(),/1 individual text review prepared/);
  assert.match(flow.panel(),/does not approve these texts/);
  assert.match(flow.panel(),/data-page="volunteers"/);
  assert.ok(calls.every(call=>!call.path.includes('/api/proposals/')&&!call.path.includes('/send')));
  rows=[raw()];await flow.load();await flow.decide(42,hash,'reject');
  assert.equal(calls.at(-1).path,'/api/planning/availability-collections/42/reject');
  assert.match(flow.panel(),/No collection was started/);
});

test('a changed scope refreshes the review and never substitutes a new hash into the prior approval',async()=>{
  let row=normalizeCollection(raw()),writes=0;
  const flow=controller({list:async()=>[row],request:async()=>row,decide:async()=>{
    writes++;row=normalizeCollection(raw({content_hash:'b'.repeat(64),scope:{recipients:[],excluded_counts:{care_hold:1}}}));
    throw Error('Scope changed. Request a new review.');
  }});
  await flow.load();await flow.decide(42,hash,'approve');
  assert.equal(writes,1);assert.match(flow.panel(),/Scope changed/);
  await flow.decide(42,hash,'approve');assert.equal(writes,1);
  assert.match(flow.panel(),/This review changed/);
});

test('held composition retries only the approved parent; it never approves child texts',async()=>{
  const row=normalizeCollection(raw({status:'approved',composition_status:'held'})),calls=[];
  const flow=controller({list:async()=>[row],request:async()=>row,decide:async(...args)=>{
    calls.push(args);return normalizeCollection(raw({status:'approved',composition_status:'reviews_pending',text_review_ids:[70,71]}));
  }});
  await flow.load();assert.match(flow.panel(),/Retry preparing texts/);
  assert.doesNotMatch(flow.panel(),/>Approve collection</);
  await flow.decide(42,hash,'retry');assert.deepEqual(calls,[[42,'retry',hash]]);
  assert.match(flow.panel(),/2 individual text reviews prepared/);
  assert.doesNotMatch(flow.panel(),/Retry preparing texts/);
});

test('uncertain month requests reuse their UUID while a changed-scope decision needs a fresh review',async()=>{
  const requests=[];let fail=true,row=normalizeCollection(raw());
  const flow=controller({list:async()=>[row],request:async(month,request_id)=>{
    requests.push({month,request_id});if(fail)throw Error('Temporary response failure');return row;
  },decide:async()=>{throw Object.assign(Error('Scope changed.'),{status:409});}});
  await flow.load();await flow.request('2026-11');fail=false;await flow.request('2026-11');
  assert.equal(requests[0].request_id,requests[1].request_id);
  await flow.decide(42,hash,'approve');assert.match(flow.panel(),/data-planning-decision="approve"[^>]*disabled/);
  assert.match(flow.panel(),/request a new review/);
  row=normalizeCollection(raw({id:43,content_hash:'b'.repeat(64)}));await flow.request('2026-11');
  assert.notEqual(requests[2].request_id,requests[1].request_id);
  assert.match(flow.panel(),/data-planning-id="43"/);
});

test('repeated request clicks queue one parent request and an account change discards its response',async()=>{
  let release,calls=0,token='account-a';
  const flow=controller({list:async()=>[],request:()=>{calls++;return new Promise(resolve=>{release=resolve;});}}, {getToken:()=>token});
  await flow.load();const first=flow.request('2026-11');await flow.request('2026-12');
  assert.equal(calls,1);assert.match(flow.panel(),/Working…/);
  token='account-b';release(normalizeCollection(raw()));await first;
  assert.doesNotMatch(flow.panel(),/Casey Example/);
});

test('disconnected preview and invalid months have no collection writes',async()=>{
  let calls=0;
  const adapter={list:async()=>{calls++;return [];},request:async()=>{calls++;return normalizeCollection(raw());}};
  const offline=controller(adapter,{getMode:()=> 'demo',getToken:()=>null});
  await offline.load();assert.match(offline.panel(),/preview is disconnected/);
  assert.doesNotMatch(offline.panel(),/<form|data-planning-decision|data-planning-refresh/);
  await assert.rejects(offline.request('2026-11'),/Sign in/);assert.equal(calls,0);
  const live=controller(adapter);await live.request('2026-13');assert.equal(calls,0);
  assert.match(live.panel(),/Choose a specific month/);
});

test('scope values are escaped and missing review hashes cannot enable a decision',()=>{
  const html=collectionCard(normalizeCollection(raw({content_hash:null,scope:{recipients:[{name:'<script>',phone:'<img>'}],excluded_counts:{}}})));
  assert.doesNotMatch(html,/<script>|<img>/);assert.match(html,/&lt;script&gt;/);
  assert.match(html,/data-planning-decision="approve"[^>]*disabled/);
});

test('held preparation shows its reason and retry time without claiming a zero-ready collection finished',()=>{
  const html=collectionCard(normalizeCollection(raw({status:'approved',composition_status:'held',
    hold_reason:'Gloo is waiting for its retry time.',retry_at:'2026-10-03T20:02:00Z',remaining_recipient_count:0,
    authorization_expires_at:'2026-12-01T07:00:00Z'})));
  assert.match(html,/AI is waiting for its retry time/);assert.match(html,/Preparation can retry after/);
  assert.match(html,/0 recipients ready for text preparation/);assert.match(html,/Retry preparing texts/);
  assert.match(html,/Collection authorization expires/);
  assert.doesNotMatch(html,/No remaining recipients need/);
});

function appFixture() {
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(key=>[key,globalThis[key]]));
  const elements=new Map(['#app','#modal','#toast'].map(key=>[key,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},open:false}]));
  const listeners=new Map(),calls=[];
  globalThis.document={querySelector:key=>elements.get(key),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.location={hash:'#access_token=synthetic-token',pathname:'/texty'};globalThis.history={replaceState(){}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.FormData=class{constructor(form){this.data=form.data;}[Symbol.iterator](){return Object.entries(this.data)[Symbol.iterator]();}};
  return {elements,calls,click:dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}}),
    submit:form=>listeners.get('submit')({preventDefault(){},target:form}),
    restore(){for(const[key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}};
}

test('actual Schedule form and collection buttons use signed-in parent endpoints, never generic or text approvals',async()=>{
  const f=appFixture(),state=seed();let collections=[];
  state.proposals=[{id:'42',intent:'confirm_collection',status:'pending'}];
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options});let data;
    if(path==='/api/config')data={connected:true,aiReady:true,macBridgeConnected:true};
    else if(path==='/api/state')data=state;
    else if(path==='/api/setup')data={details:{church_name:'Synthetic church'},completed:true,revision:1};
    else if(path==='/api/setup/contacts')data={contacts:[]};
    else if(path==='/api/setup/admin-texts')data={enabled:false,recent:[],issues:[]};
    else if(path==='/api/planning/availability-collections'){
      if(options.body){const body=JSON.parse(options.body);assert.equal(body.month,'2026-11');assert.match(body.request_id,/^[0-9a-f-]{36}$/);collections=[raw()];data={collection:collections[0]};}
      else data={collections};
    }else if(path==='/api/planning/availability-collections/42/approve'){
      assert.deepEqual(JSON.parse(options.body),{content_hash:hash});
      collections=[raw({status:'approved',composition_status:'not_started',text_review_ids:[]})];data={collection:collections[0]};state.proposals=[];
    }else if(path==='/api/planning/availability-collections/42/retry'){
      assert.deepEqual(JSON.parse(options.body),{content_hash:hash});collections=[raw({status:'approved',composition_status:'reviews_pending',text_review_ids:[70]})];data={collection:collections[0]};
    }else throw Error('Unexpected request '+path);
    assert.equal(options.headers.Authorization,path==='/api/config'?undefined:'Bearer synthetic-token');
    return{ok:true,json:async()=>data};
  };
  try{
    await import('../public/app.js?planning-workflows');await f.click({page:'messages'});
    assert.match(f.elements.get('#app').innerHTML,/Availability collection needs a scope review/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/data-approve="42"|data-reject="42"/);
    await f.click({page:'schedule'});
    assert.match(f.elements.get('#app').innerHTML,/id="planning-month-form"/);
    await f.submit({id:'planning-month-form',data:{month:'2026-11'},querySelector:()=>({disabled:false})});
    assert.match(f.elements.get('#app').innerHTML,/Casey Example/);
    assert.equal(f.calls.filter(call=>call.options.body).length,1);
    await f.click({planningDecision:'approve',planningId:'42',planningHash:hash});
    assert.match(f.elements.get('#app').innerHTML,/0 individual text reviews prepared/);
    assert.match(f.elements.get('#app').innerHTML,/Prepare texts for review/);
    assert.equal(f.calls.filter(call=>call.options.body).length,2);
    await f.click({planningDecision:'retry',planningId:'42',planningHash:hash});
    assert.match(f.elements.get('#app').innerHTML,/1 individual text review prepared/);
    assert.ok(!f.calls.some(call=>call.path.includes('/api/proposals/')||call.path.includes('/send')));
  }finally{f.restore();}
});


test('policy-suppressed collection explains the hold without preparation or queue claims',()=>{
  const html=collectionCard(normalizeCollection(raw({status:'approved',composition_status:'blocked_policy',
    hold_reason:'Monthly availability requests are disabled by the saved quiet-text policy.',suppressed_recipient_count:2,remaining_recipient_count:0})));
  assert.match(html,/Suppressed by conversation rules, not queued/);
  assert.match(html,/2 recipients suppressed/);assert.match(html,/quiet-text policy/);
  assert.match(html,/No texts were prepared or queued/);
  assert.doesNotMatch(html,/data-planning-decision="retry"|Prepare one text at a time/);
});

test('late prior-account collection reads cannot erase the new account or unlock its action',async()=>{
  let token='account-a',releaseOld,releaseNew,writeCalls=0;
  const flow=controller({list:()=>token==='account-a'?new Promise(resolve=>{releaseOld=resolve;}):Promise.resolve([normalizeCollection(raw({scope:{recipients:[{name:'New account',phone:'+12025550199'}]}}))]),
    request:()=>{writeCalls++;return new Promise(resolve=>{releaseNew=resolve;});}}, {getToken:()=>token});
  const old=flow.load();token='account-b';flow.reset();await flow.load();
  const current=flow.request('2026-11');
  releaseOld([normalizeCollection(raw())]);await old;
  assert.match(flow.panel(),/New account/);assert.match(flow.panel(),/Working…/);
  await flow.request('2026-12');assert.equal(writeCalls,1);
  releaseNew(normalizeCollection(raw()));await current;
});


test('collection recipient names remove display markers without changing canonical scope',()=>{
  const row=normalizeCollection(raw());row.recipients[0].name='Casey <Example> [Fictional]';
  const before=structuredClone(row),html=collectionCard(row);
  assert.match(html,/Casey &lt;Example&gt;/);assert.doesNotMatch(html,/Fictional|<Example>/);
  assert.deepEqual(row,before);
});
