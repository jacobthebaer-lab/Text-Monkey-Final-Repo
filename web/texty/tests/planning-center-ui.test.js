import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

test('actual Settings control uses roster ID and session auth; only explicit load and exact record call Planning Center',async()=>{
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval'];
  const saved=Object.fromEntries(keys.map(key=>[key,globalThis[key]])),elements=new Map(['#app','#modal','#toast'].map(key=>[key,{innerHTML:'',textContent:'',classList:{add(){},remove(){}}}]));
  const listeners=new Map(),calls=[],state=seed(),intent='a'.repeat(64),exact={preview_hash:'b'.repeat(64),source_hash:'c'.repeat(64),remote_hash:'d'.repeat(64),operation_hash:'e'.repeat(64)};
  state.volunteers[0].id='7';state.escalations=[];state.proposals=[];state.fills=[];
  const operation={kind:'membership_frequency',logical_key:'role:1',method:'PATCH',state:'held',intent_key:intent,body:{data:{type:'PersonTeamPositionAssignment',attributes:{schedule_preference:'Twice a month'}}}};
  globalThis.document={querySelector:key=>elements.get(key),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};globalThis.sessionStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.location={hash:'#access_token=synthetic-admin-session',pathname:'/texty'};globalThis.history={replaceState(){}};globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});let data;
    if(path==='/api/config')data={connected:true,aiReady:false,macBridgeConnected:false};
    else if(path==='/api/state')data=state;
    else if(path==='/api/setup')data={details:{church_name:'Synthetic church'},completed:true,revision:1};
    else if(path==='/api/setup/contacts')data={contacts:[]};
    else if(path==='/api/setup/admin-texts')data={enabled:false,issues:[],recent:[]};
    else if(path==='/api/planning-center/held-previews'){
      assert.deepEqual(JSON.parse(options.body),{volunteer_id:7});
      data={...exact,operations:[operation],holds:[],release_holds:['native_notification_silence_unverified'],execution_enabled:false};
    }else if(path===`/api/planning-center/frequency-reviews/${intent}`){
      data=options.body?{receipt_id:'synthetic-receipt',receipt_hash:'f'.repeat(64),state:'reviewed_held',expires_at:'2026-10-03T18:10:00Z',release_holds:['fresh_native_preflight_required'],execution_enabled:false}
        :{...exact,intent_key:intent,membership:{role_name:'Greeter'},operation,native_snapshot:{schedule_preference:'Every week',saved_at:'2026-10-03T18:00:00Z'},release_holds:['native_notification_silence_unverified'],execution_enabled:false};
      if(options.body)assert.deepEqual(JSON.parse(options.body),exact);
    }else throw Error('Unexpected request '+path);
    return{ok:true,json:async()=>data};
  };
  const click=(dataset={},attributes=[])=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:key=>attributes.includes(key)})}});
  try{
    await import('../public/app.js?planning-center-held-ui');await click({page:'settings'});
    assert.match(elements.get('#app').innerHTML,/Planning Center review/);
    assert.equal(calls.filter(call=>call.path.startsWith('/api/planning-center')).length,0);
    await listeners.get('change')({target:{id:'pco-review-volunteer',value:'7'}});
    await click({},['data-pco-load']);
    assert.match(elements.get('#app').innerHTML,/Every week/);assert.match(elements.get('#app').innerHTML,/Twice a month/);
    assert.match(elements.get('#app').innerHTML,/Record review/);
    await click({pcoRecord:intent});assert.match(elements.get('#app').innerHTML,/Review recorded, still held/);
    const nativeCalls=calls.filter(call=>call.path.startsWith('/api/planning-center'));
    assert.equal(nativeCalls.length,3);assert.ok(nativeCalls.every(call=>call.options.headers.Authorization==='Bearer synthetic-admin-session'));
    assert.equal(nativeCalls.filter(call=>call.options.method==='POST').length,2);
    assert.ok(!calls.some(call=>call.path.includes('/execute')||call.path.includes('/api/reply')));
    await click({action:'demo'});await click({page:'settings'});
    assert.match(elements.get('#app').innerHTML,/preview is disconnected/);assert.doesNotMatch(elements.get('#app').innerHTML,/data-pco-load|data-pco-record/);
  }finally{for(const[key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
