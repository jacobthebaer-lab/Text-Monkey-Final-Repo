import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

function fixture(state) {
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}}}]));
  const listeners=new Map(),calls=[];
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(type,fn)=>listeners.set(type,fn)};
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.location={hash:'#access_token=synthetic-coverage',pathname:'/'};
  globalThis.history={replaceState(){globalThis.location.hash='';}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});let data;
    if(path==='/api/state')data=state;
    else if(path==='/api/config')data={connected:true};
    else if(path==='/api/setup')data={details:{church_name:'Sample church'},completed:true};
    else if(path==='/api/setup/contacts')data={contacts:[]};
    else if(path==='/api/setup/admin-texts')data={enabled:false,issues:[],recent:[]};
    else if(path==='/api/planning/availability-collections')data={collections:[]};
    else if(path.startsWith('/api/notification-status?'))data={notifications:[]};
    else throw Error('Unexpected path '+path);
    return {ok:true,json:async()=>data};
  };
  return {calls,html:()=>elements.get('#app').innerHTML,click:dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}}),
    restore(){for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}};
}
function requiredState() {
  const state=seed();state.proposals=[];state.escalations=[];
  state.shifts=Array.from({length:5},(_,i)=>({id:String(i+1),event_id:'106',title:'Sunday sample',role:'Greeter',ministry:'Welcome',required:1,starts_at:'2026-10-11T15:00:00Z',ends_at:'2026-10-11T16:15:00Z'}));
  state.assignments=[{id:'assignment',shift_id:'1',volunteer_id:state.volunteers[0].id,status:'approved'}];
  state.staffing=[{event_id:'106',title:'Sunday sample',starts_at:'2026-10-11T15:00:00Z',covered:1,required:10,fully_staffed:false,gaps:[{role:'Greeter',open:4},{role:'Production',open:5}]}];
  return state;
}
test('Shifts uses full event requirements and shows missing roles without creating slots or mappings',async()=>{
  const state=requiredState(),before=structuredClone(state),f=fixture(state);
  try {
    await import('../public/app.js?coverage-missing-roles');
    assert.match(f.html(),/1 of 10 required spots covered/);
    await f.click({page:'schedule'});
    const html=f.html(),requirements=html.slice(html.indexOf('aria-labelledby="required-role-gaps"'),html.indexOf('aria-label="Shift schedule'));
    assert.match(html,/1 \/ 10/);assert.match(html,/9<\/strong>/);
    assert.match(requirements,/<strong>Production<\/strong>/);assert.match(requirements,/5 open/);assert.match(requirements,/No scheduled shifts shown/);
    assert.match(requirements,/5 scheduled shifts shown/);
    const shifts=html.slice(html.indexOf('aria-label="Shift schedule'));
    assert.equal((shifts.match(/<strong>Greeter<\/strong>/g)||[]).length,5);
    assert.doesNotMatch(requirements,/data-shift|data-pco|data-split-role|data-approve/);
    assert.deepEqual(state,before);assert.ok(f.calls.every(call=>!call.options.body && call.options.method==='GET'));
  }finally{f.restore();}
});
test('authoritative event coverage is preserved when visible assignments differ and names are escaped',async()=>{
  const state=requiredState();state.staffing[0].covered=0;state.staffing[0].required=11;
  state.staffing[0].gaps=[{role:'Production <unsafe>',open:6}];state.staffing[0].title='Sunday <unsafe>';
  const f=fixture(state);
  try {
    await import('../public/app.js?coverage-reported-counts');await f.click({page:'schedule'});
    assert.match(f.html(),/0 \/ 11/);assert.match(f.html(),/Production &lt;unsafe&gt;/);assert.match(f.html(),/6 open/);
    assert.doesNotMatch(f.html(),/<unsafe>/);
  }finally{f.restore();}
});
test('a missing staffing plan is visible in Shifts without claiming full coverage',async()=>{
  const state=requiredState();state.staffing[0]={...state.staffing[0],covered:0,required:0,gaps:[]};const f=fixture(state);
  try {
    await import('../public/app.js?coverage-no-plan');await f.click({page:'schedule'});
    assert.match(f.html(),/no required staffing plan is saved/);assert.match(f.html(),/Review event staffing plans/);
    assert.doesNotMatch(f.html(),/Every role is covered/);
  }finally{f.restore();}
});
test('legacy disconnected shift summaries retain their recorded coverage when staffing snapshots are absent',async()=>{
  const state=requiredState();delete state.staffing;const f=fixture(state);
  try {
    await import('../public/app.js?coverage-fallback');await f.click({page:'schedule'});
    assert.match(f.html(),/1 \/ 5/);assert.doesNotMatch(f.html(),/<strong>Production<\/strong>/);
    assert.deepEqual(state.shifts.length,5);assert.ok(f.calls.every(call=>!call.options.body));
  }finally{f.restore();}
});
