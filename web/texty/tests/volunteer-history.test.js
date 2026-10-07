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
