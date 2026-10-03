import test from 'node:test';
import assert from 'node:assert/strict';
import {createSetup} from '../public/setup.js';

// Exercise the production controller's delegated actions with synthetic APIs.
test('actual setup controller saves, resumes, previews and stages without scheduling calls',async()=>{
  const saved=Object.fromEntries(['document','localStorage','FormData'].map(k=>[k,globalThis[k]]));
  const listeners=new Map(), calls=[], messages=[]; let currentForm=null, mode='live', html='';
  let stored={details:{country:'US',timezone:'America/Denver',quiet_start:'21:00',quiet_end:'07:00',monthly_ask_limit:4},revision:0,completed:false};let contacts=[];
  const document={querySelector:selector=>selector==='#church-setup-form'?currentForm:null,addEventListener:(type,fn)=>{if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(fn);}};
  globalThis.document=document;
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.FormData=class {constructor(form){this.data=form.data;}[Symbol.iterator](){return Object.entries(this.data)[Symbol.iterator]();}};
  const api=async(path,body)=>{
    calls.push({path,body});
    if(path==='/api/setup' && !body)return stored;
    if(path==='/api/setup'){stored={details:body.details,revision:stored.revision+1,completed:body.complete,saved_at:'2026-10-02T10:00:00Z'};return stored;}
    if(path==='/api/setup/contacts')return{contacts};
    if(path==='/api/setup/preview')return{counts:{ready:1,duplicate:0,invalid:0},rows:[{row:2,name:'Alex Sample',phone:'+12025550111',status:'ready',reason:'Awaiting consent'}],preview_hash:'a'.repeat(64)};
    if(path==='/api/setup/import'){contacts=[{id:'synthetic',name:'Alex Sample',phone:'+12025550111',source:body.source,can_text:false}];return{imported:1,texts_sent:0};}
    throw new Error('Unexpected API request '+path);
  };
  let controller;
  const click=async dataset=>{const event={target:{closest:()=>({dataset})},stopImmediatePropagation(){}};for(const fn of listeners.get('click')||[])await fn(event);};
  const submit=async(form,action='continue')=>{for(const fn of listeners.get('submit')||[])await fn({target:form,submitter:{value:action},preventDefault(){},stopImmediatePropagation(){}});};
  try {
    controller=createSetup({api,getMode:()=>mode,getToken:()=> 'synthetic-token',toast:m=>messages.push(m),render:()=>{html=controller.screen();}});
    await controller.load();assert.match(controller.screen(),/Tell us about your church/);
    currentForm={id:'church-setup-form',data:{church_name:'Example Church',affiliation:'Independent',address:'100 Example Way',city:'Example City',region:'CO',postal_code:'80000',coordinator_name:'Alex Sample',coordinator_role:'Coordinator'}};
    await submit(currentForm);assert.equal(stored.details.church_name,'Example Church');assert.match(html,/Make it fit your ministry/);
    await controller.load();assert.equal(controller.details().church_name,'Example Church');
    currentForm=null;
    await click({setup:'sample'});
    await submit({id:'contact-map-form',data:{name:'0',first_name:'',last_name:'',phone:'1',email:'2',ministry:'3',country:'US',source:'Synthetic list'}});
    assert.match(controller.importScreen(),/Save 1 staged contact/);
    await click({setup:'commit'});
    assert.match(controller.importScreen(),/Awaiting consent/);
    const request=calls.find(c=>c.path==='/api/setup/import');assert.equal(request.body.preview_hash,'a'.repeat(64));assert.ok(request.body.submission_id);
    assert.ok(calls.every(c=>c.path.startsWith('/api/setup')));assert.equal(messages.at(-1),'1 contacts staged. No texts sent.');
    mode='demo';await controller.load();assert.doesNotMatch(controller.importScreen(),/Alex Sample/);
  } finally {for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});
