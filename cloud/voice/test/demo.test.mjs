import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {readConfig, startServer} from '../server.mjs';
import {VoiceBrowser, selectors} from '../browser.mjs';
import {Connector, Store, Hold} from '../core.mjs';

const email='demo@example.test', number='+12025550101', phone='+12025550102';
const start='2026-10-05T12:00:00Z', end='2026-10-05T13:00:00Z', id='a'.repeat(32);
const sessions={[phone]:{id,starts_at:start,expires_at:end}};
const env={VOICE_ENABLED:'true',VOICE_API_TOKEN:'synthetic-token'.padEnd(32,'0'),GOOGLE_VOICE_DEMO_MODE:'true',
 VOICE_EXPECTED_EMAIL:email,VOICE_EXPECTED_NUMBER:number,GOOGLE_VOICE_ALLOWED_PHONES:phone,
 GOOGLE_VOICE_TEST_SESSIONS:JSON.stringify(sessions)};

test('historical demo scope and every enable/signup flag combination remain held',()=>{
 for(const extra of [{VOICE_ENABLED:'false'},{GOOGLE_VOICE_TEST_SESSIONS:'{}'},{VOICE_EXPECTED_NUMBER:phone},
   {GOOGLE_VOICE_ALLOWED_PHONES:''},{VOICE_EXPECTED_EMAIL:''},
   {GOOGLE_VOICE_TEST_SESSIONS:JSON.stringify({[phone]:{id,starts_at:start,expires_at:'2026-10-05T15:00:00Z'}})}]) {
  const config=readConfig({...env,...extra});
  assert.equal(config.enabled,false);assert.equal(config.demoMode,false);assert.equal(config.signupEnabled,false);
 }
 for(const GOOGLE_VOICE_DEMO_MODE of ['false','true'])
  for(const VOICE_ENABLED of ['false','true'])
   for(const GOOGLE_VOICE_SIGNUP_ENABLED of ['false','true']) {
    const config=readConfig({...env,GOOGLE_VOICE_DEMO_MODE,VOICE_ENABLED,GOOGLE_VOICE_SIGNUP_ENABLED});
    assert.equal(config.enabled,false);assert.equal(config.demoMode,false);assert.equal(config.signupEnabled,false);
   }
 assert.equal(readConfig(env).demoMode,false);
 assert.equal(readConfig({VOICE_API_TOKEN:env.VOICE_API_TOKEN}).demoMode,false);
});

test('shipped startup rejects historical demo session and intake without account access',async t=>{
 const directory=await mkdtemp(join(tmpdir(),'voice-demo-'));
 t.after(()=>rm(directory,{recursive:true,force:true}));
 let scans=0, identities=0;
 t.mock.method(VoiceBrowser.prototype,'start',async()=>{});
 t.mock.method(VoiceBrowser.prototype,'close',async()=>{});
 t.mock.method(VoiceBrowser.prototype,'identity',async()=>{identities++;return {email,phone:number};});
 t.mock.method(VoiceBrowser.prototype,'importSession',async()=>{});
 t.mock.method(VoiceBrowser.prototype,'scan',async()=>{scans++;return [];});
 const runtime=await startServer({...readConfig({...env,VOICE_DATA_DIR:directory}),port:0});
 t.after(()=>runtime.close());
 const base=`http://127.0.0.1:${runtime.server.address().port}`;
 const headers={Authorization:`Bearer ${env.VOICE_API_TOKEN}`,'Content-Type':'application/json'};
 assert.equal(scans,0); assert.equal(identities,0);
 assert.equal((await fetch(base+'/demo/intake',{method:'POST',body:'{}'})).status,401);
 let health=await (await fetch(base+'/health',{headers})).json();
 assert.equal(health.demo_mode,false); assert.equal(health.ready,false);
 const response=await fetch(base+'/session',{method:'POST',headers,body:JSON.stringify({cookies:[{name:'SID',value:'synthetic',domain:'.google.com',path:'/'}]})});
 assert.equal(response.status,503);assert.equal(scans,0);assert.equal(identities,0);
 for(let i=0;i<2;i++) await fetch(base+'/health',{headers});
 assert.equal(scans,0);
 health=await (await fetch(base+'/demo/intake',{method:'POST',headers,body:'{}'})).json();
 assert.deepEqual(health,{error:'provider_policy_hold'});
 assert.equal(scans,0);assert.equal(identities,0);
});

