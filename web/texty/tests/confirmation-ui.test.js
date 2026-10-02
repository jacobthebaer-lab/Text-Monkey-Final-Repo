import test from 'node:test';
import assert from 'node:assert/strict';
import { seed } from '../public/domain.js';

test('actual dashboard click submits exact review and setup shows a held message', async () => {
  const elements = new Map(['#app', '#modal', '#toast'].map(k => [k, {innerHTML:'',textContent:'',classList:{add(){},remove(){}},showModal(){},close(){}}]));
  const listeners = new Map();
  const saved = Object.fromEntries(['document','localStorage','location','history','fetch','setTimeout','setInterval'].map(k=>[k,globalThis[k]]));
  const state = seed();
  state.proposals = [{id:'900',phone:'+12025550188',intent:'confirm_text',summary:'Review exact message',reply:'Synthetic exact text.',reason:'Booking status requested by volunteer',expires_at:'2026-10-02T20:00:00Z',content_hash:'a'.repeat(64),confirmation_required:true,status:'pending',confidence:1}];
  state.escalations=[]; state.fills=[];
  const calls=[];
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.location={hash:'#access_token=synthetic-ui-token',pathname:'/texty'};
  globalThis.history={replaceState(){}};
  globalThis.setTimeout=(callback, delay)=>{const t=saved.setTimeout(callback,delay);t.unref();return t;};
  globalThis.setInterval=()=>0;
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});
    let payload;
    if(path==='/api/config')payload={name:'Texty',connected:true,provider:'gloo',humanConfirmationRequired:true};
    else if(path==='/api/state')payload=state;
    else if(path==='/api/proposals/900/approve'){state.proposals[0].status='approved';payload={reviewed:true};}
    else if(path.endsWith('/text-setup'))payload={approval_id:901};
    else throw Error('Unexpected request '+path);
    return {ok:true,json:async()=>payload};
  };
  try {
    await import('../public/app.js?confirmation-ui-fixture');
    const html=elements.get('#app').innerHTML;
    assert.match(html,/Human confirmation is active/);
    assert.match(html,/\+12025550188/);
    assert.match(html,/Synthetic exact text\./);
    assert.match(html,/Booking status requested by volunteer/);
    assert.match(html,/Approve exact text/);
    const button={dataset:{approve:'900'},hasAttribute:()=>false};
    await listeners.get('click')({target:{closest:()=>button}});
    const sent=calls.find(c=>c.path==='/api/proposals/900/approve');
    assert.deepEqual(JSON.parse(sent.options.body),{content_hash:'a'.repeat(64)});
    assert.equal(sent.options.headers.Authorization,'Bearer synthetic-ui-token');
    const setup={dataset:{textSetup:state.volunteers[0].id},hasAttribute:()=>false};
    await listeners.get('click')({target:{closest:()=>setup}});
    assert.equal(elements.get('#toast').textContent,'Setup text awaits your exact review.');
  } finally {
    for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}
  }
});
