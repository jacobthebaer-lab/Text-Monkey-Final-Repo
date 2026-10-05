import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {readConfig, startServer} from '../server.mjs';
import {VoiceBrowser} from '../browser.mjs';
import {Connector, Store} from '../core.mjs';

const email='demo@example.test', number='+12025550101', phone='+12025550102';
const start='2026-10-05T12:00:00Z', end='2026-10-05T13:00:00Z', id='a'.repeat(32);
const sessions={[phone]:{id,starts_at:start,expires_at:end}};
const env={VOICE_ENABLED:'true',VOICE_API_TOKEN:'synthetic-token'.padEnd(32,'0'),GOOGLE_VOICE_DEMO_MODE:'true',
 VOICE_EXPECTED_EMAIL:email,VOICE_EXPECTED_NUMBER:number,GOOGLE_VOICE_ALLOWED_PHONES:phone,
 GOOGLE_VOICE_TEST_SESSIONS:JSON.stringify(sessions)};

test('dedicated demo requires isolated sender and bounded distinct test sessions',()=>{
 for(const extra of [{VOICE_ENABLED:'false'},{GOOGLE_VOICE_TEST_SESSIONS:'{}'},{VOICE_EXPECTED_NUMBER:phone},
   {GOOGLE_VOICE_ALLOWED_PHONES:''},{VOICE_EXPECTED_EMAIL:''},
   {GOOGLE_VOICE_TEST_SESSIONS:JSON.stringify({[phone]:{id,starts_at:start,expires_at:'2026-10-05T15:00:00Z'}})}]) {
  assert.throws(()=>readConfig({...env,...extra}));
 }
 assert.equal(readConfig(env).demoMode,true);
 assert.equal(readConfig({VOICE_API_TOKEN:env.VOICE_API_TOKEN}).demoMode,false);
});

test('real startup adapter waits for explicit intake; session import and refresh never scan',async t=>{
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
 assert.equal(health.demo_mode,true); assert.equal(health.ready,false);
 const response=await fetch(base+'/session',{method:'POST',headers,body:JSON.stringify({cookies:[{name:'SID',value:'synthetic',domain:'.google.com',path:'/'}]})});
 assert.equal(response.status,200);assert.equal(scans,0);assert.equal(identities,1);
 for(let i=0;i<2;i++) await fetch(base+'/health',{headers});
 assert.equal(scans,0);
 health=await (await fetch(base+'/demo/intake',{method:'POST',headers,body:'{}'})).json();
 assert.equal(scans,1);assert.equal(health.ready,true);assert.ok(health.baseline_at);
 assert.equal(health.delivery_verified,false);
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
 assert.equal(readConfig({...env,GOOGLE_VOICE_ALLOWED_PHONES:'',GOOGLE_VOICE_TEST_SESSIONS:'{}'}).demoMode,true);
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
