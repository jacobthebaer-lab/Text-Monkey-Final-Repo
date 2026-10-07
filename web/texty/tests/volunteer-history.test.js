import test from 'node:test';
import assert from 'node:assert/strict';
import {createVolunteerHistory} from '../public/volunteer-history.js';
const row=(id,date='2026-07-01')=>({id:String(id),fictional:true,status:'simulated',created_at:date+'T12:00:00',body:'Synthetic reply'});

test('fictional history uses bounded reads and combines earlier pages in date order',async()=>{
  const calls=[];
  const results=[{volunteer_id:'1',fictional:true,messages:[row(2,'2026-09-01'),row(3,'2026-08-01')],next_before_id:2},
    {volunteer_id:'1',fictional:true,messages:[row(1)],next_before_id:null}];
  const history=createVolunteerHistory({api:async path=>{calls.push(path);return results.shift();},getSelectedId:()=> '1',getSessionEpoch:()=>1,render(){}});
  await history.load('1');await history.load('1',{older:true});
  assert.deepEqual(calls,['/api/volunteers/1/history?limit=100','/api/volunteers/1/history?limit=100&before_id=2']);
  assert.deepEqual(history.view('1').messages.map(r=>r.id),['1','3','2']);
  assert.equal(history.view('1').next,null);
  await history.load('1',{older:true});assert.equal(calls.length,2);
  assert.equal(history.view('2'),null);
});

test('switching people or login sessions discards a late history response',async()=>{
  let selected='1',epoch=1,release;
  const pending=new Promise(resolve=>{release=resolve;});
  const history=createVolunteerHistory({api:()=>pending,getSelectedId:()=>selected,getSessionEpoch:()=>epoch,render(){}});
  const first=history.load('1');selected='2';history.reset();
  release({volunteer_id:'1',fictional:true,messages:[row(1)],next_before_id:null});await first;
  assert.equal(history.view('1'),null);assert.equal(history.view('2'),null);
  selected='1';let finish;
  const next=createVolunteerHistory({api:()=>new Promise(resolve=>{finish=resolve;}),getSelectedId:()=>selected,getSessionEpoch:()=>epoch,render(){}});
  const second=next.load('1');epoch++;
  finish({volunteer_id:'1',fictional:true,messages:[row(1)],next_before_id:null});await second;
  assert.equal(next.view('1'),null);
});

test('wrong person, unverified simulation, live status or API failure never display history',async()=>{
  for(const result of [
    {volunteer_id:'2',fictional:true,messages:[row(1)]},
    {volunteer_id:'1',fictional:false,messages:[row(1)]},
    {volunteer_id:'1',fictional:true,messages:[{...row(1),status:'received'}]},
    new Error('History temporarily unavailable.'),
  ]) {
    const history=createVolunteerHistory({api:async()=>{if(result instanceof Error)throw result;return result;},getSelectedId:()=> '1',getSessionEpoch:()=>1,render(){}});
    await history.load('1');
    assert.deepEqual(history.view('1').messages,[]);assert.ok(history.view('1').error);
    assert.equal(history.view('1').loading,false);
  }
});

test('real histories page independently of the global inbox and preserve exact bodies and statuses',async()=>{
  const calls=[], exact='[Fictional history] Exact real body, unchanged.';
  const results=[{volunteer_id:'1',fictional:false,messages:[{id:'202',fictional:false,status:'submitted',body:exact,created_at:'2026-10-06T18:00:00Z'}],next_before_id:202},
    {volunteer_id:'1',fictional:false,messages:[{id:'1',fictional:false,status:'received',body:'Older real reply',created_at:'2026-01-01T18:00:00Z'}],next_before_id:null}];
  const history=createVolunteerHistory({api:async path=>{calls.push(path);return results.shift();},getSelectedId:()=> '1',getSessionEpoch:()=>1,render(){}});
  await history.load('1',{fictional:false});await history.load('1',{older:true,fictional:false});
  assert.deepEqual(history.view('1').messages.map(row=>row.id),['1','202']);
  assert.equal(history.view('1').messages[1].body,exact);
  assert.equal(history.view('1').messages[1].status,'submitted');
  assert.equal(calls[1],'/api/volunteers/1/history?limit=100&before_id=202');
});

test('background history refresh updates statuses while keeping loaded earlier pages and chronological offsets',async()=>{
  const results=[{volunteer_id:'1',fictional:false,messages:[{id:'20',fictional:false,status:'queued',created_at:'2026-10-06T09:00:00-06:00'}],next_before_id:20},
    {volunteer_id:'1',fictional:false,messages:[{id:'1',fictional:false,status:'received',created_at:'2026-01-01T15:00:00Z'}],next_before_id:null},
    {volunteer_id:'1',fictional:false,messages:[{id:'20',fictional:false,status:'submitted',created_at:'2026-10-06T09:00:00-06:00'},{id:'21',fictional:false,status:'received',created_at:'2026-10-06T14:00:00Z'}],next_before_id:20}];
  const history=createVolunteerHistory({api:async()=>results.shift(),getSelectedId:()=> '1',getSessionEpoch:()=>1,render(){}});
  await history.load('1',{fictional:false});await history.load('1',{older:true});
  await history.load('1',{fictional:false,preserve:true});
  assert.deepEqual(history.view('1').messages.map(row=>row.id),['1','21','20']);
  assert.equal(history.view('1').messages.at(-1).status,'submitted');
  assert.equal(history.view('1').next,null);
});