async function fixture(t){
 const directory=await mkdtemp(join(tmpdir(),'voice-demo-core-'));
 t.after(()=>rm(directory,{recursive:true,force:true}));
 const store=new Store(directory);await store.load();
 let now='2026-10-05T12:01:00Z', scans=0, sends=0, messages=[];
 const browser={identity:async()=>({email,phone:number}),scan:async()=>{scans++;return messages;},
  prepareSend:async()=>{}, submitSend:async()=>{sends++;return {status:'uncertain'};}};
 const connector=new Connector({store,browser,expectedEmail:email,expectedPhone:number,
  allowedPhones:[phone],demoMode:true,testSessions:sessions,now:()=>now});
 return {connector,store,browser,directory,setMessages:value=>{messages=value;},setNow:value=>{now=value;},sends:()=>sends};
}

test('explicit recipient probe proves exact scope without body entry, scan or send reservation',async t=>{
 const f=await fixture(t);let preparations=0;
 f.browser.prepareRecipient=async target=>{assert.equal(target,phone);preparations++;return {
  count:async()=>1,isVisible:async()=>true,inputValue:async()=>''};};
 f.browser.recipientVerified=async target=>target===phone;
 assert.deepEqual(await f.connector.recipientProbe({phone,session_id:id}),
  {status:'verified',native_submission_attempted:false,recipient_verified:true});
 assert.equal(preparations,1);assert.equal(f.sends(),0);assert.deepEqual(f.store.data.sends,{});
 for(const input of [{phone,session_id:'b'.repeat(32)},{phone:'+12025550103',session_id:id},
  {phone,session_id:id,body:'Must not type'},null]){
  await assert.rejects(f.connector.recipientProbe(input),{code:'invalid_recipient_probe'});
 }
 f.setNow(end);await assert.rejects(f.connector.recipientProbe({phone,session_id:id}),{code:'invalid_recipient_probe'});
 assert.equal(preparations,1);
});

test('recipient probe holds sender mismatch, pending preparation and stale body',async t=>{
 const f=await fixture(t);let preparations=0;
 f.browser.prepareRecipient=async()=>{preparations++;return {count:async()=>1,isVisible:async()=>true,inputValue:async()=>'Existing draft'};};
 f.browser.recipientVerified=async()=>true;
 f.connector.preparation={key:'original',digest:'unchanged',not_after:'2026-10-05T12:01:20Z'};
 await assert.rejects(f.connector.recipientProbe({phone,session_id:id}),{code:'preparation_in_progress'});
 assert.equal(preparations,0);f.connector.preparation=null;
 f.browser.identity=async()=>({email:'different@example.test',phone:number});
 assert.equal((await f.connector.recipientProbe({phone,session_id:id})).status,'held');assert.equal(preparations,0);
 f.browser.identity=async()=>({email,phone:number});
 assert.equal((await f.connector.recipientProbe({phone,session_id:id})).reason_code,'recipient_probe_not_verified');
 assert.equal(preparations,1);assert.equal(f.sends(),0);assert.deepEqual(f.store.data.sends,{});
});

test('explicit baseline skips history, next check accepts bounded new messages and holds excessive scans',async t=>{
 const f=await fixture(t);
 f.setMessages([{id:'history',phone,body:'synthetic history',received_at:start}]);
 await f.connector.poll();assert.deepEqual(f.connector.inbound().messages,[]);
 f.setMessages([{id:'new',phone,body:'synthetic new',received_at:'2026-10-05T12:01:01Z'}]);
 await f.connector.poll();assert.equal(f.connector.inbound().messages.length,1);assert.equal(f.sends(),0);
 f.setMessages(Array.from({length:101},(_,i)=>({id:'excess'+i,phone,body:'synthetic',received_at:'2026-10-05T12:01:01Z'})));
 assert.equal((await f.connector.poll()).reason_code,'demo_intake_limit_exceeded');
 assert.equal(f.connector.inbound().messages.length,1);
});

test('one exact scoped send requires active session and uncertainty survives restart without retry',async t=>{
 const f=await fixture(t);await f.connector.poll();
 const input={idempotency_key:`GV${id}:single-message`,to:phone,body:'Synthetic reviewed text.',not_after:'2026-10-05T12:01:20Z'};
 assert.equal((await f.connector.prepare({...input,idempotency_key:'wrong-session'})).reason_code,'demo_session_not_authorized');
 assert.equal((await f.connector.prepare(input)).status,'prepared');
 assert.equal((await f.connector.send(input)).status,'uncertain');
 assert.equal((await f.connector.send(input)).status,'uncertain');assert.equal(f.sends(),1);
 const store=new Store(f.directory);await store.load();
 const restarted=new Connector({store,browser:f.browser,expectedEmail:email,expectedPhone:number,allowedPhones:[phone],demoMode:true,testSessions:sessions,now:()=>start});
 assert.equal((await restarted.send(input)).status,'uncertain');assert.equal(f.sends(),1);
 f.setNow(end);await f.connector.poll();
 assert.equal((await f.connector.prepare({...input,idempotency_key:`GV${id}:expired`,not_after:'2026-10-05T13:00:20Z'})).reason_code,'demo_session_not_authorized');
});


