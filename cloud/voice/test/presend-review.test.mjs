import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Connector,Store,hash} from '../core.mjs';

const phone='+12025550102',number='+12025550101',email='synthetic@example.test',id='a'.repeat(32);
const now='2026-10-06T04:40:00Z';
const sessions={[phone]:{id,starts_at:'2026-10-06T04:00:00Z',expires_at:'2026-10-06T05:00:00Z'}};
const input={idempotency_key:`GV${id}:rejected-before-send`,to:phone,session_id:id,
 body_hash:hash('Synthetic exact reviewed text.'),reason_code:'recipient_choice_wait_unavailable'};
async function fixture(t){
 const directory=await mkdtemp(join(tmpdir(),'presend-observation-'));t.after(()=>rm(directory,{recursive:true,force:true}));
 const store=new Store(directory);await store.load();let native=0;
 const browser={identity:async()=>({email,phone:number}),prepareSend:async()=>{native++;},submitSend:async()=>{native++;}};
 const config={store,browser,expectedEmail:email,expectedPhone:number,allowedPhones:[phone],demoMode:true,testSessions:sessions,now:()=>now};
 return {store,browser,config,connector:new Connector(config),native:()=>native,directory};
}
test('serialized absence proof permanently disables only original key, survives restart and never touches browser sender',async t=>{
 const f=await fixture(t);const before=JSON.stringify(f.store.data.sends);
 const result=await f.connector.presendAbsence(input);
 assert.equal(result.status,'unsubmitted');assert.equal(result.proof.original_key_disabled,true);
 assert.equal(result.proof.submission_key_hash,hash(input.idempotency_key));assert.equal(f.native(),0);
 assert.equal(JSON.stringify(f.store.data.sends),before);
 assert.deepEqual(await f.connector.presendAbsence(input),result);
 const request={idempotency_key:input.idempotency_key,to:phone,body:'Synthetic exact reviewed text.',not_after:'2026-10-06T04:40:20Z'};
 assert.equal((await f.connector.prepare(request)).reason_code,'original_key_disabled_after_review_recovery');
 assert.equal((await f.connector.send(request)).reason_code,'original_key_disabled_after_review_recovery');
 const store=new Store(f.directory);await store.load();const restarted=new Connector({...f.config,store});
 assert.equal((await restarted.send(request)).reason_code,'original_key_disabled_after_review_recovery');
 assert.equal(f.native(),0);assert.deepEqual(store.data.sends,{});
});
for(const status of ['pending','uncertain','submitted','rejected'])test('existing '+status+' original ledger record prevents absence proof',async t=>{
 const f=await fixture(t);f.store.data.sends[hash(input.idempotency_key)]={status,digest:'original'};
 await assert.rejects(f.connector.presendAbsence(input),{code:'submission_record_exists'});
 assert.equal(f.store.data.presend_recoveries,undefined);assert.equal(f.native(),0);
});
test('wrong recipient/session/reason, pending preparation, unknown other key and sender mismatch hold',async t=>{
 const f=await fixture(t);
 for(const extra of [{to:'+12025550103'},{session_id:'b'.repeat(32)},{reason_code:'browser_unavailable'},
  {idempotency_key:'GVwrong:original'},{body_hash:'not-a-hash'}]){
  await assert.rejects(f.connector.presendAbsence({...input,...extra}),{code:'invalid_presend_observation'});
 }
 f.connector.preparation={key:'pending',not_after:'2026-10-06T04:40:20Z'};
 await assert.rejects(f.connector.presendAbsence(input),{code:'preparation_in_progress'});f.connector.preparation=null;
 f.store.data.sends.another={status:'uncertain'};
 await assert.rejects(f.connector.presendAbsence(input),{code:'submission_record_exists'});delete f.store.data.sends.another;
 f.browser.identity=async()=>({email:'wrong@example.test',phone:number});
 await assert.rejects(f.connector.presendAbsence(input));assert.equal(f.store.data.presend_recoveries,undefined);
 assert.equal(f.native(),0);
});
test('failed durable save cannot issue proof or enable original-key replay',async t=>{
 const f=await fixture(t);f.store.save=async()=>{throw Error('synthetic storage outage');};
 await assert.rejects(f.connector.presendAbsence(input),{code:'state_unavailable'});
 const request={idempotency_key:input.idempotency_key,to:phone,body:'Synthetic exact reviewed text.',not_after:'2026-10-06T04:40:20Z'};
 assert.equal((await f.connector.send(request)).reason_code,'original_key_disabled_after_review_recovery');assert.equal(f.native(),0);
});
