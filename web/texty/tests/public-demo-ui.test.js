import test from 'node:test';
import assert from 'node:assert/strict';

test('public dashboard opens immediately without a text simulator or backend writes',async()=>{
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
  let focused='';
  elements.set('#main-content',{focus(){focused='main-content';}});
  const click=async dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}});
  const form={id:'simulate-form',data:{phone:'+12025550199',body:'JOIN TEST Volunteer'},querySelector:()=>({disabled:false})};
  try {
    await import('../public/app.js?public-demo-ui');
    assert.match(elements.get('#app').innerHTML,/Open the demo/);
    assert.match(elements.get('#app').innerHTML,/No real texts are sent/);
    assert.doesNotMatch(elements.get('#app').innerHTML,/id="login-form"|type="password"/);
    await click({action:'demo'});
    let html=elements.get('#app').innerHTML;
    assert.equal(focused,'main-content','Entering the preview restores a useful keyboard position');
    assert.match(html,/data-page="overview" aria-current="page"/);
    assert.match(html,/Texting disconnected/);
    assert.doesNotMatch(html,/Finish your account|Text Lab/);
    await click({volunteer:'v1'});
    assert.match(elements.get('#app').innerHTML,/Text history/);
    assert.doesNotMatch(elements.get('#app').innerHTML,/simulate-form|data-sample|Try an incoming text/);
    await click({page:'volunteers'});
    assert.match(elements.get('#app').innerHTML,/tabindex="0" role="region" aria-label="Volunteer roster, scroll horizontally"/);
    await click({page:'schedule'});
    assert.match(elements.get('#app').innerHTML,/tabindex="0" role="region" aria-label="Shift schedule, scroll horizontally"/);
    assert.match(elements.get('#app').innerHTML,/Try a sample booking/);
    await click({page:'settings'});
    assert.match(elements.get('#app').innerHTML,/no texting connection/);
    assert.deepEqual(calls,['/api/config']);
    assert.ok(![...storage.values()].some(value=>value.includes('TEST Volunteer')));
  } finally {for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});