test('final sender identity change rejects before the click and preserves prepared composer',async()=>{
 let closes=0, clicks=0;
 const preparedPage={url:()=>`https://voice.google.com/u/0/messages?itemId=t.${phone}`};
 const verification={setDefaultTimeout(){},goto:async()=>{},locator:()=>({waitFor:async()=>{}}),
  url:()=> 'https://voice.google.com/u/0/settings',close:async()=>{closes++;}};
 const browser=new VoiceBrowser({directory:'/unused-synthetic',allowedPhones:[phone],demoMode:true});
 browser.context={newPage:async()=>verification};browser.page=preparedPage;
 browser.prepared={to:phone,body:'Synthetic reviewed text.',before:0};
 browser.readIdentity=async()=>({email:'different@example.test',phone:number});
 preparedPage.locator=()=>({click:async()=>{clicks++;}});
 const outcome=await browser.submitSend(phone,'Synthetic reviewed text.',end,{email,phone:number});
 assert.equal(outcome.status,'rejected');assert.equal(outcome.reason_code,'final_identity_not_verified');
 assert.equal(clicks,0);assert.equal(closes,1);assert.equal(browser.page,preparedPage);
});


test('empty waiting scope can register a new exact recipient durably without scan or send',async t=>{
 const f=await fixture(t);
 f.connector.testSessions={};f.connector.allowedPhones=new Set();
 f.browser.allowedPhones=[];
 const emptyHash=f.connector.health().scope_fingerprint;
 const registered=await f.connector.registerRecipient({phone,id,starts_at:start,expires_at:end,expected_scope:emptyHash});
 assert.equal(registered.registered,true);assert.equal(f.sends(),0);
 const stableHash=registered.scope_fingerprint;
 const repeat=await f.connector.registerRecipient({phone,id,starts_at:start,expires_at:end,expected_scope:emptyHash});
 assert.equal(repeat.scope_fingerprint,stableHash);
 await assert.rejects(()=>f.connector.registerRecipient({phone:'+12025550103',id:'b'.repeat(32),starts_at:start,expires_at:end,expected_scope:emptyHash}),{code:'demo_scope_mismatch'});
 const store=new Store(f.directory);await store.load();
 const restarted=new Connector({store,browser:f.browser,expectedEmail:email,expectedPhone:number,allowedPhones:[],demoMode:true,testSessions:{},now:()=> '2026-10-05T12:01:00Z'});
 assert.equal(restarted.health().scope_fingerprint,stableHash);assert.equal(restarted.health().ready,false);
 assert.deepEqual(f.browser.allowedPhones,[phone]);
 f.setMessages([{id:'pre-consent',phone,body:'history',received_at:'2026-10-05T11:59:59Z'},
  {id:'post-registration',phone,body:'new name',received_at:'2026-10-05T12:00:01Z'}]);
 await restarted.poll();
 assert.deepEqual(restarted.inbound().messages.map(row=>row.id),[await import('../core.mjs').then(m=>m.hash('post-registration'))]);
 assert.equal(f.sends(),0);
 assert.equal(readConfig({...env,GOOGLE_VOICE_ALLOWED_PHONES:'',GOOGLE_VOICE_TEST_SESSIONS:'{}'}).demoMode,false);
});

