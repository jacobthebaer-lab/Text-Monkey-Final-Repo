import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

test('actual admin composer holds exact roster text and never approves or sends; errors and opt-out/session guards remain', async () => {
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},showModal(){},close(){}}]));
  const listeners=new Map(), calls=[], state=seed(), storage=new Map();
  state.volunteers[0].id='1'; state.proposals=[];
  let backendError=null;
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'#access_token=synthetic-fixture-session',pathname:'/texty'};
  globalThis.history={replaceState(){globalThis.location.hash='';}};
  globalThis.setTimeout=()=>0; globalThis.setInterval=()=>0;
  globalThis.FormData=class {constructor(form){this.entries=form.data;} [Symbol.iterator](){return Object.entries(this.entries)[Symbol.iterator]();}};
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});
    let result;
    if(path==='/api/config')result={connected:true,humanConfirmationRequired:true,adminReplyAvailable:true,provider:'gloo'};
    else if(path==='/api/state')result=state;
    else if(path==='/api/setup')result={details:{church_name:'Synthetic church'},completed:true};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/reply'){
      if(backendError)return {ok:false,status:backendError.status,json:async()=>({detail:backendError.message})};
      const payload=JSON.parse(options.body);
      state.proposals=[{id:'900',phone:state.volunteers[0].phone,reply:payload.body,intent:'confirm_text',summary:'Exact text review',reason:'admin reply',expires_at:'2026-10-02T23:00:00Z',confirmation_required:true,content_hash:'a'.repeat(64),status:'pending'}];
      result={delivery:'awaiting_confirmation',approval_id:900};
    } else throw new Error('Unexpected approval/send/network request '+path);
    return {ok:true,json:async()=>result};
  };
  const error={textContent:''};
  const form={id:'admin-reply-form',data:{volunteer_id:'1',body:'  Synthetic exact words.\nSecond line. 🐒  '},querySelector:selector=>selector==='.error'?error:{disabled:false}};
  const submit=()=>listeners.get('submit')({preventDefault(){},target:form});
  try {
    await import('../public/app.js?admin-reply-fixture');
    await listeners.get('click')({target:{closest:()=>({dataset:{volunteer:'1'},hasAttribute:()=>false})}});
    assert.match(elements.get('#app').innerHTML,/Write a volunteer text/);
    assert.match(elements.get('#app').innerHTML,/Create text for review/);
    await submit();
    const request=calls.find(c=>c.path==='/api/reply');
    assert.deepEqual(JSON.parse(request.options.body),{volunteer_id:1,body:form.data.body});
    assert.equal(request.options.headers.Authorization,'Bearer synthetic-fixture-session');
    assert.match(elements.get('#app').innerHTML,/Text is held for exact review. Nothing has been sent/);
    assert.match(elements.get('#app').innerHTML,/Approve exact text/);
    assert.ok(!calls.some(c=>c.path.includes('/approve') || c.path.endsWith('/send')));
    const before=calls.filter(c=>c.path==='/api/reply').length;
    state.volunteers[0].consent=false; await submit();
    assert.match(error.textContent,/text consent/);
    assert.equal(calls.filter(c=>c.path==='/api/reply').length,before);
    state.volunteers[0].consent=true; form.data.body=' '; await submit();
    assert.match(error.textContent,/1–1,600/);
    assert.equal(calls.filter(c=>c.path==='/api/reply').length,before);
    form.data.body='Synthetic retained text'; backendError={status:409,message:'An active approved test session is required.'}; await submit();
    assert.equal(error.textContent,'An active approved session is required.');
    backendError={status:401,message:'Session expired. Sign in again.'}; await submit();
    assert.equal(storage.size,0);
    assert.match(elements.get('#app').innerHTML,/Welcome back/);
    const after=calls.filter(c=>c.path==='/api/reply').length; await submit();
    assert.equal(calls.filter(c=>c.path==='/api/reply').length,after);
  } finally {
    for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}
  }
});
