import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

function fixture(hash='') {
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},open:false}]));
  const listeners=new Map(), calls=[], storage=new Map();
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.location={hash,pathname:'/texty'}; globalThis.history={replaceState(){}};
  globalThis.setTimeout=()=>0; globalThis.setInterval=()=>0;
  globalThis.FormData=class {constructor(form){this.data=form.data;}[Symbol.iterator](){return Object.entries(this.data)[Symbol.iterator]();}};
  return {elements,calls,storage,listeners,click:async dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}}),
    submit:async(data,action='enable',formId='admin-text-form')=>{const error={textContent:''};const button={disabled:false};await listeners.get('submit')({preventDefault(){},submitter:{value:action},target:{id:formId,data,querySelector:s=>s==='.error'?error:button}});return error.textContent;},
    restore(){for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}};
}

test('offline console has no text simulator, sample-send control or simulated pause switch',async()=>{
  const f=fixture();
  globalThis.fetch=async(path,options)=>{f.calls.push({path,options});assert.equal(path,'/api/config');return{ok:true,json:async()=>({publicDemo:true,connected:false})};};
  try {
    await import('../public/app.js?admin-no-simulator');await f.click({action:'demo'});
    assert.match(f.elements.get('#app').innerHTML,/What needs me\?/);
    await f.click({page:'settings'});
    assert.match(f.elements.get('#app').innerHTML,/no texting connection/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/Preview admin update|Sample admin mobile|Pause demo texting|Resume demo texting/);
    await f.click({page:'messages'});
    assert.match(f.elements.get('#app').innerHTML,/Conversation history/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/simulate-form|sim-phone|Try an incoming text|Process test message|data-sample/);
    assert.ok(f.calls.every(c=>!c.options.body));
  } finally {f.restore();}
});

test('live admin setting saves explicit consent, shows blockers and can pause without consent',async()=>{
  const f=fixture('#access_token=synthetic-token');let enabled=false;
  const details={church_name:'Synthetic church',coordinator_phone:'+12025550199',timezone:'America/Denver'};
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options});let data;
    if(path==='/api/config')data={connected:true,aiReady:true,macBridgeConnected:false,automationEnabled:false};
    else if(path==='/api/state')data=seed();
    else if(path==='/api/setup')data={details,completed:true,revision:1};
    else if(path==='/api/setup/contacts')data={contacts:[]};
    else if(path==='/api/setup/admin-texts'){
      if(options.body){const body=JSON.parse(options.body);if(body.enabled)assert.equal(body.consent,true);enabled=body.enabled;}
      data={enabled,phone:'+12025550199',ready:false,issues:['The laptop Messages connection is offline.'],recent:[],pre_event_hours:3};
    } else throw Error('Unexpected request '+path);
    return{ok:true,json:async()=>data};
  };
  try {
    await import('../public/app.js?admin-text-connected');await f.click({page:'settings'});
    assert.match(f.elements.get('#app').innerHTML,/Your mobile number/);
    await f.submit({phone:'(202) 555-0199',consent:'on'});
    assert.match(f.elements.get('#app').innerHTML,/Pause my updates/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/Saving…/);
    assert.match(f.elements.get('#app').innerHTML,/offline/);
    await f.submit({phone:'',consent:''},'pause');assert.equal(enabled,false);
    assert.ok(f.calls.filter(c=>c.options.body).every(c=>c.path==='/api/setup/admin-texts'));
  } finally {f.restore();}
});
