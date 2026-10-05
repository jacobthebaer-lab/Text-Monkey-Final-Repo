import test from 'node:test';
import assert from 'node:assert/strict';
import {createCloudTexting} from '../public/cloud-texting.js';
import {seed} from '../public/domain.js';

const connected = {authorized:true, provider:'google_voice', enabled:true, live_enabled:true, paused:false, state:'ready', gloo_ready:true, test_recipients:1, connection:{connected:true,state:'ready',account_email:'fixture@example.test',number:'+12025550199'}, queue:{queued:2,submitted:1,uncertain:1}};
const cookie = {name:'SYNTHETIC', value:'synthetic-secret-never-render', domain:'.google.com', path:'/'};
function fixture({superadmin=true, available=true, response=connected, disconnectedPreview=false}={}) {
  const calls = []; let token='fixture-bearer', next=response, failure=null, renders=0;
  const ui=createCloudTexting({disconnectedPreview,getMode:()=> 'live', getToken:()=>token, getConfig:()=>({cloudTextingAvailable:available}), render:()=>{renders++;}, api:async(path,body,options)=>{
    calls.push({path,body,options});
    if (failure) throw failure;
    if (path==='/api/auth/me') return {superadmin,email:'fixture@example.test'};
    return next;
  }});
  return {ui,calls,setToken:value=>{token=value;},setNext:value=>{next=value;},setFailure:value=>{failure=value;},renders:()=>renders};
}

test('connection controls require a signed-in superadmin and do not exist in the public demo', async()=>{
  for(const options of [{superadmin:false}, {available:false}]) {
    const f=fixture(options); await f.ui.load();
    assert.equal(f.ui.screen(),'');
    assert.ok(f.calls.every(call=>call.path==='/api/auth/me'));
    await f.ui.action('pause');
    assert.ok(f.calls.every(call=>!call.body));
  }
  const f=fixture();f.setToken(null);await f.ui.load();
  assert.equal(f.ui.screen(),'');assert.equal(f.calls.length,0);
});

test('explicit disconnected preview supports role and pause controls but cannot import credentials or claim a connection',async()=>{
  let superadmin=true, paused=true;
  const calls=[];
  const ui=createCloudTexting({disconnectedPreview:true,getMode:()=> 'demo',getToken:()=> 'synthetic-preview',getConfig:()=>({cloudTextingAvailable:true}),render(){},api:async(path,body)=>{
    calls.push({path,body});
    if(path==='/api/auth/me') return {superadmin};
    if(body) paused=body.paused;
    return {...connected,paused,held_inbound:{held_gloo:1}};
  }});
  await ui.load();
  assert.match(ui.screen(),/Disconnected preview/);
  assert.match(ui.screen(),/Simulated outage; sample replies held/);
  assert.match(ui.screen(),/automation remains held by provider policy/);
  assert.doesNotMatch(ui.screen(),/cloud-session-form|Google session cookies|fixture@example|12025550199/);
  assert.deepEqual(ui.summary(),{connected:false,label:'Disconnected preview'});
  await ui.action('pause');assert.equal(paused,false);
  assert.match(ui.screen(),/Resumed, but disconnected/);
  assert.equal(ui.summary().connected,false);
  const input={value:JSON.stringify([cookie])};
  await ui.submit({querySelector:()=>input});
  assert.equal(input.value,'');
  assert.ok(calls.every(call=>!call.path.endsWith('/session')));
  superadmin=false;await ui.load();
  assert.equal(ui.screen(),'');
  const before=calls.length;await ui.action('pause');assert.equal(calls.length,before);
});

test('stale connected flags and completed ID verification cannot release the displayed policy hold',async()=>{
  const f=fixture();await f.ui.load();
  for(const state of ['ready','connected','paused','disabled','live_disabled','verification_required','provider_policy_hold']) {
    f.setNext({...connected,state,identity_verified:true,verification_complete:true});await f.ui.load();
    assert.deepEqual(f.ui.summary(),{connected:false,label:'Google Voice automation held'});
    assert.match(f.ui.screen(),/including after account or ID approval/);
    assert.match(f.ui.screen(),/Historical submission records do not prove delivery/);
    assert.doesNotMatch(f.ui.screen(),/cloud-session-form|data-cloud-action="pause"|Google Voice session connected|fixture@example|12025550199/);
  }
});

test('live pause and resume events cannot mutate transport, including with a stale preview flag',async()=>{
  for(const disconnectedPreview of [false,true]) {
    const f=fixture({disconnectedPreview});await f.ui.load();
    const before=f.calls.length;
    for(const action of ['pause','resume','connect']) await f.ui.action(action);
    assert.equal(f.calls.length,before);
    assert.equal(f.ui.summary().connected,false);
    assert.match(f.ui.screen(),/Google Voice automation held/);
    assert.doesNotMatch(f.ui.screen(),/Simulate resume|Simulate pause/);
  }
});

