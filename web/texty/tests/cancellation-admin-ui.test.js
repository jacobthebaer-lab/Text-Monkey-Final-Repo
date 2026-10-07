import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

test('connected internal cancellation shows safe current bookings and only read-only navigation',async()=>{
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval'];
  const saved=Object.fromEntries(keys.map(key=>[key,globalThis[key]]));
  const elements=new Map(['#app','#modal','#toast'].map(key=>[key,{innerHTML:'',textContent:'',classList:{add(){},remove(){}}}]));
  const listeners=new Map(),calls=[],state=seed();
  state.proposals=[];state.messages=[];state.fills=[];
  state.escalations=[{id:'1',category:'cancellation_scope',status:'open',severity:'normal',summary:'Hidden incoming/care text',
    internal_review:{volunteer_id:'1',recipient_name:'Casey <script> Example [Fictional]',scope_changed:false,delivery:'internal_only',
      bookings:[{assignment_id:'1',shift_id:'1',role:'Greeter',event_title:'Demo: Sunday service [Mock]',starts_at:'2026-10-04T15:00:00Z',status:'approved'}],
      next_step:'Review this volunteer’s current roles and dates in Shifts. Identify the intended booking before making any change.'}}];
  globalThis.document={querySelector:key=>elements.get(key),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.location={hash:'#access_token=synthetic-ui-only',pathname:'/texty'};globalThis.history={replaceState(){}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});let payload;
    if(path==='/api/config')payload={connected:true,aiReady:true,macBridgeConnected:true,humanConfirmationRequired:true};
    else if(path==='/api/state')payload=state;
    else if(path==='/api/setup')payload={details:{church_name:'Synthetic church'},completed:true,revision:1};
    else if(path==='/api/setup/contacts')payload={contacts:[]};
    else if(path==='/api/setup/admin-texts')payload={enabled:false,issues:[],recent:[]};
    else if(path==='/api/planning/availability-collections')payload={collections:[]};
    else if(path.startsWith('/api/notification-status?'))payload={notifications:[],next_offset:null};
    else throw Error('Unexpected request '+path);
    return {ok:true,json:async()=>payload};
  };
  const click=dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}});
  try{
    await import('../public/app.js?cancellation-admin-ui');
    assert.match(elements.get('#app').innerHTML,/1 human follow-up/);
    await click({page:'messages'});
    let html=elements.get('#app').innerHTML;
    assert.match(html,/Cancellation review.*Casey &lt;script&gt; Example/);
    assert.doesNotMatch(html,/Fictional|Mock|Demo:/);
    assert.equal(state.escalations[0].internal_review.recipient_name,'Casey <script> Example [Fictional]');
    assert.match(html,/Current bookings/);assert.match(html,/Greeter/);assert.match(html,/Sunday service/);
    assert.match(html,/Internal review only/);assert.match(html,/does not queue a volunteer text/);
    assert.match(html,/Review Shifts/);assert.doesNotMatch(html,/Hidden incoming\/care text|<script>|data-approve=|data-reject=/);
    state.escalations[0].internal_review.scope_changed=true;
    state.escalations[0].internal_review.bookings=[];
    await click({page:'messages'});html=elements.get('#app').innerHTML;
    assert.match(html,/original booking scope changed/);assert.match(html,/No upcoming bookings remain/);
    await click({page:'schedule'});
    assert.match(elements.get('#app').innerHTML,/Shift notices/);
    assert.ok(calls.every(call=>call.options.method==='GET'&&!call.options.body));
    assert.ok(!calls.some(call=>call.path.includes('/api/proposals/')));
  }finally{for(const[key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