test('renewal and restart retain late withdrawals from only the registered thread',async t=>{
 const f=await fixture(t);
 await f.connector.registerRecipient({phone,id,starts_at:start,expires_at:end,expected_scope:f.connector.health().scope_fingerprint});
 await f.connector.poll();
 f.setNow('2026-10-05T15:01:00Z');
 f.setMessages([{id:'late-stop',phone,body:'STOP',received_at:'2026-10-05T14:00:00Z'}]);
 let scanned;
 f.browser.scan=async phones=>{scanned=phones;return [{id:'late-stop',phone,body:'STOP',received_at:'2026-10-05T14:00:00Z'}];};
 await f.connector.poll({phone});
 assert.deepEqual(scanned,[phone]);assert.equal(f.connector.inbound().messages[0].body,'STOP');
 await f.connector.registerRecipient({phone,id:'b'.repeat(32),starts_at:'2026-10-05T15:01:00Z',expires_at:'2026-10-05T16:01:00Z',expected_scope:f.connector.health().scope_fingerprint});
 const store=new Store(f.directory);await store.load();
 const restarted=new Connector({store,browser:f.browser,expectedEmail:email,expectedPhone:number,allowedPhones:[],demoMode:true,testSessions:{},now:()=> '2026-10-05T15:02:00Z'});
 assert.equal(store.data.demo_activation[phone],start);
 await restarted.poll({phone});assert.equal(restarted.inbound().messages.length,1);
 assert.equal(f.sends(),0);
 await assert.rejects(()=>restarted.poll({phone:'+12025550103'}),{code:'recipient_not_allowed'});
});

test('explicit historical profile verification remains held after authentication',async t=>{
 const directory=await mkdtemp(join(tmpdir(),'voice-profile-'));
 t.after(()=>rm(directory,{recursive:true,force:true}));
 let identity={email,phone:number}, identities=0;
 t.mock.method(VoiceBrowser.prototype,'start',async()=>{});
 t.mock.method(VoiceBrowser.prototype,'close',async()=>{});
 t.mock.method(VoiceBrowser.prototype,'identity',async()=>{identities++;return identity;});
 for(const method of ['importSession','clearSession','scan','submitSend']) t.mock.method(VoiceBrowser.prototype,method,async()=>{throw Error('No account mutation, intake or submission during verification');});
 const runtime=await startServer({...readConfig({...env,VOICE_DATA_DIR:directory}),port:0});
 t.after(()=>runtime.close());
 const url=`http://127.0.0.1:${runtime.server.address().port}/demo/verify-profile`;
 const headers={Authorization:`Bearer ${env.VOICE_API_TOKEN}`,'Content-Type':'application/json'};
 assert.equal(identities,0);
 assert.equal((await fetch(url,{method:'POST',body:'{}'})).status,401);
 assert.equal((await fetch(url,{method:'POST',headers,body:'{"cookies":[]}'})).status,503);
 assert.equal(identities,0);
 const verified=await (await fetch(url,{method:'POST',headers,body:'{}'})).json();
 assert.deepEqual(verified,{error:'provider_policy_hold'});
 assert.equal(identities,0);
 identity={email:'foreign@example.test',phone:number};
 assert.equal((await fetch(url,{method:'POST',headers,body:'{}'})).status,503);
 const failed=await (await fetch(url.replace('/demo/verify-profile','/health'),{headers})).json();
 assert.equal(failed.identity_verified,false);assert.equal(failed.ready,false);
});

test('durable manual-login marker blocks profile startup and existing-profile identity access',async t=>{
 const directory=await mkdtemp(join(tmpdir(),'voice-login-marker-'));
 t.after(()=>rm(directory,{recursive:true,force:true}));
 await writeFile(join(directory,'manual-login.active'),'synthetic login active');
 const browser=new VoiceBrowser({directory,allowedPhones:[],demoMode:true});
 await assert.rejects(()=>browser.start(),{code:'manual_login_active'});
 await assert.rejects(()=>browser.identity(),{code:'manual_login_active'});
 await rm(join(directory,'manual-login.active'));
 await browser.assertProfileAvailable();
});

test('observed non-heading account-number component excludes hidden digits and linked numbers',async()=>{
 const visible=value=>({getClientRects:()=>[{}],...value});
 let sections=[visible({querySelectorAll:selector=>{
   assert.equal(selector,'.phone-number [aria-hidden="true"]');
   return [visible({textContent:'\u202a(202) 555-0101\u202c'})];
 }})];
 const account=visible({getAttribute:()=>`Google Account: Synthetic Demo (${email})`});
 // The observed DOM also contains cdk-visually-hidden duplicate digits and
 // linked-number components. Neither is part of these scoped selectors.
 const document={querySelectorAll:selector=>{
   if(selector==='[aria-label]') return [account];
   if(selector==='gv-account-number') return sections;
   throw Error('Identity must not scan headings or unrelated linked numbers');
 }};
 const saved=globalThis.document;globalThis.document=document;
 try{
   const browser=new VoiceBrowser({directory:'/unused-synthetic',allowedPhones:[],demoMode:true});
   const page={evaluate:async callback=>callback()};
   assert.deepEqual(await browser.readIdentity(page),{email,phone:number});
   sections=[...sections,visible({querySelectorAll:()=>[visible({textContent:'(202) 555-0103'})]})];
   await assert.rejects(()=>browser.readIdentity(page),{code:'identity_not_observable'});
   sections=[];await assert.rejects(()=>browser.readIdentity(page),{code:'identity_not_observable'});
 }finally{if(saved===undefined)delete globalThis.document;else globalThis.document=saved;}
});