test('legacy credential forms are cleared without parsing, retaining or transmitting their input',async()=>{
  const f=fixture();await f.ui.load();
  const input={value:JSON.stringify([cookie])}, form={querySelector:()=>input};
  const before=f.calls.length;
  await f.ui.submit(form);
  assert.equal(input.value,'');
  assert.equal(f.calls.length,before);
  assert.doesNotMatch(f.ui.screen(),/synthetic-secret-never-render/);
  input.value=`${cookie.value} invalid-json`;await f.ui.submit(form);
  assert.equal(input.value,'');
  assert.equal(f.calls.length,before);
  let cleared=false;
  await f.ui.submit({querySelector:()=>({get value(){throw Error('Credential value must not be read');},set value(value){assert.equal(value,'');cleared=true;}})});
  assert.equal(cleared,true);
  assert.equal(f.calls.length,before);
  assert.doesNotMatch(f.ui.screen(),/synthetic-secret-never-render/);
});

test('forbidden status revokes controls without turning a role check into logout',async()=>{
  const f=fixture();await f.ui.load();
  f.setFailure(Object.assign(new Error('forbidden'),{status:403}));await f.ui.action('refresh');
  assert.equal(f.ui.screen(),'');
  assert.equal(f.ui.summary().connected,false);
  assert.ok(f.calls.every(call=>call.options.keepSessionOnForbidden===true));
});

test('a late response cannot restore superadmin state after signout',async()=>{
  let release;let token='token';
  const ui=createCloudTexting({getMode:()=> 'live',getToken:()=>token,getConfig:()=>({cloudTextingAvailable:true}),render(){},api:()=>new Promise(resolve=>{release=resolve;})});
  const loading=ui.load(); token=null;ui.reset();release({superadmin:true});await loading;
  assert.equal(ui.screen(),'');assert.equal(ui.summary(),null);
});

test('untrusted status fields and service failures cannot alter the policy label or expose account data',async()=>{
  const f=fixture({response:{...connected,state:'<script>unsafe</script>',queue:{queued:'<img src=x onerror=unsafe>'},connection:{...connected.connection,account_email:'<script>unsafe</script>'}}});await f.ui.load();
  assert.doesNotMatch(f.ui.screen(),/unsafe|<script>|<img/);
  assert.equal(f.ui.summary().label,'Google Voice automation held');
  f.setFailure(Object.assign(new Error(cookie.value),{status:503}));await f.ui.action('refresh');
  assert.match(f.ui.screen(),/Google Voice automation remains held/);
  assert.doesNotMatch(f.ui.screen(),/synthetic-secret-never-render/);
  assert.equal(f.ui.summary().connected,false);
});

