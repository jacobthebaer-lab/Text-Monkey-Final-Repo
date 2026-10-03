import test from 'node:test';
import assert from 'node:assert/strict';
import {createCloudTexting, parseSessionCookies} from '../public/cloud-texting.js';
import {seed} from '../public/domain.js';

const connected = {authorized:true, provider:'google_voice', enabled:true, live_enabled:true, paused:false, state:'ready', gloo_ready:true, test_recipients:1, connection:{connected:true,state:'ready',account_email:'fixture@example.test',number:'+12025550199'}, queue:{queued:2,submitted:1,uncertain:1}};
const cookie = {name:'SYNTHETIC', value:'synthetic-secret-never-render', domain:'.google.com', path:'/'};
function fixture({superadmin=true, available=true, response=connected}={}) {
  const calls = []; let token='fixture-bearer', next=response, failure=null, renders=0;
  const ui=createCloudTexting({getMode:()=> 'live', getToken:()=>token, getConfig:()=>({cloudTextingAvailable:available}), render:()=>{renders++;}, api:async(path,body,options)=>{
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

test('queue acknowledgment and incomplete verification never claim carrier delivery',async()=>{
  const f=fixture();await f.ui.load();
  assert.equal(f.ui.summary().connected,true);
  assert.match(f.ui.screen(),/Submitted to Google Voice/);
  assert.match(f.ui.screen(),/does not prove delivery/);
  assert.match(f.ui.screen(),/Needs delivery check/);
  f.setNext({...connected,state:'paused',paused:true,connection:{connected:false,state:'verification_required'}});
  await f.ui.load();
  assert.equal(f.ui.summary().connected,false);
  assert.match(f.ui.screen(),/Finish Google verification/);
  assert.match(f.ui.screen(),/Resume outgoing texts/);
  f.setNext({...connected,state:'disabled',enabled:false});await f.ui.load();
  assert.equal(f.ui.summary().connected,false);
  assert.match(f.ui.screen(),/Cloud texting disabled/);
  f.setNext({...connected,live_enabled:false,state:'live_disabled'});await f.ui.load();
  assert.equal(f.ui.summary().connected,false);
  assert.match(f.ui.screen(),/Live texting disabled/);
});

test('pause sends a boolean and renders server-returned status',async()=>{
  const f=fixture();await f.ui.load();
  f.setNext({...connected,paused:true,state:'paused'});
  await f.ui.action('pause');
  const call=f.calls.find(row=>row.body);
  assert.equal(call.path,'/api/cloud-texting/pause');
  assert.deepEqual(call.body,{paused:true});
  assert.deepEqual(call.options,{keepSessionOnForbidden:true});
  assert.match(f.ui.screen(),/Resume outgoing texts/);
});

test('session import clears credentials on success, rejection and malformed input',async()=>{
  const f=fixture();await f.ui.load();
  const input={value:JSON.stringify([cookie])}, form={querySelector:()=>input};
  await f.ui.submit(form);
  assert.equal(input.value,'');
  assert.deepEqual(f.calls.find(call=>call.path.endsWith('/session')).body,{cookies:[cookie]});
  assert.doesNotMatch(f.ui.screen(),/synthetic-secret-never-render/);
  f.setFailure(Object.assign(new Error(cookie.value),{status:422}));
  input.value=JSON.stringify([cookie]);await f.ui.submit(form);
  assert.equal(input.value,'');
  assert.match(f.ui.screen(),/session could not be accepted/);
  assert.doesNotMatch(f.ui.screen(),/synthetic-secret-never-render/);
  input.value=`${cookie.value} invalid-json`;await f.ui.submit(form);
  assert.equal(input.value,'');
  assert.match(f.ui.screen(),/JSON array/);
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

test('unexpected server text is escaped; only the specified cookie structure is accepted',async()=>{
  const f=fixture({response:{...connected,connection:{...connected.connection,account_email:'<script>unsafe</script>'}}});await f.ui.load();
  assert.doesNotMatch(f.ui.screen(),/<script>/);
  assert.match(f.ui.screen(),/&lt;script&gt;/);
  assert.throws(()=>parseSessionCookies('{"cookies":[]}'),/JSON array/);
  assert.throws(()=>parseSessionCookies(JSON.stringify([{name:'bad'}])),/name, value and domain/);
  assert.throws(()=>parseSessionCookies('x'.repeat(60001)),/60 KB/);
  assert.deepEqual(parseSessionCookies(JSON.stringify([cookie])),[cookie]);
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
    assert.match(elements.get('#app').innerHTML,/id="cloud-session-form"/);
    assert.doesNotMatch(elements.get('#app').innerHTML,/laptop Messages connection is offline/);
    forbidden=true;await click({cloudAction:'refresh'});
    assert.doesNotMatch(elements.get('#app').innerHTML,/id="cloud-session-form"/);
    assert.match(elements.get('#app').innerHTML,/Coordinator workspace/);
    assert.equal(storage.get('texty.coordinator.session.v1'),'synthetic-session');
  } finally {for(const[key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
