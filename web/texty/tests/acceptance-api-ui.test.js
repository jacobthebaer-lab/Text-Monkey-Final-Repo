import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

test('actual Schedule sends plain scope fields through the shared JSON API helper',async()=>{
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval'];
  const originals=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},showModal(){},close(){}}]));
  const listeners=new Map(),storage=new Map(),calls=[];
  const snapshot={participant:'Synthetic Tester',zone:'America/Denver',roles:[],event:{id:7,title:'Demo: Synthetic',starts_at:'2026-10-02T16:00:00Z'},assignment_id:9,reviews:[],message:null,timer:{enabled:false}};
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,fn)=>listeners.set(event,fn)};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'#access_token=synthetic-admin',pathname:'/texty'};
  globalThis.history={replaceState(){location.hash='';}};
  globalThis.setTimeout=()=>0;globalThis.setInterval=()=>0;
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});let result;
    if(path==='/api/config')result={connected:true,acceptanceEventAvailable:true};
    else if(path==='/api/state')result=seed();
    else if(path==='/api/setup')result={details:{church_name:'Synthetic church'},completed:true};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/setup/admin-texts')result={enabled:false,phone:'',issues:[]};
    else if(path==='/api/planning/availability-collections')result={collections:[]};
    else if(path.startsWith('/api/notification-status'))result={notifications:[]};
    else if(path==='/api/acceptance-event' || path==='/api/acceptance-event/prepare')result=snapshot;
    else throw new Error('Unexpected synthetic route '+path);
    return {ok:true,json:async()=>result};
  };
  try {
    await import('../public/app.js?acceptance-shared-api');
    const click=dataset=>listeners.get('click')({target:{closest:()=>({dataset})}});
    await click({page:'schedule'});
    assert.match(elements.get('#app').innerHTML,/Private event test/);
    await click({acceptanceAction:'prepare'});
    const request=calls.find(c=>c.path==='/api/acceptance-event/prepare');
    assert.equal(request.options.method,'POST');
    assert.equal(request.options.headers.Authorization,'Bearer synthetic-admin');
    assert.deepEqual(JSON.parse(request.options.body),{event_id:7,assignment_id:9});
    assert.ok(!calls.some(c=>c.path.endsWith('/dispatch') || c.path.endsWith('/approve')));
  } finally {for(const[k,v]of Object.entries(originals)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});