function draftBrowser(){
 let route='https://voice.google.com/u/0/messages?itemId=draft',chipText='\u202a(202) 555-0102\u202c',chipCount=1,body='',clicks=0,checks=0,lateChange=false,committed=false;
 const visibleLabel={count:async()=>1,isVisible:async()=>true,textContent:async()=>chipText};
 const chips={count:async()=>committed?chipCount:0,locator:selector=>{assert.equal(selector,'.chip-name[aria-hidden="true"]');return visibleLabel;}};
 const region={count:async()=>1,isVisible:async()=>true,locator:selector=>{assert.equal(selector,'mat-chip-row');return chips;}};
 const choice={count:async()=>1,waitFor:async()=>{},click:async()=>{committed=true;},innerText:async()=>{throw Error('Do not concatenate hidden spoken digits with formatted number');},
  locator:selector=>{assert.equal(selector,selectors.recipientChoiceLabel);return {count:async()=>1,isVisible:async()=>true,textContent:async()=> 'Send to (202) 555-0102'};}};
 const composer={count:async()=>1,fill:async value=>{body=value;},inputValue:async()=>body};
 const send={count:async()=>1,isEnabled:async()=>{checks++;if(lateChange&&checks>1)chipText='(202) 555-0103';return true;},click:async()=>{clicks++;body='';}};
 const browser=new VoiceBrowser({directory:'/unused-synthetic',allowedPhones:[phone],demoMode:true});
 browser.navigate=async()=>{committed=false;};browser.verifyPreparedIdentity=async()=>{};
 browser.waitForRecipientProof=async(proof,code)=>{if(!await proof())throw new Hold(code);};
 browser.page={url:()=>route,keyboard:{press:async()=>{}},waitForFunction:async()=>{},locator:selector=>{
  if(selector===selectors.newMessage)return {click:async()=>{}};
  if(selector===selectors.recipient)return {fill:async value=>assert.equal(value,''),pressSequentially:async(value,options)=>{assert.equal(value,phone);assert.deepEqual(options,{delay:40});}};
  if(selector===selectors.recipientChoice)return choice;
  if(selector===selectors.recipientRegion)return region;
  if(selector===selectors.compose)return composer;
  if(selector===selectors.send)return send;
  throw Error('Unknown scoped selector');
 }};
 browser.rows=async()=>clicks?[{incoming:false,directionKnown:true,text:'Exact synthetic reviewed body.'}]:[];
 return {browser,setChip:value=>{chipText=value;},setCount:value=>{chipCount=value;},setRoute:value=>{route=value;},lateChange:()=>{lateChange=true;},clicks:()=>clicks};
}

test('observed first-message draft uses one numeric chip and visible send-to label for exact recipient',async()=>{
 const f=draftBrowser();
 await f.browser.prepareSend(phone,'Exact synthetic reviewed body.');
 const outcome=await f.browser.submitSend(phone,'Exact synthetic reviewed body.',new Date(Date.now()+30000).toISOString(),{email,phone:number});
 assert.equal(outcome.status,'submitted');assert.equal(f.clicks(),1);
});

test('draft recipient changes, multiple chips and ambiguous labels reject immediately before click',async()=>{
 for(const mutate of [f=>f.setChip('(202) 555-0103'),f=>f.setCount(2),f=>f.setCount(0),
   f=>f.setChip('Unverified contact name'),f=>f.setChip('Contact (202) 555-0102'),
   f=>f.setRoute('https://voice.google.com/u/0/messages?itemId=unknown'),f=>f.lateChange()]){
  const f=draftBrowser();await f.browser.prepareSend(phone,'Exact synthetic reviewed body.');mutate(f);
  const outcome=await f.browser.submitSend(phone,'Exact synthetic reviewed body.',new Date(Date.now()+30000).toISOString(),{email,phone:number});
  assert.equal(outcome.status,'rejected');assert.equal(outcome.reason_code,'recipient_not_verified');assert.equal(f.clicks(),0);
 }
 const f=draftBrowser();f.setCount(2);
 await assert.rejects(()=>f.browser.prepareSend(phone,'Exact synthetic reviewed body.'),{code:'recipient_selected_not_verified'});
 assert.equal(f.clicks(),0);
});
