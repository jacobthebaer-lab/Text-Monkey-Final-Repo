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
    await f.click({volunteer:'v1'});
    assert.match(f.elements.get('#app').innerHTML,/Text history/);
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

test('live check button requires recipient readiness even when the laptop and Gloo are configured',async()=>{
  const f=fixture('#access_token=synthetic-token');let checkReady=false, focused=false, checked=false;
  const pendingId='11111111-1111-4111-8111-111111111111';
  f.elements.set('#admin-mobile',{focus(){focused=true;}});
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options});let data;
    if(path==='/api/config')data={connected:true,aiReady:true,macBridgeConnected:true,automationEnabled:false};
    else if(path==='/api/state')data=seed();
    else if(path==='/api/setup')data={details:{church_name:'Synthetic church',coordinator_phone:'+12025550199'},completed:true,revision:1};
    else if(path==='/api/setup/contacts')data={contacts:[]};
    else if(path==='/api/setup/admin-texts')data={enabled:true,ready:false,connection_check_ready:checkReady,phone:'+12025550199',checks:[
      {code:'session',label:'Messages session',ready:checkReady,detail:checkReady?'Active session.':'No Messages session is configured.',action:'connection-help',next_step:'Have the owner connect this exact mobile.'},
      {code:'scheduler',label:'Scheduled updates',ready:false,detail:'Automatic scheduling is paused.'}],recent:[],
      pending_check:checked?null:{request_id:pendingId,retry_at:'2026-10-03T16:02:00Z'}};
    else if(path==='/api/setup/admin-texts/send-check'){
      assert.equal(JSON.parse(options.body).request_id,pendingId);checked=true;
      data={delivery:'queued_for_mac',message_id:'synthetic-message'};
    }
    else throw Error('Unexpected request '+path);
    return{ok:true,json:async()=>data};
  };
  try{
    await import('../public/app.js?admin-recipient-readiness');await f.click({page:'settings'});
    assert.match(f.elements.get('#app').innerHTML,/data-action="send-admin-check"[^>]*disabled/);
    assert.match(f.elements.get('#app').innerHTML,/Have the owner connect this exact mobile/);
    await f.click({action:'focus-admin-mobile'});assert.ok(focused);
    checkReady=true;await f.click({action:'reload-admin-texts'});
    assert.match(f.elements.get('#app').innerHTML,/Ready for a one-time connection check/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/data-action="send-admin-check"[^>]*disabled/);
    assert.match(f.elements.get('#app').innerHTML,/Automatic scheduling is paused/);
    assert.ok(f.calls.every(c=>!c.options.body));
    assert.match(f.elements.get('#app').innerHTML,/Retry my connection check/);
    assert.match(f.elements.get('#app').innerHTML,/saved request is reused to prevent duplicates/);
    await f.click({action:'send-admin-check'});
    assert.ok(checked);
    assert.match(f.elements.get('#app').innerHTML,/Send me a connection check/);
    assert.equal(f.calls.filter(c=>c.options.body).length,1);
  }finally{f.restore();}
});

test('existing-contact replacement reviews exact target and sends operator attestation without first-person consent',async()=>{
  const f=fixture('#access_token=synthetic-token');
  const proof={review_id:'admin-recipient-review:synthetic',record_hash:'a'.repeat(64),primary_hash:'b'.repeat(64),
    recipient:{id:42,name:'Casey Contact',phone:'+12025550198'},replacing:[{name:'Old Primary',phone:'+12025550199'}]};
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options});let data;
    if(path==='/api/config')data={connected:true,aiReady:true};
    else if(path==='/api/state')data=seed();
    else if(path==='/api/setup')data={details:{church_name:'Synthetic church'},completed:true,revision:1};
    else if(path==='/api/setup/contacts')data={contacts:[]};
    else if(path==='/api/setup/admin-texts/review'){
      assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{phone:'2025550198'});data=proof;
    }else if(path==='/api/setup/admin-texts'){
      if(options.body)assert.deepEqual(JSON.parse(options.body),{phone:proof.recipient.phone,enabled:true,consent:false,
        operator_consent:true,review_id:proof.review_id,record_hash:proof.record_hash,primary_hash:proof.primary_hash});
      data={enabled:!!options.body,phone:proof.recipient.phone,recent:[],ready:false,
        recipient_name:'Casey Contact',consent_mode:options.body?'operator_attested':null};
    }else throw Error('Unexpected request '+path);
    return{ok:true,json:async()=>data};
  };
  try{
    await import('../public/app.js?review-existing-admin');await f.click({page:'settings'});
    await f.submit({phone:'2025550198'},'review','admin-recipient-review-form');
    const html=f.elements.get('#app').innerHTML;
    assert.match(html,/Use Casey Contact as the primary admin recipient/);
    assert.match(html,/Replacing: Old Primary/);
    assert.match(html,/this person agreed to receive church admin text updates/);
    assert.match(html,/name="operator_consent" required/);
    assert.doesNotMatch(html,/name="operator_consent"[^>]*checked/);
    assert.ok(await f.submit({},'confirm','admin-recipient-claim-form'));
    assert.equal(f.calls.filter(c=>c.options.body).length,1);
    assert.equal(await f.submit({operator_consent:'on'},'confirm','admin-recipient-claim-form'),'');
    assert.equal(f.calls.filter(c=>c.options.body).length,2);
    assert.ok(f.calls.every(c=>!c.path.includes('send-check')&&!c.path.includes('signup')));
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/id="admin-recipient-claim-form"/);
    assert.match(f.elements.get('#app').innerHTML,/Current primary: Casey Contact/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/name="consent"[^>]*checked/);
    assert.match(f.elements.get('#app').innerHTML,/Send the primary recipient a connection check/);
  }finally{f.restore();}
});
