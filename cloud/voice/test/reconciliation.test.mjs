import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Connector,Store,Hold,hash} from '../core.mjs';
import {apiServer} from '../server.mjs';

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

test('stored signup lookup returns only one original durable input without browser or cursor changes',async t=>{
 const f=await fixture(t);f.connector.signupEnabled=true;f.connector.testSessions[phone].continuous=true;
 const item={id:'original-inbound',phone,body:'Synthetic Name',received_at:'2026-10-06T02:02:00Z',cursor:7};
 f.store.data.inbound=[{...item,id:'unrelated-item',phone:'+12025550103'},item];
 const before=JSON.stringify(f.store.data);
 const result=f.connector.storedSignupInput({id:item.id,phone,session_id:sessionId});
 const {cursor:_,...original}=item;assert.deepEqual(result,{message:original,session_id:sessionId});
 result.message.body='Caller mutation';assert.equal(JSON.stringify(f.store.data),before);
 assert.equal(f.reads(),0);
});
for(const change of ['disabled','foreign phone','wrong session','not continuous','missing item','duplicate item','old time'])
 test('stored signup lookup rejects '+change,async t=>{
 const f=await fixture(t);f.connector.signupEnabled=true;f.connector.testSessions[phone].continuous=true;
 const item={id:'original-inbound',phone,body:'Synthetic Name',received_at:'2026-10-06T02:02:00Z',cursor:7};
 f.store.data.inbound=[item];const request={id:item.id,phone,session_id:sessionId};
 if(change==='disabled')f.connector.signupEnabled=false;
 if(change==='foreign phone')request.phone='+12025550103';
 if(change==='wrong session')request.session_id='b'.repeat(32);
 if(change==='not continuous')f.connector.testSessions[phone].continuous=false;
 if(change==='missing item')request.id='unknown';
 if(change==='duplicate item')f.store.data.inbound.push({...item});
 if(change==='old time')item.received_at='2026-10-06T01:56:00Z';
 assert.throws(()=>f.connector.storedSignupInput(request),Hold);assert.equal(f.reads(),0);
});
test('stored input API authenticates and returns only the exact scoped durable item',async t=>{
 const f=await fixture(t);f.connector.signupEnabled=true;f.connector.testSessions[phone].continuous=true;
 f.store.data.inbound=[{id:'original-inbound',phone,body:'Synthetic Name',received_at:'2026-10-06T02:02:00Z',cursor:7}];
 const token='synthetic-token'.padEnd(32,'0'),server=apiServer(f.connector,token);
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(()=>new Promise(resolve=>server.close(resolve)));
 const url=`http://127.0.0.1:${server.address().port}/demo/signup-input`,body=JSON.stringify({id:'original-inbound',phone,session_id:sessionId});
 assert.equal((await fetch(url,{method:'POST',body})).status,401);
 const headers={Authorization:`Bearer ${token}`,'Content-Type':'application/json'};
 const response=await fetch(url,{method:'POST',headers,body});assert.equal(response.status,200);
 assert.equal((await response.json()).message.id,'original-inbound');assert.equal(f.reads(),0);
});
