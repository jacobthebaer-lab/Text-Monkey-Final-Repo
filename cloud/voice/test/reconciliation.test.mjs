import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Connector,Store,Hold,hash} from '../core.mjs';

const phone='+12025550102',sender='+12025550101',email='synthetic@example.test',sessionId='a'.repeat(32);
const claim='2026-10-06T01:58:15+00:00',reserved='2026-10-06T01:58:17.000Z',body='Hello 🐵. Reply 🙈.';
const input={idempotency_key:`GV${sessionId}:synthetic-original-key`,to:phone,body,session_id:sessionId,claim_created_at:claim};
async function fixture(t){
 const directory=await mkdtemp(join(tmpdir(),'voice-observe-'));t.after(()=>rm(directory,{recursive:true,force:true}));
 const store=new Store(directory);await store.load();
 store.data.baseline_at='2026-10-06T01:57:00Z';store.data.sends[hash(input.idempotency_key)]={digest:hash('immutable original deadline preimage'),created_at:reserved,status:'uncertain',reason_code:'submission_unconfirmed'};
 await store.save();let reads=0;
 const browser={identity:async()=>({email,phone:sender}),observeSubmission:async()=>{reads++;return {body_hash:hash(body),provider_item_fingerprint:hash('synthetic exact item'),native_timestamp_interval:{start:'2026-10-06T01:58:00.000Z',end:'2026-10-06T01:59:00.000Z',precision:'minute'}};},
  scan:async()=>assert.fail('Reconciliation never scans intake'),prepareSend:async()=>assert.fail('Never prepare'),submitSend:async()=>assert.fail('Never submit')};
 const connector=new Connector({store,browser,expectedEmail:email,expectedPhone:sender,allowedPhones:[phone],demoMode:true,
  testSessions:{[phone]:{id:sessionId,starts_at:'2026-10-06T01:57:00Z',expires_at:'2026-10-06T03:57:00Z'}},now:()=> '2026-10-06T02:20:00Z'});
 return {store,browser,connector,reads:()=>reads};
}
test('same original opaque digest reconciles durably and idempotently without send or baseline change',async t=>{
 const f=await fixture(t),key=hash(input.idempotency_key),digest=f.store.data.sends[key].digest;
 const result=await f.connector.reconcile(input);assert.equal(result.status,'submitted');
 assert.equal(result.proof.original_digest,digest);assert.equal(result.proof.digest_verification,'opaque_original_preserved');
 assert.equal(f.store.data.sends[key].digest,digest);assert.equal(f.store.data.baseline_at,'2026-10-06T01:57:00Z');
 assert.equal(f.store.data.next_cursor,1);assert.equal(f.store.data.inbound.length,0);
 assert.deepEqual(await f.connector.reconcile(input),result);assert.equal(f.reads(),1);
 const restarted=new Store(f.store.directory);await restarted.load();assert.deepEqual(restarted.data.sends[key],f.store.data.sends[key]);
 await assert.rejects(f.connector.reconcile({...input,body:body+' changed'}),{code:'reconciliation_proof_conflict'});
});
for(const mode of ['foreign recipient','different session','fresh key','mismatched observed body','account mismatch','unconfirmed item'])test('reconciliation holds '+mode,async t=>{
 const f=await fixture(t);let request={...input};
 if(mode==='foreign recipient')request.to='+12025550103';
 if(mode==='different session')request.session_id='b'.repeat(32);
 if(mode==='fresh key')request.idempotency_key+= '-fresh';
 if(mode==='mismatched observed body')f.browser.observeSubmission=async()=>({body_hash:hash('different')});
 if(mode==='account mismatch')f.browser.identity=async()=>({email:'other@example.test',phone:sender});
 if(mode==='unconfirmed item')f.browser.observeSubmission=async()=>{throw new Hold('reconciliation_message_not_verified');};
 await assert.rejects(f.connector.reconcile(request));
 assert.equal(f.store.data.sends[hash(input.idempotency_key)].status,'uncertain');
 assert.equal(Object.keys(f.store.data.sends).length,1);
});
test('minute-precision reply overlapping activation holds without marking seen or resetting baseline',async t=>{
 const f=await fixture(t),start='2026-10-06T01:58:15Z';
 f.connector.testSessions[phone].starts_at=start;f.store.data.demo_activation={[phone]:start};
 f.browser.scan=async()=>[{id:'synthetic-overlap',phone,body:'Synthetic Name',received_at:'2026-10-06T01:58:00Z',
  received_at_interval:{start:'2026-10-06T01:58:00Z',end:'2026-10-06T01:59:00Z',precision:'minute'}}];
 const report=await f.connector.poll();assert.equal(report.reason_code,'message_timestamp_ambiguous');
 assert.equal(f.store.data.seen[hash('synthetic-overlap')],undefined);
 assert.equal(f.store.data.inbound.length,0);assert.equal(f.store.data.baseline_at,'2026-10-06T01:57:00Z');
});
