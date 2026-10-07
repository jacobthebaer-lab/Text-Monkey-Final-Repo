import test from 'node:test';
import assert from 'node:assert/strict';
import {createSplitCoverage} from '../public/split-coverage.js';

const data=()=>({roles:[{id:1,name:'Fictional <Role>',allowed:false}],evidence:[{parent_id:8,outreach_id:4,incoming_id:12,
  role:'Fictional Role',event:'Fictional Event',start:'2026-12-01T16:00:00Z',end:'2026-12-01T18:00:00Z',actual_reply:'Only <one> hour'}],reviews:[],coverage:[]});
function fixture(){
  let mode='live',token='fictional-token',epoch=1,result=data(),failure=null,wait=null;
  const calls=[];
  const ui=createSplitCoverage({getMode:()=>mode,getToken:()=>token,getSessionEpoch:()=>epoch,render(){},
    api:async(path,body)=>{calls.push({path,body});if(wait)await wait;if(failure)throw new Error(failure);return structuredClone(result);}});
  return {ui,calls,setData:v=>result=v,setMode:v=>mode=v,setFailure:v=>failure=v,setWait:v=>wait=v,switchUser:()=>epoch++,logout:()=>{token=null;epoch++;}};
}
const button=(action,fields={})=>({dataset:{splitAction:action,...fields}});

test('explicit initial load exposes escaped actual evidence without changing a role or booking',async()=>{
  const f=fixture();assert.match(f.ui.panel(),/Load split coverage/);assert.equal(f.calls.length,0);
  await f.ui.action(button('refresh'));assert.equal(f.calls.length,1);assert.equal(f.calls[0].body,undefined);
  assert.match(f.ui.panel(),/Only &lt;one&gt; hour/);assert.match(f.ui.panel(),/ &lt;Role&gt;/);
  assert.match(f.ui.panel(),/<h3>Role, Event<\/h3>/);
  assert.doesNotMatch(f.ui.panel(),/Fictional/);
  assert.match(f.ui.panel(),/No helper has been booked|Helpers are booked only/);
});

test('preparation retries preserve source IDs and one request ID without approval or delivery',async()=>{
  const f=fixture();await f.ui.load();const html=f.ui.panel();
  const request=html.match(/data-request="([^"]+)"/)[1];
  f.setFailure('Fictional connection failure');await f.ui.action(button('partition',{input:'12',request}));
  assert.match(f.ui.panel(),/role="alert"/);f.setFailure(null);
  await f.ui.action(button('partition',{input:'12',request}));
  const writes=f.calls.filter(c=>c.body);assert.equal(writes.length,2);assert.deepEqual(writes[0],writes[1]);
  assert.deepEqual(writes[0].body,{parent_id:8,outreach_id:4,incoming_id:12,request_id:request});
  assert.ok(writes.every(c=>c.path==='/api/split-coverage/partitions'));
});

test('offline preview and signed-out UI expose no live controls or mutations',async()=>{
  const f=fixture();f.setMode('demo');assert.equal(f.ui.panel(),'');await f.ui.action(button('refresh'));assert.equal(f.calls.length,0);
  f.setMode('live');f.logout();await f.ui.setRole({dataset:{splitRole:'1'},checked:true});assert.equal(f.calls.length,0);
});

test('a changed account discards old evidence and an in-flight result',async()=>{
  const f=fixture();let resolve;f.setWait(new Promise(r=>resolve=r));const loading=f.ui.load();
  f.switchUser();resolve();await loading;assert.doesNotMatch(f.ui.panel(),/Only &lt;one&gt;/);
  await f.ui.action(button('partition',{input:'12',request:'old-id'}));assert.equal(f.calls.length,1);
});

for(const kind of ['confirm_split_partition','confirm_split_booking']){
  test(`${kind} shows exact dated DST offsets and source precision`,async()=>{
    const f=fixture(),value=data(),start='2026-11-01T01:00:05.123456-06:00',end='2026-11-01T01:00:05.123456-07:00';
    value.reviews=[{id:91,kind,status:'pending',content_hash:'synthetic-exact-hash',scope:{
      intervals:[{start,end}],children:[{volunteer_id:1,snapshot:{start,end}}]}}];
    f.setData(value);await f.ui.load();const html=f.ui.panel();
    assert.match(html,/2026-11-01 01:00:05\.123456 UTC-06:00 \(America\/Denver\)/);
    assert.match(html,/2026-11-01 01:00:05\.123456 UTC-07:00 \(America\/Denver\)/);
  });
}