test('the actual Settings page gates cloud controls and a cloud 403 keeps the coordinator signed in',async()=>{
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval'];
  const saved=Object.fromEntries(keys.map(key=>[key,globalThis[key]]));
  const elements=new Map(['#app','#modal','#toast'].map(key=>[key,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},open:false}]));
  const listeners=new Map(), storage=new Map();let forbidden=false;
  globalThis.document={querySelector:key=>elements.get(key),addEventListener:(name,callback)=>listeners.set(name,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){throw Error('Cloud credentials must not be saved');}};
  globalThis.sessionStorage={getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
  globalThis.location={hash:'#access_token=synthetic-session',pathname:'/texty'};
  globalThis.history={replaceState(){}};globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.fetch=async(path,options)=>{
    let value;
    if(path==='/api/config') value={connected:true,aiReady:true,messagingTransport:'google_voice',cloudTextingAvailable:true,automationEnabled:true};
    else if(path==='/api/state') value=seed();
    else if(path==='/api/setup') value={details:{church_name:'Synthetic church'},completed:true,revision:1};
    else if(path==='/api/setup/contacts') value={contacts:[]};
    else if(path==='/api/setup/admin-texts') value={enabled:false,issues:[],recent:[]};
    else if(path==='/api/auth/me') value={email:'fixture@example.test',superadmin:true};
    else if(path==='/api/cloud-texting') {
      assert.equal(options.headers.Authorization,'Bearer synthetic-session');
      if(forbidden) return {ok:false,status:403,json:async()=>({detail:'Superadmin access revoked'})};
      value=connected;
    } else throw Error('Unexpected request '+path);
    return {ok:true,json:async()=>value};
  };
  try {
    await import('../public/app.js?cloud-role-integration');
    const click=async dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}});
    await click({page:'settings'});
    assert.match(elements.get('#app').innerHTML,/id="cloud-texting-title"/);
    assert.match(elements.get('#app').innerHTML,/Google Voice automation held/);
    assert.doesNotMatch(elements.get('#app').innerHTML,/id="cloud-session-form"|data-cloud-action="pause"/);
    assert.doesNotMatch(elements.get('#app').innerHTML,/laptop Messages connection is offline/);
    forbidden=true;await click({cloudAction:'refresh'});
    assert.doesNotMatch(elements.get('#app').innerHTML,/id="cloud-texting-title"/);
    assert.match(elements.get('#app').innerHTML,/Coordinator workspace/);
    assert.equal(storage.get('texty.coordinator.session.v1'),'synthetic-session');
  } finally {for(const[key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});


test('bounded demo displays exact reviewed choices and only performs separate explicit steps',async()=>{
  const response={...connected,demo_mode:true,reviewed_messages:[{id:42,phone:'+12025550102',body:'Exact reviewed synthetic text.',body_hash:'a'.repeat(64)}]};
  const f=fixture({response});await f.ui.load();
  assert.match(f.ui.screen(),/Check inbox once/);
  assert.match(f.ui.screen(),/Exact reviewed synthetic text/);
  assert.match(f.ui.screen(),/Send this reviewed text/);
  assert.doesNotMatch(f.ui.screen(),/Enter.*(?:message ID|hash)/i);
  await f.ui.action('refresh');
  assert.ok(f.calls.every(call=>!call.path.endsWith('/intake') && !call.path.endsWith('/dispatch')));
  await f.ui.action('intake');
  assert.deepEqual(f.calls.at(-1).body,{});
  assert.equal(f.calls.at(-1).path,'/api/cloud-texting/demo/intake');
  await f.ui.action('dispatch:42');
  assert.equal(f.calls.at(-1).path,'/api/cloud-texting/demo/dispatch');
  assert.deepEqual(f.calls.at(-1).body,{message_id:42,body_hash:'a'.repeat(64)});
  const before=f.calls.length;await f.ui.action('dispatch:43');assert.equal(f.calls.length,before);
});

test('bounded demo session import clears input and does not invoke intake or send',async()=>{
  const f=fixture({response:{...connected,demo_mode:true,paused:true}});await f.ui.load();
  const input={value:JSON.stringify([cookie])};
  await f.ui.submit({querySelector:()=>input});
  assert.equal(input.value,'');
  assert.equal(f.calls.at(-1).path,'/api/cloud-texting/session');
  assert.ok(f.calls.every(call=>!call.path.endsWith('/intake') && !call.path.endsWith('/dispatch')));
  f.setNext({...connected,demo_mode:true,step_result:{action:'dispatch',status:'submitted'}});await f.ui.load();
  assert.match(f.ui.screen(),/device delivery unverified/);
});

test('church demo registers pending signup, reviews exact drafts and starts only a requested window',async()=>{
 const f=fixture({response:{...connected,demo_mode:true,participants:[],pending_reviews:[{id:7,phone:'+12025550102',body:'First invitation. Text STOP to stop.',content_hash:'b'.repeat(64)}]}});
 await f.ui.load();
 assert.match(f.ui.screen(),/first-and-last-name reply/);assert.doesNotMatch(f.ui.screen(),/verbal|checkbox|type YES/i);
 const form=(id,values)=>({id,querySelector:selector=>({value:values[selector.match(/name="([^"]+)"/)[1]]})});
 await f.ui.submit(form('cloud-demo-recipient-form',{phone:'(202) 555-0102',name:''}));
 assert.deepEqual(f.calls.at(-1).body,{phone:'+12025550102',name:'Demo participant'});
 assert.equal(f.calls.at(-1).path,'/api/cloud-texting/demo/recipients');
 assert.ok(f.calls.every(call=>!call.path.endsWith('/dispatch') && !call.path.endsWith('/intake')));
 await f.ui.action('approve:7');
 const approval=f.calls.find(call=>call.path==='/api/proposals/7/approve');
 assert.deepEqual(approval.body,{content_hash:'b'.repeat(64)});
 await f.ui.submit(form('cloud-demo-window-form',{minutes:'15',submission_budget:'100'}));
 assert.deepEqual(f.calls.at(-1).body,{minutes:15,submission_budget:100});
 assert.equal(f.calls.at(-1).path,'/api/cloud-texting/demo/window');
 f.setNext({...connected,demo_mode:true,demo_window:{active:true,until:'synthetic-expiry',reserved_submissions:1,submission_budget:100}});await f.ui.load();
 assert.match(f.ui.screen(),/Church demo window active/);assert.doesNotMatch(f.ui.screen(),/Manual steps only/);
 await f.ui.action('window-stop');assert.equal(f.calls.at(-1).path,'/api/cloud-texting/demo/window/stop');
});

test('cloud sign-in verification uses existing profile without requesting or importing local cookies',async()=>{
 const f=fixture({response:{...connected,demo_mode:true,paused:true,connection:{identity_verified:true,connected:false,state:'baseline_pending'},step_result:{action:'verify_profile',message:'Cloud sender identity verified. Sending remains paused.'}}});
 await f.ui.load();
 assert.match(f.ui.screen(),/Verify cloud sign-in/);
 assert.doesNotMatch(f.ui.screen(),/cloud-session-form|session cookies/);
 await f.ui.action('verify-profile');
 assert.equal(f.calls.at(-1).path,'/api/cloud-texting/demo/verify-profile');
 assert.deepEqual(f.calls.at(-1).body,{});
 assert.ok(f.calls.every(call=>!call.path.endsWith('/session') && !call.path.endsWith('/intake') && !call.path.endsWith('/dispatch')));
 assert.match(f.ui.screen(),/Sending remains paused/);
 assert.equal(f.ui.summary().connected,false);
});
