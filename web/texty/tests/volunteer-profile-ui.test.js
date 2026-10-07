import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

function fixture() {
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},showModal(){},close(){}}]));
  const listeners=new Map(), calls=[], state=seed(), storage=new Map(), histories=new Map();
  state.volunteers[0].id='1';state.volunteers[1].id='2';state.volunteers[0].can_start_text_setup=true;
  state.proposals=[];state.escalations=[];
  state.messages=[{id:'1',phone:state.volunteers[0].phone,body:'First volunteer <safe> text',direction:'inbound',created_at:'2026-10-06T17:00:00Z'},
    {id:'2',phone:state.volunteers[1].phone,body:'Second volunteer private text',direction:'outbound',status:'queued'},
    {id:'3',phone:state.volunteers[0].phone,body:'Gloo welcome',direction:'outbound',status:'queued',created_at:'2026-10-06T17:01:00Z'}];
  const config={connected:true,aiReady:true,adminReplyAvailable:true,messagingTransport:'mac_messages',humanConfirmationRequired:true};
  const timers=[],batches=new Map();let cloneState=false,failure=0, receipt={delivery:'awaiting_confirmation',approval_id:11}, release;
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'#access_token=synthetic-profile-session',pathname:'/texty'};
  globalThis.history={replaceState(){globalThis.location.hash='';}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=fn=>{timers.push(fn);return 0;};
  globalThis.FormData=class {constructor(form){this.entries=form.data;}[Symbol.iterator](){return Object.entries(this.entries)[Symbol.iterator]();}};
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});let result;
    if(path==='/api/config')result=config;
    else if(path==='/api/state')result=state;
    else if(path==='/api/setup')result={details:{church_name:'Synthetic church'},completed:true};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/setup/admin-texts')result={enabled:false,issues:[],recent:[]};
    else if(/^\/api\/volunteers\/\d+\/history\?/.test(path))result=histories.get(path.split('/')[3]);
    else if(path==='/api/welcome-batches'){
      const data=JSON.parse(options.body),rows=batches.get(data.request_id)||[];
      if(rows.length<data.volunteer_ids.length){const id=String(data.volunteer_ids[rows.length]);rows.push({volunteer_id:id,name:'Example '+id,status:'prepared',delivery:'queued_for_mac'});state.volunteers.find(v=>v.id===id).can_start_text_setup=false;}
      batches.set(data.request_id,rows);result={request_id:data.request_id,results:structuredClone(rows),done:rows.length===data.volunteer_ids.length,completed:rows.length,total:data.volunteer_ids.length};
    }else if(path==='/api/volunteers/1/text-setup'){
      if(release)await release;
      if(failure)return {ok:false,status:failure,json:async()=>({detail:failure===401?'Session expired. Sign in again.':'Gloo unavailable. Nothing sent.'})};
      state.volunteers[0].can_start_text_setup=false;
      result=receipt;
    }else throw Error('Unexpected request '+path);
    return {ok:true,json:async()=>path==='/api/state'&&cloneState?structuredClone(result):result};
  };
  return {elements,listeners,calls,state,config,storage,histories,timers,snapshot:()=>{cloneState=true;},
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
    assert.match(f.elements.get('#toast').textContent,/AI unavailable/);
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


test('welcome renders the exact server block, clears it when scope is ready, and never guesses setup is in progress',async()=>{
  const f=fixture();
  try {
    await import('../public/app.js?profile-welcome-block-reasons');
    const person=f.state.volunteers[0];
    person.can_start_text_setup=false;
    for(const reason of [
      'This volunteer is outside the approved texting recipients. Ask the connection owner to review their texting authorization.',
      "This volunteer's approved texting session is not active. Ask the connection owner to review its start and expiry.",
      'Text setup is already in progress. Their next reply continues it. Check text history below.',
      'Gloo text setup is disabled. Ask an administrator to enable text onboarding.',
      'Synthetic <unsafe> reason',
    ]) {
      person.text_setup_block_reason=reason;
      await f.click({volunteer:'1'});
      const html=f.elements.get('#app').innerHTML;
      assert.match(html,/data-text-setup="1" disabled/);
      assert.ok(html.includes(reason.replace(/\bgloo(?:\s+ai)?\b/gi,'AI').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;')));
      assert.doesNotMatch(html,/in progress or the connection|<unsafe>/);
      await f.click({textSetup:'1'});
      assert.ok(!f.calls.some(c=>c.path.endsWith('/text-setup')));
    }
    delete person.text_setup_block_reason;
    await f.click({volunteer:'1'});
    assert.match(f.elements.get('#app').innerHTML,/Welcome availability could not be confirmed. Refresh this profile/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/Text setup is already in progress/);
    person.can_start_text_setup=true;
    await f.click({action:'refresh-volunteer'});
    assert.match(f.elements.get('#app').innerHTML,/data-text-setup="1" >Send welcome message/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/availability could not be confirmed/);
    person.consent=false;
    await f.click({volunteer:'1'});
    assert.match(f.elements.get('#app').innerHTML,/data-text-setup="1" disabled/);
    assert.match(f.elements.get('#app').innerHTML,/Text consent and an active volunteer profile are required/);
  }finally{f.restore();}
});


test('100 fictional profiles show individual three-month histories with simulated labels and no text actions',async()=>{
  const f=fixture();
  try {
    const base={...f.state.volunteers[0],fictional:true,consent:false,can_start_text_setup:false};
    f.state.volunteers=Array.from({length:100},(_,i)=>({...base,id:String(i+1),phone:`+120255501${String(i).padStart(2,'0')}`,first_name:'Fictional',last_name:`Person ${i+1}`,status:i<85?'active':'paused'}));
    for(const id of ['1','2']) f.histories.set(id,{volunteer_id:id,fictional:true,next_before_id:null,
      messages:Array.from({length:19},(_,i)=>({id:`${id}0${i}`,phone:f.state.volunteers[Number(id)-1].phone,fictional:true,status:'simulated',direction:i%2?'inbound':'outbound',
        body:`Person ${id} simulated <reply> ${i}`,created_at:`2026-0${7+i%3}-01T17:00:00Z`}))});
    await import('../public/app.js?fictional-profile-histories');
    await f.click({page:'volunteers'});
    let html=f.elements.get('#app').innerHTML;
    assert.equal((html.match(/class="quiet volunteer-name"/g)||[]).length,100);
    assert.match(html,/100 of 100 volunteers/);
    assert.match(html,/Fictional profile · Texting disabled/);
    await f.click({volunteer:'1'});html=f.elements.get('#app').innerHTML;
    assert.equal((html.match(/class="bubble"/g)||[]).length,19);
    assert.match(html,/Simulated text history/);assert.match(html,/Simulated reply/);
    assert.match(html,/Simulated text/);assert.match(html,/Fictional date:/);
    assert.match(html,/Fictional conversations, dates and replies. No texts were sent/);
    assert.match(html,/Person 1 simulated &lt;reply&gt;/);
    assert.doesNotMatch(html,/Received|Person 2 simulated|data-text-setup|admin-reply-form/);
    await f.click({volunteer:'2'});html=f.elements.get('#app').innerHTML;
    assert.match(html,/Person 2 simulated/);assert.doesNotMatch(html,/Person 1 simulated/);
    f.state.volunteers[1].consent=true;f.state.volunteers[1].can_start_text_setup=true;
    await f.click({textSetup:'2'});
    assert.ok(f.calls.every(c=>!c.options.body));
    const reads=f.calls.filter(c=>c.path.includes('/history?'));
    assert.equal(reads.length,2);
    assert.equal(reads[0].options.headers.Authorization,'Bearer synthetic-profile-session');
  }finally{f.restore();}
});


test('actual roster checkboxes stay selected during Connecting readiness polls and filtered bulk action',async()=>{
  const f=fixture();
  try {
    f.snapshot();f.state.volunteers[1].can_start_text_setup=false;f.state.volunteers[1].text_setup_block_code='enrollment_pending';
    await import('../public/app.js?bulk-checkbox-poll');await f.click({page:'volunteers'});
    assert.match(f.elements.get('#app').innerHTML,/data-welcome-select="1"/);
    assert.match(f.elements.get('#app').innerHTML,/data-welcome-select="2"[^>]*disabled/);
    f.listeners.get('change')({target:{dataset:{welcomeSelect:'1'},checked:true}});
    assert.match(f.elements.get('#app').innerHTML,/data-welcome-select="1"[^>]*checked/);
    globalThis.document.activeElement={tagName:'INPUT',type:'checkbox'};
    f.state.volunteers[1].can_start_text_setup=true;f.state.volunteers[1].text_setup_block_code=null;
    await f.timers[0]();
    assert.match(f.elements.get('#app').innerHTML,/data-welcome-select="1"[^>]*checked/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/data-welcome-select="2"[^>]*disabled/);
    f.listeners.get('change')({target:{dataset:{welcomeSelectAll:''},hasAttribute:name=>name==='data-welcome-select-all',checked:true}});
    assert.match(f.elements.get('#app').innerHTML,/2 selected/);
    await f.click({welcomeAction:'send'});
    const calls=f.calls.filter(c=>c.path==='/api/welcome-batches');assert.equal(calls.length,2);
    assert.deepEqual(JSON.parse(calls[0].options.body).volunteer_ids,[1,2]);
    assert.match(f.elements.get('#app').innerHTML,/0 selected/);assert.match(f.elements.get('#app').innerHTML,/Queued for Messages/);
  }finally{f.restore();}
});
