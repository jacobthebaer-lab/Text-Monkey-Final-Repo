import test from 'node:test';
import assert from 'node:assert/strict';
import {preferencesPanel} from '../public/signup-preferences.js';
import {seed} from '../public/domain.js';

const draft={volunteer_id:1,name:'Fictional Recipient',source_hash:'a'.repeat(64),actual_reply:'Second Wednesday childcare, paired Sunday roles. December off.',
  windows:[{index:0,roles:['Greeter'],weekday:6,start_time:'09:00',end_time:'10:15'},
    {index:1,roles:['Production'],weekday:6,start_time:'11:00',end_time:'12:15'},
    {index:2,roles:['Child Care'],weekday:2,start_time:'18:00',end_time:'20:00',month_ordinals:[2]}],
  constraints:[{kind:'same_day',description:'Production with greeting on the same Sundays'}],
  group_windows:[2],event_types:[{id:71,name:"Women's Ministry"}],unavailable_months:['2026-12'],
  role_caps:[{role_name:'Greeter',max_per_month:2}]};

test('usable review controls preserve role-specific occurrence without inventing fixed Sundays',()=>{
  const html=preferencesPanel([draft],true);
  assert.match(html,/Review saved signup preferences/);
  assert.match(html,/name="group:2"/);assert.doesNotMatch(html,/name="group:0"/);
  assert.match(html,/Second Wednesday/);assert.match(html,/name="ordinal:2"[\s\S]*value="2" selected/);
  assert.match(html,/Choosing a stable schedule does not restrict otherwise flexible Sundays/);
  assert.match(html,/clearance and scheduling remain separate/);
  assert.equal(preferencesPanel([draft],false),'');
  assert.doesNotMatch(preferencesPanel([{...draft,name:'<script>bad</script>'}],true),/<script>/);
});

test('real app stages from Volunteers and approves through existing exact review card only',async()=>{
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}}}]));
  const listeners=new Map(),calls=[],state=seed(),storage=new Map();
  state.signup_preference_drafts=[draft];state.proposals=[];
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'#access_token=synthetic-preference-session',pathname:'/texty'};
  globalThis.history={replaceState(){globalThis.location.hash='';}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.FormData=class{constructor(form){this.entries=form.data;}[Symbol.iterator](){return Object.entries(this.entries)[Symbol.iterator]();}};
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});let result;
    if(path==='/api/config')result={connected:true,aiReady:true,messagingTransport:'mac_messages',humanConfirmationRequired:false};
    else if(path==='/api/state')result=state;
    else if(path==='/api/setup')result={details:{church_name:'Synthetic church'},completed:true};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/signup-preferences/1/review'){
      state.proposals=[{id:'81',phone:state.volunteers[0].phone,intent:'confirm_record',summary:'Complete saved preferences',
        confirmation_required:true,content_hash:'b'.repeat(64),record_change:{record:'Volunteer',before:{preferences:{onboarding_stage:'availability'}},after:{preferences:{onboarding_stage:'complete'}}},status:'pending'}];
      result={approval_id:81,state:'pending_exact_review'};
    }else if(path==='/api/proposals/81/approve'){
      state.proposals[0].status='approved';state.signup_preference_drafts=[];
      result={reviewed:true,delivery:'not_queued',message_id:null,notes:['Exact record change approved and applied.']};
    }else throw new Error('Unexpected request '+path);
    return {ok:true,json:async()=>result};
  };
  const click=dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}});
  try{
    await import('../public/app.js?preference-review-fixture');await click({page:'volunteers'});
    assert.match(elements.get('#app').innerHTML,/data-preference-review="1"/);
    const form={id:'',dataset:{preferenceReview:'1',sourceHash:draft.source_hash},data:{'group:2':'71','ordinal:0':'','ordinal:1':'','ordinal:2':'2','absence:2026-12':'on'},querySelector:()=>({textContent:''})};
    await listeners.get('submit')({preventDefault(){},target:form,submitter:{disabled:false}});
    const staged=calls.find(c=>c.path==='/api/signup-preferences/1/review');assert.ok(staged);
    assert.deepEqual(JSON.parse(staged.options.body),{source_hash:draft.source_hash,event_mappings:[{window_index:2,event_type_id:71}],window_ordinals:[{window_index:2,ordinals:[2]}],absence_months:['2026-12']});
    assert.match(elements.get('#app').innerHTML,/Approve exact change/);
    assert.ok(!calls.some(c=>c.path.includes('/approve')||c.path.includes('/send')));
    await click({approve:'81'});
    assert.deepEqual(JSON.parse(calls.find(c=>c.path==='/api/proposals/81/approve').options.body),{content_hash:'b'.repeat(64)});
    assert.ok(!calls.some(c=>c.path.includes('/send')||c.path.includes('/qualifications')));
  }finally{for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});
