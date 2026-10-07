import test from 'node:test';
import assert from 'node:assert/strict';
import {createEventAdmins} from '../public/admin-notifications.js';

const snapshot=()=>({default_admins:[{id:1,name:'Primary Casey'}],admins:[
  {id:1,name:'Primary Casey',phone:'+12025550199',record_hash:'a'.repeat(64),primary:true,eligible:true},
  {id:2,name:'Riley <literal>',phone:'+12025550198',record_hash:'b'.repeat(64),primary:false,eligible:true}],events:[
  {id:10,title:'Sunday <literal>',starts_at:'2026-10-11T15:00:00Z',editable:true,mode:'inherit',recipient_ids:[],event_hash:'c'.repeat(64),
    changes:{cancelled:[{name:'Jordan',role:'Greeter'}],filled:[{name:'Morgan',role:'Greeter'}],initial_roster:[{name:'Drew',role:'Sound'}]},coverage:{gaps:[{role:'Parking',open:1}]}},
  {id:11,title:'Second service',starts_at:'2026-10-11T17:00:00Z',editable:true,mode:'inherit',recipient_ids:[],event_hash:'d'.repeat(64),changes:{},coverage:{gaps:[]}}]});

function fixture(api){
  const prior=globalThis.FormData;let token='account-a',epoch=1,renders=0;
  globalThis.FormData=class{constructor(form){this.fields=form.fields;}get(k){return this.fields[k];}getAll(k){return this.fields[k]||[];}};
  const flow=createEventAdmins({api,getMode:()=>token?'live':'demo',getToken:()=>token,getSessionEpoch:()=>epoch,render:()=>renders++});
  const form=(id,mode='selected',ids=['2'])=>{const output={textContent:''},button={disabled:false};return{dataset:{eventAdmins:String(id)},fields:{mode,recipient_id:ids},querySelector:k=>k==='.error'?output:button,output,button};};
  return{flow,form,account(next){token=next;epoch++;flow.reset();},renders:()=>renders,restore(){globalThis.FormData=prior;}};
}

test('event portal shows default, original/cancelled/filled names and explicit settings management',async()=>{
  const f=fixture(async()=>snapshot());try{
    await f.flow.load();const html=f.flow.panel();
    assert.match(html,/Current default: <strong>Primary Casey/);assert.match(html,/Choose admins for this event/);
    assert.match(html,/none checked to turn off pre-event texts/);assert.match(html,/Manage saved admins in Settings/);
    assert.match(html,/Canceled:<\/strong> Jordan/);assert.match(html,/Filled spots:<\/strong> Morgan/);
    assert.match(html,/Initial roster still serving:<\/strong> Drew/);assert.match(html,/Parking: 1 open/);
    assert.match(html,/Riley &lt;literal&gt;/);assert.doesNotMatch(html,/<literal>/);
    assert.match(f.flow.managementPanel(),/Choose recipients by event/);
  }finally{f.restore();}
});

test('save multiple recipients uses exact event and server record hashes, reload preserves per-event isolation',async()=>{
  let data=snapshot();const posts=[];
  const f=fixture(async(path,body)=>{
    if(body){posts.push({path,body});assert.equal(path,'/api/setup/event-admins/10');data={...data,events:data.events.map(e=>e.id===10?{...e,mode:body.mode,recipient_ids:body.recipients.map(p=>p.id),event_hash:'e'.repeat(64)}:e)};}
    return data;
  });try{
    await f.flow.load();await f.flow.submit(f.form(10,'selected',['1','2']));
    assert.deepEqual(posts[0].body,{mode:'selected',recipients:[{id:1,record_hash:'a'.repeat(64)},{id:2,record_hash:'b'.repeat(64)}],event_hash:'c'.repeat(64)});
    await f.flow.load();const html=f.flow.panel();
    assert.match(html,/value="2" checked/);assert.match(html,/Event recipients saved. No texts sent/);
    assert.equal(data.events[1].mode,'inherit');
    await f.flow.submit(f.form(10,'inherit',['1','2']));assert.deepEqual(posts[1].body.recipients,[]);
    await f.flow.submit(f.form(10,'selected',[]));assert.deepEqual(posts[2].body.recipients,[]);
    assert.equal(posts.length,3);
  }finally{f.restore();}
});

test('foreign-owned config is read-only and oversized-list hold is visible without leaking review tokens',async()=>{
  const data=snapshot();data.events[0]={...data.events[0],editable:false,mode:'managed_elsewhere',recipient_ids:[],event_hash:null};
  data.events[1].notices=[{state:'blocked_policy',reason:'Complete list exceeds 1,600 characters. Review the full list in Shifts.'}];
  let posts=0;const f=fixture(async(_,body)=>{if(body)posts++;return data;});try{
    await f.flow.load();const html=f.flow.panel();
    assert.match(html,/Recipients are managed by another administrator/);assert.match(html,/full list in Shifts/);
    await f.flow.submit(f.form(10));assert.equal(posts,0);
    assert.doesNotMatch(html,/review_id|record_hash|c{64}/);
  }finally{f.restore();}
});

test('duplicate save is suppressed and old-account result cannot restore private settings',async()=>{
  let finish,posts=0;const f=fixture(async(_,body)=>body?(posts++,new Promise(resolve=>{finish=resolve;})):snapshot());try{
    await f.flow.load();const form=f.form(10),saving=f.flow.submit(form);
    assert(form.button.disabled);await f.flow.submit(f.form(10));assert.equal(posts,1);
    f.account(null);finish({...snapshot(),default_admins:[{name:'Old private account'}]});await saving;
    assert.equal(f.flow.panel(),'');assert.equal(f.renders(),0);
  }finally{f.restore();}
});

test('failure preserves form choices, reenables save and makes no other endpoint call',async()=>{
  const posts=[];const f=fixture(async(path,body)=>{if(body){posts.push(path);throw Error('Event changed. Reload before saving.');}return snapshot();});try{
    await f.flow.load();const form=f.form(10);await f.flow.submit(form);
    assert.match(form.output.textContent,/Event changed/);assert(!form.button.disabled);
    assert.deepEqual(form.fields.recipient_id,['2']);assert.deepEqual(posts,['/api/setup/event-admins/10']);
  }finally{f.restore();}
});
