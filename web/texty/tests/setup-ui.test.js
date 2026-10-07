import test from 'node:test';
import assert from 'node:assert/strict';
import {createSetup} from '../public/setup.js';

// Exercise the production controller's delegated actions with synthetic APIs.
test('actual setup controller saves, resumes, previews and stages without scheduling calls',async()=>{
  const saved=Object.fromEntries(['document','localStorage','FormData'].map(k=>[k,globalThis[k]]));
  const listeners=new Map(), calls=[], messages=[]; let currentForm=null, mode='live', html='';
  let stored={details:{country:'US',timezone:'America/Denver',quiet_start:'21:00',quiet_end:'07:00',monthly_ask_limit:4},revision:0,completed:false};let contacts=[], failSave=false;
  const document={querySelector:selector=>selector==='#church-setup-form'?currentForm:null,addEventListener:(type,fn)=>{if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(fn);}};
  globalThis.document=document;
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.FormData=class {constructor(form){this.data=form.data;}[Symbol.iterator](){return Object.entries(this.data)[Symbol.iterator]();}};
  const api=async(path,body)=>{
    calls.push({path,body});
    if(path==='/api/setup' && !body)return stored;
    if(path==='/api/setup'){if(failSave) throw Error('Setup temporarily unavailable');stored={details:body.details,revision:stored.revision+1,completed:body.complete,saved_at:'2026-10-02T10:00:00Z'};return stored;}
    if(path==='/api/setup/contacts')return{contacts};
    if(path==='/api/setup/coordinator') {assert.deepEqual(body,{revision:stored.revision});return{coordinator_ready:true,texts_sent:0,text_consent_recorded:false};}
    if(path==='/api/setup/preview')return{counts:{ready:1,duplicate:0,invalid:0},rows:[{row:2,name:'Alex Sample [Fictional]',phone:'+12025550111',status:'ready',reason:'Awaiting consent'}],preview_hash:'a'.repeat(64)};
    if(path==='/api/setup/import'){contacts=[{id:'synthetic',name:'Alex Sample [Fictional]',phone:'+12025550111',source:body.source,can_text:false}];return{imported:1,texts_sent:0};}
    throw new Error('Unexpected API request '+path);
  };
  let focused='';
  const querySelector=document.querySelector;
  document.querySelector=selector=>selector==='.setup-card h2'||selector==='.setup-card .error'?{focus(){focused=selector;}}:querySelector(selector);
  let controller;
  const click=async dataset=>{const event={target:{closest:()=>({dataset})},stopImmediatePropagation(){}};for(const fn of listeners.get('click')||[])await fn(event);};
  const submit=async(form,action='continue')=>{for(const fn of listeners.get('submit')||[])await fn({target:form,submitter:{value:action},preventDefault(){},stopImmediatePropagation(){}});};
  try {
    controller=createSetup({api,getMode:()=>mode,getToken:()=> 'synthetic-token',toast:m=>messages.push(m),render:()=>{html=controller.screen();}});
    await controller.load();assert.match(controller.screen(),/Tell us about your church/);
    currentForm={id:'church-setup-form',data:{church_name:'Example Church',affiliation:'Independent',address:'100 Example Way',city:'Example City',region:'CO',postal_code:'80000',coordinator_name:'Alex Sample',coordinator_role:'Coordinator'}};
    await submit(currentForm);assert.equal(stored.details.church_name,'Example Church');assert.match(html,/Make it fit your ministry/);
    assert.equal(focused,'.setup-card h2','Continuing setup focuses the new step');
    await controller.load();assert.equal(controller.details().church_name,'Example Church');
    await click({setupStep:'1'});
    assert.equal(focused,'.setup-card h2');
    failSave=true;
    currentForm={id:'church-setup-form',data:{coordinator_name:'Alex Sample'}};
    await submit(currentForm);
    assert.equal(focused,'.setup-card .error','A failed submission focuses its visible error');
    failSave=false;
    currentForm=null;
    await click({setup:'sample'});
    await submit({id:'contact-map-form',data:{name:'0',first_name:'',last_name:'',phone:'1',email:'2',ministry:'3',country:'US',source:'Synthetic list'}});
    assert.match(controller.importScreen(),/Save 1 staged contact/);
    assert.match(controller.importScreen(),/>Alex Sample<\/td>/);
    assert.doesNotMatch(controller.importScreen(),/Fictional/);
    await click({setup:'commit'});
    assert.match(controller.importScreen(),/Awaiting consent/);
    assert.match(controller.importScreen(),/<strong>Alex Sample<\/strong>/);
    assert.doesNotMatch(controller.importScreen(),/Fictional/);
    assert.equal(contacts[0].name,'Alex Sample [Fictional]');
    const request=calls.find(c=>c.path==='/api/setup/import');assert.equal(request.body.preview_hash,'a'.repeat(64));assert.ok(request.body.submission_id);
    assert.ok(calls.every(c=>c.path.startsWith('/api/setup')));assert.equal(messages.at(-1),'1 contacts staged. No texts sent.');
    stored={...stored,completed:true,details:{...stored.details,coordinator_name:'Alex Sample',coordinator_phone:'+12025550199'}};
    await controller.load();assert.match(controller.screen(),/Set up coordinator tools/);
    await click({setup:'coordinator'});
    assert.match(html,/Coordinator ready/);
    assert.equal(messages.at(-1),'Coordinator tools are ready. No texts sent.');
    assert.equal(calls.filter(c=>c.path==='/api/setup/coordinator').length,1);
    assert.ok(calls.every(c=>!c.path.includes('admin-texts')));
    mode='demo';await controller.load();assert.doesNotMatch(controller.importScreen(),/Alex Sample/);
  } finally {for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});

test('late import parsing cannot expose old-account file content in a new workspace',async()=>{
  const keys=['document','localStorage'],saved=Object.fromEntries(keys.map(key=>[key,globalThis[key]]));
  const listeners=new Map();let epoch=1,release,parseOptions;
  globalThis.document={querySelector:()=>null,addEventListener:(type,fn)=>listeners.set(type,fn)};
  globalThis.localStorage={getItem:()=>null,removeItem(){}};
  const flow=createSetup({api:async(path,body,options)=>path==='/api/setup/parse'?(parseOptions=options,new Promise(resolve=>{release=resolve;})):path==='/api/setup/contacts'?{contacts:[]}:{details:{church_name:'New account',country:'US'},completed:true},
    getMode:()=> 'live',getToken:()=> 'synthetic-token',getSessionEpoch:()=>epoch,render(){},toast(){}});
  try {
    await flow.load();
    const file=new File(['Full name,Mobile\nPrivate Person,3035550123'],'private-contact-file.csv',{type:'text/csv'});
    const pending=listeners.get('change')({target:{id:'contact-file',files:[file]}});
    epoch++;flow.clearSession();await flow.load();
    release({sheets:[{name:'Private contacts',rows:[['Full name','Mobile'],['Private Person','3035550123']]}]});await pending;
    assert.deepEqual(parseOptions,{multipart:true});
    assert.doesNotMatch(flow.importScreen(),/Private Person|private-contact-file|Private contacts/);
    assert.equal(flow.details().church_name,'New account');
  }finally{for(const[key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
