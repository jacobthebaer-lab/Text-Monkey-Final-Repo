import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

test('signed-in Mac website starts signup, retries one request and preserves auth/transport holds', async () => {
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}}}]));
  const listeners=new Map(), calls=[], state=seed(), storage=new Map();
  state.volunteers=[]; state.proposals=[];
  let failure=503;
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'#access_token=synthetic-invitation-session',pathname:'/texty'};
  globalThis.history={replaceState(){globalThis.location.hash='';}};
  globalThis.setTimeout=()=>0; globalThis.setInterval=()=>0;
  globalThis.FormData=class {constructor(form){this.entries=form.data;} [Symbol.iterator](){return Object.entries(this.entries)[Symbol.iterator]();}};
  const config={connected:true,aiReady:true,messagingTransport:'mac_messages',humanConfirmationRequired:false};
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options}); let result;
    if(path==='/api/config')result=config;
    else if(path==='/api/state')result=state;
    else if(path==='/api/setup')result={details:{church_name:'Synthetic church'},completed:true};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/signup-invitations'){
      if(failure)return {ok:false,status:failure,json:async()=>({detail:failure===401?'Session expired. Sign in again.':'Gloo temporarily unavailable. Nothing sent.'})};
      result={delivery:'queued_for_mac',message_id:11,phone:'+12025550199',body:'Synthetic Gloo invitation'};
    } else throw new Error('Unexpected request '+path);
    return {ok:true,json:async()=>result};
  };
  const error={textContent:''};
  const form={id:'signup-invitation-form',data:{name:'Synthetic Recipient',phone:'+12025550199'},querySelector:s=>s==='.error'?error:{disabled:false}};
  const submit=()=>listeners.get('submit')({preventDefault(){},target:form});
  const click=dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}});
  try {
    await import('../public/app.js?signup-invitation-fixture');
    await click({page:'volunteers'});
    assert.match(elements.get('#app').innerHTML,/id="signup-invitation-form"/);
    await submit();
    assert.match(error.textContent,/Gloo temporarily unavailable/);
    failure=0; await submit();
    const requests=calls.filter(c=>c.path==='/api/signup-invitations');
    assert.equal(requests.length,2);
    const first=JSON.parse(requests[0].options.body), second=JSON.parse(requests[1].options.body);
    assert.deepEqual(first,second);
    assert.equal(first.phone,form.data.phone); assert.equal(first.name,form.data.name);
    assert.match(first.request_id,/^[0-9a-f-]{36}$/);
    assert.equal(requests[0].options.headers.Authorization,'Bearer synthetic-invitation-session');
    assert.match(elements.get('#app').innerHTML,/Signup invitation queued/);
    assert.ok(!calls.some(c=>c.path==='/api/volunteers'||c.path.endsWith('/send')||c.path.includes('/approve')));
    const count=requests.length;
    config.messagingTransport='google_voice'; await submit();
    assert.equal(calls.filter(c=>c.path==='/api/signup-invitations').length,count);
    config.messagingTransport='mac_messages'; config.aiReady=false; await submit();
    assert.equal(calls.filter(c=>c.path==='/api/signup-invitations').length,count);
    config.aiReady=true; failure=401; await submit();
    assert.equal(storage.size,0); assert.match(elements.get('#app').innerHTML,/Welcome back/);
    const finalCount=calls.filter(c=>c.path==='/api/signup-invitations').length;
    await submit(); assert.equal(calls.filter(c=>c.path==='/api/signup-invitations').length,finalCount);
  } finally {
    for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}
  }
});
