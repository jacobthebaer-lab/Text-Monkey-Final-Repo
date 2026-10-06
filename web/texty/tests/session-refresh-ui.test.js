import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

test('actual callback, expiry rotation, transient reload, retry and explicit offline signout',async()=>{
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval'];
  const originals=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},showModal(){},close(){}}]));
  const storage=new Map(),listeners=new Map(),calls=[];let poll,expired=false,outage=false,logoutOutage=false;
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,fn)=>listeners.set(event,fn)};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'#access_token=synthetic-old&refresh_token=synthetic-parent&expires_in=3600',pathname:'/texty'};
  globalThis.history={replaceState(){location.hash='';}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=fn=>{poll=fn;return 0;};
  const response=(status,result)=>({ok:status===200,status,json:async()=>result});
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});
    if(path==='/api/config')return response(200,{connected:true});
    if(path==='/api/session/refresh'){
      assert.deepEqual(JSON.parse(options.body),{refresh_token:'synthetic-parent'});expired=false;
      return response(200,{access_token:'synthetic-new',refresh_token:'synthetic-rotated',expires_in:3600});
    }
    if(path==='/api/state')return response(outage?503:expired?401:200,outage?{detail:'Temporary Supabase outage.'}:expired?{detail:'expired'}:seed());
    if(path==='/api/setup')return response(200,{details:{church_name:'Synthetic church'},completed:true});
    if(path==='/api/setup/contacts')return response(200,{contacts:[]});
    if(path==='/api/setup/admin-texts')return response(200,{enabled:false,phone:'',issues:[]});
    if(path==='/api/logout')return response(logoutOutage?503:200,{});
    throw new Error('Unexpected synthetic route '+path);
  };
  try {
    await import('../public/app.js?session-refresh-confirmed');
    assert.equal(JSON.parse([...storage.values()][0]).refresh_token,'synthetic-parent');
    assert.match(elements.get('#app').innerHTML,/Signed-in admin/);
    expired=true;await poll();
    assert.equal(calls.filter(c=>c.path==='/api/session/refresh').length,1);
    assert.equal(JSON.parse([...storage.values()][0]).refresh_token,'synthetic-rotated');
    assert.match(elements.get('#app').innerHTML,/Signed-in admin/);
    outage=true;await import('../public/app.js?session-refresh-transient-reload');
    assert.equal(storage.size,1);
    assert.match(elements.get('#app').innerHTML,/Workspace temporarily unavailable/);
    assert.doesNotMatch(elements.get('#app').innerHTML,/Welcome back|Synthetic church/);
    outage=false;await listeners.get('click')({target:{closest:()=>({dataset:{action:'retry-workspace'}})}});
    assert.match(elements.get('#app').innerHTML,/Signed-in admin/);
    logoutOutage=true;await listeners.get('click')({target:{closest:()=>({dataset:{action:'logout'}})}});
    assert.equal(storage.size,0);assert.match(elements.get('#app').innerHTML,/Welcome back/);
  } finally {for(const[k,v]of Object.entries(originals)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});
