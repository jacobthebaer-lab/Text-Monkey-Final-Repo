import test from 'node:test';
import assert from 'node:assert/strict';
import {eventInstant,createAcceptanceWorkflow} from '../public/acceptance-workflow.js';

test('church zone conversion rejects skipped and repeated clock hours',()=>{
  assert.equal(eventInstant('2026-10-02T10:00','America/Denver'),'2026-10-02T16:00:00.000Z');
  assert.throws(()=>eventInstant('2026-03-08T02:30','America/Denver'),/invalid or repeats/);
  assert.throws(()=>eventInstant('2026-11-01T01:30','America/Denver'),/invalid or repeats/);
});

test('separate controls preserve exact review and bound reminder IDs/hash, never auto-approve',async()=>{
  const calls=[];
  let value={participant:'Synthetic Tester',zone:'America/Denver',roles:[],event:{id:7,title:'Demo: <fictional>',starts_at:'2026-10-02T16:00:00Z'},assignment_id:9,
    reviews:[{id:11,status:'pending',kind:'confirm_text',content_hash:'a'.repeat(64),body:'Exact <Gloo> copy.'}],message:{id:13,status:'queued',body:'Exact <Gloo> copy.',body_hash:'b'.repeat(64)},timer_available:true,timer:{enabled:false}};
  const ui=createAcceptanceWorkflow({api:async(path,options)=>{calls.push({path,options});return structuredClone(value);},getMode:()=> 'live',getToken:()=> 'synthetic',getConfig:()=>({acceptanceEventAvailable:true}),render(){}});
  await ui.load();
  assert.match(ui.panel(),/Exact &lt;Gloo&gt; copy/);
  assert.equal(calls.length,1);
  await ui.action({dataset:{acceptanceAction:'prepare'}});
  assert.deepEqual(calls.at(-1).options,{event_id:7,assignment_id:9});
  assert.ok(!calls.some(c=>c.path.endsWith('/approve')));
  await ui.action({dataset:{acceptanceAction:'approve',reviewId:'11',contentHash:'a'.repeat(64)}});
  assert.deepEqual(calls.at(-1).options,{review_id:11,content_hash:'a'.repeat(64)});
  await ui.action({dataset:{acceptanceAction:'dispatch'}});
  assert.deepEqual(calls.at(-1).options,{event_id:7,assignment_id:9,message_id:13,body_hash:'b'.repeat(64)});
  await ui.action({dataset:{acceptanceAction:'stop-timer'}});
  assert.deepEqual(calls.at(-1).options,{enabled:false});
});

test('no scope capability hides the controls and performs no API calls',async()=>{
  const ui=createAcceptanceWorkflow({api:()=>assert.fail('No request authorized'),getMode:()=> 'live',getToken:()=> 'synthetic',getConfig:()=>({}),render(){}});
  await ui.load();assert.equal(ui.panel(),'');await ui.action({dataset:{acceptanceAction:'dispatch'}});
});
