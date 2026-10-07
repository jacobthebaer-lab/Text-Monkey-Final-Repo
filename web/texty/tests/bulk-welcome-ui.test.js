import test from 'node:test';
import assert from 'node:assert/strict';
import {createBulkWelcome} from '../public/bulk-welcome.js';
function fixture(){
  let epoch=1,ready=true,people=Array.from({length:40},(_,i)=>({id:String(i+1),name:'Example '+i,consent:true,status:'active',welcome_eligible:true}));
  const calls=[],renders=[];let failures=0,release=null,changed=0,uuids=0;
  const server=new Map();
  const bulk=createBulkWelcome({getSessionEpoch:()=>epoch,getReady:()=>ready,getVolunteers:()=>people,
    uuid:()=>`request-${++uuids}`,render:()=>renders.push(bulk.panel()),onChanged:async()=>{changed++;},
    api:async(path,body)=>{
      calls.push({path,body:structuredClone(body)});if(release)await release;
      const rows=server.get(body.request_id)||[];
      if(rows.length<body.volunteer_ids.length){const id=String(body.volunteer_ids[rows.length]);rows.push({volunteer_id:id,name:'Example '+id,status:'prepared',delivery:'queued_for_mac'});}
      server.set(body.request_id,rows);
      if(failures){failures--;throw Error('Synthetic lost response');}
      return {request_id:body.request_id,results:structuredClone(rows),completed:rows.length,total:body.volunteer_ids.length,done:rows.length===body.volunteer_ids.length};
    }});
  return {bulk,calls,renders,server,get people(){return people;},setPeople:value=>{people=value;},epoch:()=>epoch++,ready:v=>{ready=v;},fail:n=>{failures=n;},wait:v=>{release=v;},changed:()=>changed};
}
test('filtered select all excludes fictional, STOP, inactive and Connecting profiles, selection survives polls',()=>{
  const f=fixture();f.people[1].fictional=true;f.people[2].consent=false;f.people[3].status='inactive';f.people[4].welcome_eligible=false;
  f.bulk.selectFiltered(f.people.slice(0,10),true);assert.deepEqual([...f.bulk.selected()],['1','6','7','8','9','10']);
  f.setPeople(structuredClone(f.people));assert.equal(f.bulk.selected().size,6);
  f.bulk.selectFiltered(f.people.slice(0,2),false);assert.deepEqual([...f.bulk.selected()],['6','7','8','9','10']);
  f.bulk.select('20',true);assert.ok(f.bulk.selected().has('20'));
  assert.match(f.bulk.panel(),/current filtered roster/);
});
test('40 selected people process one step at a time with distinct per-person results and honest labels',async()=>{
  const f=fixture();f.bulk.selectFiltered(f.people,true);await f.bulk.send();
  assert.equal(f.calls.length,40);assert.equal(new Set(f.calls.map(c=>c.body.request_id)).size,1);
  assert.ok(f.calls.every(c=>c.body.volunteer_ids.length===40));assert.equal(f.bulk.selected().size,0);
  assert.equal(f.changed(),1);assert.match(f.bulk.panel(),/Queued for Messages/);assert.doesNotMatch(f.bulk.panel(),/Delivered/);
  assert.ok(f.renders.some(html=>html.includes('Preparing 20 of 40')));
});
test('lost result resumes same stable batch ID and clears only successful recipients',async()=>{
  const f=fixture();f.bulk.selectFiltered(f.people.slice(0,3),true);f.fail(1);await f.bulk.send();
  const id=f.calls[0].body.request_id;assert.match(f.bulk.panel(),/Resume welcome request/);
  assert.equal(f.bulk.selected().size,3);await f.bulk.resume();
  assert.ok(f.calls.every(c=>c.body.request_id===id));assert.equal(f.bulk.selected().size,0);
  assert.equal(f.server.get(id).length,3);
});
test('new explicit retry selects only held/failed people, pending review counts as prepared',async()=>{
  const f=fixture();f.bulk.selectFiltered(f.people.slice(0,3),true);
  f.server.set('request-1',[{volunteer_id:'1',name:'One',status:'prepared',delivery:'awaiting_confirmation'},
    {volunteer_id:'2',name:'Two',status:'held',reason:'STOP'},{volunteer_id:'3',name:'Three',status:'failed',reason:'Gloo unavailable'}]);
  await f.bulk.send();assert.deepEqual([...f.bulk.selected()],['2','3']);
  assert.match(f.bulk.panel(),/Awaiting exact review. Nothing sent/);
  assert.match(f.bulk.panel(),/AI unavailable/);assert.doesNotMatch(f.bulk.panel(),/Gloo/);
  assert.equal(f.server.get('request-1')[2].reason,'Gloo unavailable');
  await f.bulk.retry();const last=f.calls.at(-1);assert.equal(last.body.request_id,'request-2');assert.deepEqual(last.body.volunteer_ids,[2,3]);
  assert.equal(f.bulk.selected().size,0);
});
test('session change clears selection and prevents another request after an in-flight result',async()=>{
  const f=fixture();f.bulk.selectFiltered(f.people.slice(0,3),true);let resolve;f.wait(new Promise(r=>resolve=r));
  const pending=f.bulk.send();assert.equal(f.calls.length,1);f.epoch();assert.equal(f.bulk.selected().size,0);
  resolve();await pending;assert.equal(f.calls.length,1);assert.equal(f.changed(),0);assert.doesNotMatch(f.bulk.panel(),/Example 1/);
});
test('busy state blocks selection changes and duplicate explicit actions',async()=>{
  const f=fixture();f.bulk.selectFiltered(f.people.slice(0,2),true);let resolve;f.wait(new Promise(r=>resolve=r));
  const pending=f.bulk.send();f.bulk.select('3',true);await f.bulk.send();assert.equal(f.calls.length,1);assert.equal(f.bulk.selected().size,2);
  resolve();await pending;assert.equal(f.calls.length,2);
});


test('welcome result names hide markers without changing saved per-person results',async()=>{
  const f=fixture();f.bulk.select('1',true);
  const row={volunteer_id:'1',name:'Casey <Example> [Mock]',status:'prepared',delivery:'awaiting_confirmation'};
  f.server.set('request-1',[row]);await f.bulk.send();
  assert.match(f.bulk.panel(),/Casey &lt;Example&gt;/);assert.doesNotMatch(f.bulk.panel(),/Mock|<Example>/);
  assert.equal(row.name,'Casey <Example> [Mock]');assert.equal(f.calls[0].body.volunteer_ids[0],1);
});
