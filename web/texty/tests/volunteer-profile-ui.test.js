import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

function fixture() {
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},showModal(){},close(){}}]));
  const listeners=new Map(), calls=[], state=seed(), storage=new Map();
  state.volunteers[0].id='1';state.volunteers[1].id='2';state.volunteers[0].can_start_text_setup=true;
  state.proposals=[];state.escalations=[];
  state.messages=[{id:'1',phone:state.volunteers[0].phone,body:'First volunteer <safe> text',direction:'inbound',created_at:'2026-10-06T17:00:00Z'},
    {id:'2',phone:state.volunteers[1].phone,body:'Second volunteer private text',direction:'outbound',status:'queued'},
    {id:'3',phone:state.volunteers[0].phone,body:'Gloo welcome',direction:'outbound',status:'queued',created_at:'2026-10-06T17:01:00Z'}];
  const config={connected:true,aiReady:true,adminReplyAvailable:true,messagingTransport:'mac_messages',humanConfirmationRequired:true};
  let failure=0, receipt={delivery:'awaiting_confirmation',approval_id:11}, release;
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'#access_token=synthetic-profile-session',pathname:'/texty'};
  globalThis.history={replaceState(){globalThis.location.hash='';}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.FormData=class {constructor(form){this.entries=form.data;}[Symbol.iterator](){return Object.entries(this.entries)[Symbol.iterator]();}};
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});let result;
    if(path==='/api/config')result=config;
    else if(path==='/api/state')result=state;
    else if(path==='/api/setup')result={details:{church_name:'Synthetic church'},completed:true};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/setup/admin-texts')result={enabled:false,issues:[],recent:[]};
    else if(path==='/api/volunteers/1/text-setup'){
      if(release)await release;
      if(failure)return {ok:false,status:failure,json:async()=>({detail:failure===401?'Session expired. Sign in again.':'Gloo unavailable. Nothing sent.'})};
      state.volunteers[0].can_start_text_setup=false;
      result=receipt;
    }else throw Error('Unexpected request '+path);
    return {ok:true,json:async()=>result};
  };
  return {elements,listeners,calls,state,config,storage,
    click:dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}}),
    failure:value=>failure=value,receipt:value=>receipt=value,wait:value=>release=value,
    restore(){for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}};
}

test('volunteer profiles replace Messages navigation and isolate histories with honest delivery status',async()=>{
  const f=fixture();
  try {
    await import('../public/app.js?profile-history');
    await f.click({page:'volunteers'});
    let html=f.elements.get('#app').innerHTML;
    assert.doesNotMatch(html,/data-page="messages"|signup-invitation-form|Second volunteer private text/);
    assert.match(html,/data-volunteer="1"/);
    await f.click({volunteer:'1'});html=f.elements.get('#app').innerHTML;
    assert.match(html,/Text history/);assert.match(html,/First volunteer &lt;safe&gt; text/);
    assert.match(html,/Received/);assert.match(html,/Queued for Messages/);
    assert.doesNotMatch(html,/Second volunteer private text|<safe>|reply-recipient/);
    assert.match(html,/data-text-setup="1" >Send welcome message/);
    assert.match(html,/type="hidden" name="volunteer_id" value="1"/);
    await f.click({volunteer:'2'});html=f.elements.get('#app').innerHTML;
    assert.match(html,/Second volunteer private text/);assert.doesNotMatch(html,/First volunteer/);
    assert.ok(f.calls.every(c=>!c.options.body));
  }finally{f.restore();}
});

test('profile welcome uses only existing Gloo setup, preserves failures and holds, and blocks duplicate clicks',async()=>{
  const f=fixture();
  try {
    await import('../public/app.js?profile-welcome');await f.click({volunteer:'1'});
    f.failure(503);await f.click({textSetup:'1'});
    assert.match(f.elements.get('#toast').textContent,/Gloo unavailable/);
    f.config.messagingTransport='google_voice';await f.click({textSetup:'1'});
    f.config.messagingTransport='mac_messages';f.config.aiReady=false;await f.click({textSetup:'1'});
    f.config.aiReady=true;f.state.volunteers[0].consent=false;await f.click({textSetup:'1'});
    assert.equal(f.calls.filter(c=>c.path.endsWith('/text-setup')).length,1);
    f.state.volunteers[0].consent=true;f.failure(0);
    let release;f.wait(new Promise(resolve=>{release=resolve;}));
    const first=f.click({textSetup:'1'});
    await f.click({textSetup:'1'});
    release();await first;
    const requests=f.calls.filter(c=>c.path.endsWith('/text-setup'));
    assert.equal(requests.length,2);
    assert.deepEqual(JSON.parse(requests[1].options.body),{});
    assert.equal(requests[1].options.headers.Authorization,'Bearer synthetic-profile-session');
    assert.match(f.elements.get('#toast').textContent,/awaits your exact review/);
    await f.click({textSetup:'1'});assert.equal(f.calls.filter(c=>c.path.endsWith('/text-setup')).length,2);
    assert.ok(!f.calls.some(c=>c.path.includes('/approve')||c.path.endsWith('/send')||c.path==='/api/signup-invitations'));
  }finally{f.restore();}
});

test('profile welcome never reports success for an unconfirmed receipt or keeps an expired login',async()=>{
  const f=fixture();
  try {
    await import('../public/app.js?profile-invalid-receipt');await f.click({volunteer:'1'});
    f.receipt({delivery:'held'});await f.click({textSetup:'1'});
    assert.match(f.elements.get('#toast').textContent,/did not confirm/);
    f.state.volunteers[0].can_start_text_setup=true;f.failure(401);await f.click({textSetup:'1'});
    assert.equal(f.storage.size,0);assert.match(f.elements.get('#app').innerHTML,/Welcome back/);
  }finally{f.restore();}
});

test('profile reply rejects a different volunteer and navigating away clears the recipient draft',async()=>{
  const f=fixture(),error={textContent:''};
  try {
    await import('../public/app.js?profile-recipient-binding');await f.click({volunteer:'1'});
    const form={id:'admin-reply-form',data:{volunteer_id:'2',body:'Synthetic body'},querySelector:s=>s==='.error'?error:{disabled:false}};
    await f.listeners.get('submit')({preventDefault(){},target:form});
    assert.match(error.textContent,/Open this volunteer/);assert.ok(!f.calls.some(c=>c.path==='/api/reply'));
    f.listeners.get('input')({target:{id:'reply-body',value:'Private draft for first volunteer'}});
    await f.click({volunteer:'2'});assert.doesNotMatch(f.elements.get('#app').innerHTML,/Private draft for first volunteer/);
  }finally{f.restore();}
});
