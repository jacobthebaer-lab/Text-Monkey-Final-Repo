import test from 'node:test';
import assert from 'node:assert/strict';
import {createAcceptanceWorkflow} from '../public/acceptance-workflow.js';
const snapshot=participant=>({participant,zone:'America/Denver',roles:[],reviews:[],timer:{state:'off'}});
test('late walkthrough failures cannot expose old-account details or unlock a new-account action',async()=>{
  let token='account-a',rejectOld,finishNew,writes=0;
  const flow=createAcceptanceWorkflow({api:(path,body)=>!body?Promise.resolve(snapshot(token)):(writes++,token==='account-a'?new Promise((_,reject)=>{rejectOld=reject;}):new Promise(resolve=>{finishNew=resolve;})),
    getMode:()=> 'live',getToken:()=>token,getConfig:()=>({acceptanceEventAvailable:true}),render(){}});
  await flow.load();const old=flow.action({dataset:{acceptanceAction:'stop-timer'}});
  token='account-b';flow.reset();await flow.load();
  const current=flow.action({dataset:{acceptanceAction:'stop-timer'}});
  rejectOld(Error('Old account private failure'));await old;
  assert.doesNotMatch(flow.panel(),/Old account private failure/);
  await flow.action({dataset:{acceptanceAction:'stop-timer'}});assert.equal(writes,2);
  finishNew(snapshot('account-b'));await current;
});
