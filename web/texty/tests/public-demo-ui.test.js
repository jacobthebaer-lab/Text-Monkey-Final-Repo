import test from 'node:test';
import assert from 'node:assert/strict';

test('public demo opens immediately and completes synthetic text signup without any account or backend calls',async()=>{
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]])), listeners=new Map(), storage=new Map(), calls=[];
  const elements=new Map(['#app','#modal','#toast','#sim-error','#sim-body','#sim-phone'].map(k=>[k,{innerHTML:'',textContent:'',value:'',classList:{add(){},remove(){}},showModal(){},close(){}}]));
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,fn)=>listeners.set(event,fn)};
  globalThis.localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.sessionStorage={getItem:()=>null,setItem(){throw Error('No public account session');},removeItem(){}};
  globalThis.location={hash:'',pathname:'/'};globalThis.history={replaceState(){}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.FormData=class {constructor(f){this.data=f.data;}[Symbol.iterator](){return Object.entries(this.data)[Symbol.iterator]();}};
  globalThis.fetch=async path=>{
    calls.push(path);assert.equal(path,'/api/config');
    return {ok:true,json:async()=>({publicDemo:true,connected:false,liveSms:false,aiReady:false,provider:'sample rules'})};
  };
  const click=async dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}});
  const form={id:'simulate-form',data:{phone:'+12025550199',body:'JOIN TEST Volunteer'},querySelector:()=>({disabled:false})};
  try {
    await import('../public/app.js?public-demo-ui');
    assert.match(elements.get('#app').innerHTML,/Open the demo/);
    assert.match(elements.get('#app').innerHTML,/No real texts are sent/);
    assert.doesNotMatch(elements.get('#app').innerHTML,/id="login-form"|type="password"/);
    await click({action:'demo'});
    let html=elements.get('#app').innerHTML;
    assert.match(html,/data-page="volunteers" aria-current="page"/);
    assert.match(html,/Synthetic preview/);
    assert.doesNotMatch(html,/Finish your account|Text Lab/);
    await click({page:'messages'});
    for(const body of ['JOIN TEST Volunteer','YES']) {
      form.data.body=body;
      await listeners.get('submit')({preventDefault(){},target:form});
    }
    await click({page:'volunteers'});
    assert.match(elements.get('#app').innerHTML,/TEST Volunteer/);
    assert.match(elements.get('#app').innerHTML,/Text consent recorded/);
    await click({page:'schedule'});
    assert.match(elements.get('#app').innerHTML,/Try a sample booking/);
    await click({page:'settings'});
    assert.match(elements.get('#app').innerHTML,/No background scheduling or real delivery/);
    assert.deepEqual(calls,['/api/config']);
    assert.ok([...storage.values()].some(value=>value.includes('TEST') && value.includes('Volunteer')));
  } finally {for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});
