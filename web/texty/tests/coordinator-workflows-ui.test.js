import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';
import {createCoordinatorWorkflows} from '../public/coordinator-workflows.js';


test('signed-in Shifts command reaches the route, shows a review card and submits its exact hash',async()=>{
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}}}]));
  const listeners=new Map(),calls=[],state=seed(),storage=new Map();
  state.proposals=[];state.messages=[];
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'#access_token=synthetic-coordinator-session',pathname:'/texty'};
  globalThis.history={replaceState(){globalThis.location.hash='';}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.FormData=class {constructor(form){this.values=form.data;} [Symbol.iterator](){return Object.entries(this.values)[Symbol.iterator]();}};
  let flags=[];
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});let result;
    if(path==='/api/config')result={connected:true,aiReady:true,humanConfirmationRequired:false,messagingTransport:'mac_messages'};
    else if(path==='/api/state')result=state;
    else if(path==='/api/setup')result={details:{church_name:'Synthetic church'},completed:true};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/coordinator')result={coordinators:[{id:7,name:'Synthetic Coordinator'}]};
    else if(path==='/api/coordinator/capacity'){
      if(options.method==='POST')flags=[{id:3,type:'single_point_of_failure',status:'open',summary:'Gloo narration held. Review the structured evidence.',
        evidence:{observation:'Greeter has a small recorded pool.',narration:{state:'held'}}}];
      result={flags,state:'held',sent:0};
    } else if(path==='/api/coordinator/command'){
      state.proposals=[{id:'41',phone:'',intent:'confirm_record',summary:'Review event staffing',confirmation_required:true,
        content_hash:'a'.repeat(64),reason:'Add two greeter slots',expires_at:'2026-10-06T18:00:00Z',status:'pending',confidence:1,
        record_change:{record:'Shift',before:null,after:{event_id:12,role_id:2,slot_index:1}}}];
      result={state:'pending_exact_review',final_text:'Two greeter slots are ready for your review.',approval_ids:[41],sent:0};
    } else if(path==='/api/proposals/41/approve'){
      assert.deepEqual(JSON.parse(options.body),{content_hash:'a'.repeat(64)});
      state.proposals[0].status='approved';result={delivery:'not_requested',notes:['Exact record change approved and applied.']};
    } else throw new Error('Unexpected fixture request '+path);
    return {ok:true,status:200,json:async()=>result};
  };
  const click=dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false,disabled:false})}});
  try{
    await import('../public/app.js?coordinator-workspace-fixture');
    await click({page:'schedule'});
    assert.match(elements.get('#app').innerHTML,/id="coordinator-command-form"/);
    assert.match(elements.get('#app').innerHTML,/Synthetic Coordinator/);
    const form={id:'coordinator-command-form',data:{coordinator_id:'7',command:'Add two greeter slots to Sunday'}};
    await listeners.get('submit')({preventDefault(){},target:form});
    const command=calls.find(c=>c.path==='/api/coordinator/command');
    assert.deepEqual(JSON.parse(command.options.body),{coordinator_id:7,command:form.data.command});
    assert.equal(command.options.headers.Authorization,'Bearer synthetic-coordinator-session');
    assert.match(elements.get('#app').innerHTML,/Your proposed changes are ready/);
    await click({page:'messages'});
    assert.match(elements.get('#app').innerHTML,/Approve exact change/);
    assert.match(elements.get('#app').innerHTML,/data-approve="41"/);
    await click({approve:'41'});
    assert.ok(calls.some(c=>c.path==='/api/proposals/41/approve'));
    await click({page:'schedule'});
    await click({coordinatorAction:'capacity'});
    assert.match(elements.get('#app').innerHTML,/Gloo narration held/);
    assert.match(elements.get('#app').innerHTML,/Greeter has a small recorded pool/);
    assert.ok(!calls.some(c=>c.path==='/api/reply'||c.path.includes('/send')||c.path.includes('/mac/')));
  }finally{for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});


test('preview and account changes cannot publish coordinator results or issue mutations',async()=>{
  let mode='demo',token=null,epoch=1,resolve;
  const calls=[];
  const panel=createCoordinatorWorkflows({api:async(path,body)=>{calls.push({path,body});return new Promise(r=>{resolve=r;});},
    getMode:()=>mode,getToken:()=>token,getSessionEpoch:()=>epoch,render(){}});
  assert.equal(panel.panel(),'');await panel.capacity();assert.equal(calls.length,0);
  mode='live';token='synthetic-owner';
  const pending=panel.capacity();
  mode='demo';token=null;epoch++;panel.reset();
  resolve({flags:[{summary:'Old account result'}],state:'ready'});await pending;
  mode='live';token='different-synthetic-owner';
  assert.ok(!panel.panel().includes('Old account result'));
});


test('a direct account switch hides cached coordinator names and staffing facts',async()=>{
  let token='synthetic-first-owner',epoch=1;
  const panel=createCoordinatorWorkflows({api:async(path)=>path==='/api/coordinator'?{coordinators:[{id:7,name:'First owner coordinator'}]}:
    {flags:[{id:8,status:'open',summary:'First owner staffing facts',type:'burnout',evidence:{narration:{state:'ready'}}}]},
    getMode:()=> 'live',getToken:()=>token,getSessionEpoch:()=>epoch,render(){}});
  await panel.load();
  assert.match(panel.panel(),/First owner staffing facts/);
  token='synthetic-second-owner';epoch++;
  assert.ok(!panel.panel().includes('First owner'));
});
