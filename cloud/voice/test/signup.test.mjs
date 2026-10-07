import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {EventEmitter} from 'node:events';
import {Store,Connector,scopeHash} from '../core.mjs';
import {readConfig,installShutdown,startServer} from '../server.mjs';
import {VoiceBrowser} from '../browser.mjs';

const phone='+12025550102',email='fixture@example.test',number='+12025550101';
const now='2026-10-05T12:00:00.000Z';
const enrollment={phone,id:'1'.repeat(32),starts_at:now,expires_at:'9999-12-31T23:59:59.999000+00:00',continuous:true,expected_scope:scopeHash({})};

async function fixture(t,signupEnabled=true){
 const directory=await mkdtemp(join(tmpdir(),'voice-signup-'));
 t.after(()=>rm(directory,{recursive:true,force:true}));
 const store=new Store(directory);await store.load();
 const scans=[];let submits=0;
 const browser={identity:async()=>({email,phone:number}),scan:async(phones,options)=>{scans.push({phones,options});return [];},prepareSend:async()=>{},submitSend:async()=>{submits++;return {status:'uncertain',reason_code:'synthetic_unknown'};}};
 const config={store,browser,expectedEmail:email,expectedPhone:number,allowedPhones:[],demoMode:true,signupEnabled,now:()=>now};
 return {store,browser,config,connector:new Connector(config),scans,submits:()=>submits};
}

test('continuous enrollment requires explicit mode and exact indefinite scope, then survives restart',async t=>{
 const off=await fixture(t,false);
 await assert.rejects(()=>off.connector.registerRecipient(enrollment),{code:'invalid_demo_registration'});
 const f=await fixture(t);
 await assert.rejects(()=>f.connector.registerRecipient({...enrollment,expires_at:'9998-01-01T00:00:00Z'}),{code:'invalid_demo_registration'});
 const registered=await f.connector.registerRecipient(enrollment);
 assert.equal(registered.scope_fingerprint,scopeHash({[phone]:enrollment}));
 const reloaded=new Store(f.store.directory);await reloaded.load();
 const restarted=new Connector({...f.config,store:reloaded});
 assert.equal(restarted.testSessions[phone].continuous,true);
 assert.equal(restarted.sessionPermits({to:phone,idempotency_key:`GV${enrollment.id}:key`,not_after:'2027-10-05T12:00:00Z'}),true);
 await assert.rejects(()=>restarted.poll({phones:['+12025550103']}),{code:'recipient_not_allowed'});
 assert.equal(f.scans.length,0);
});

test('first no-history permission is durably removed before uncertain submission and stays removed after restart',async t=>{
 const f=await fixture(t);await f.connector.registerRecipient(enrollment);
 await f.connector.poll({phones:[phone]});
 assert.deepEqual(f.scans[0],{phones:[phone],options:{emptyPhones:[phone]}});
 const request={to:phone,body:'Exact synthetic initial invitation.',idempotency_key:`GV${enrollment.id}:synthetic-key`,not_after:'2026-10-05T12:00:30Z'};
 assert.equal((await f.connector.prepare(request)).status,'prepared');
 assert.equal((await f.connector.send(request)).status,'uncertain');
 const reloaded=new Store(f.store.directory);await reloaded.load();
 assert.equal(reloaded.data.demo_started[phone],true);
 const restarted=new Connector({...f.config,store:reloaded});
 assert.equal((await restarted.send(request)).status,'uncertain');
 assert.equal(f.submits(),1);
 await restarted.poll({phones:[phone]});
 assert.deepEqual(f.scans.at(-1),{phones:[phone],options:{emptyPhones:[]}});
});

test('signup flag is explicit and cannot release ordinary disabled startup',()=>{
 const env={VOICE_API_TOKEN:'synthetic-private-token'.padEnd(32,'0')};
 assert.equal(readConfig(env).signupEnabled,false);
 assert.equal(readConfig({...env,GOOGLE_VOICE_SIGNUP_ENABLED:'true'}).demoMode,false);
 assert.equal(readConfig({...env,GOOGLE_VOICE_SIGNUP_ENABLED:'true'}).signupEnabled,false);
 assert.throws(()=>readConfig({...env,GOOGLE_VOICE_SIGNUP_ENABLED:'yes'}),{code:'invalid_configuration'});
});

test('both shutdown signals wait for one ordered profile close before exiting',async()=>{
 const signals=new EventEmitter();let close=0,exits=0,release;
 const closed=new Promise(resolve=>{release=resolve;});
 installShutdown({close:async()=>{close++;await closed;}},signals,code=>{assert.equal(code,0);exits++;});
 signals.emit('SIGTERM');signals.emit('SIGINT');
 assert.equal(close,1);assert.equal(exits,0);
 release();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(exits,1);
});

test('held runtime drains a blocked synthetic request once without opening or closing a browser',async t=>{
 const directory=await mkdtemp(join(tmpdir(),'voice-close-'));t.after(()=>rm(directory,{recursive:true,force:true}));
 t.mock.method(VoiceBrowser.prototype,'start',async()=>{});
 let browserCloses=0,release;
 const blocked=new Promise(resolve=>{release=resolve;});
 t.mock.method(VoiceBrowser.prototype,'close',async()=>{browserCloses++;release();});
 const runtime=await startServer({directory,demoMode:true,allowedPhones:[],expectedEmail:email,expectedPhone:number,token:'synthetic-token'.padEnd(32,'0'),port:0});
 runtime.server.removeAllListeners('request');
 let started;const ready=new Promise(resolve=>{started=resolve;});
 runtime.server.on('request',async(_request,response)=>{started();await blocked;response.end('{}');});
 const request=fetch(`http://127.0.0.1:${runtime.server.address().port}`).catch(()=>null);
 await ready;
 const first=runtime.close(),second=runtime.close();
 assert.equal(first,second);
 await first;await request;
 assert.equal(browserCloses,0);
 release();
});


test('persisted enrollment re-verifies a restarted unverified profile before scanning and holds actual sign-out',async t=>{
 const f=await fixture(t);await f.connector.registerRecipient(enrollment);
 const reloaded=new Store(f.store.directory);await reloaded.load();
 let identities=0;
 f.browser.identity=async()=>{identities++;return {email,phone:number};};
 const restarted=new Connector({...f.config,store:reloaded});
 assert.equal(restarted.health().reason_code,'session_not_verified');assert.equal(restarted.health().ready,false);
 const health=await restarted.poll({phones:[phone]});
 assert.equal(identities,1);assert.equal(health.ready,true);assert.equal(f.scans.length,1);
 f.browser.identity=async()=>{throw Object.assign(new Error('synthetic sign-out'),{code:'login_required'});};
 const signedOut=new Connector({...f.config,store:reloaded});
 assert.equal((await signedOut.poll({phones:[phone]})).ready,false);
 assert.equal(f.scans.length,1);
});
