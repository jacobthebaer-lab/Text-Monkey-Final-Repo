import test from 'node:test';
import assert from 'node:assert/strict';
import {createSetup} from '../public/setup.js';
import {createCoordinatorSession} from '../public/coordinator-session.js';

const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const preview={counts:{ready:1,duplicate:0,invalid:0},rows:[{name:'Synthetic contact',status:'ready',phone:'+12025550111'}],preview_hash:'a'.repeat(64)};
function fixture({completed=true}={}) {
  const saved=Object.fromEntries(['document','localStorage','FormData'].map(k=>[k,globalThis[k]]));
  const listeners=new Map(),calls=[],toasts=[];let epoch=1,hold=null,rendered=0,completions=0,completionWait=null;
  globalThis.document={querySelector:()=>null,addEventListener:(type,fn)=>listeners.set(type,fn)};
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.FormData=class {constructor(form){this.data=form.data;}[Symbol.iterator](){return Object.entries(this.data)[Symbol.iterator]();}};
  const api=async(path,body,options)=>{
    calls.push({path,body,options,epoch});
    if(hold?.matches(path,body,options))return hold.response.promise;
    if(path==='/api/setup/contacts')return {contacts:[{id:'same-id',name:`Account ${epoch} contact`,phone:'+12025550111'}]};
    if(path==='/api/setup/preview')return preview;
    if(path==='/api/setup/coordinator')return {coordinator_ready:true};
    if(path==='/api/setup/import')return {imported:1};
    if(options?.method==='DELETE')return {removed:true};
    return {details:{church_name:`Account ${epoch} church`,country:'US',timezone:'America/Denver'},completed:body?body.complete:completed,saved_at:'2026-10-06T17:00:00Z',revision:epoch};
  };
  const flow=createSetup({api,getMode:()=> 'live',getToken:()=>`synthetic-${epoch}`,getSessionEpoch:()=>epoch,render(){rendered++;},toast:value=>toasts.push(value),onComplete:async()=>{completions++;await completionWait?.promise;}});
  const click=dataset=>listeners.get('click')({target:{closest:()=>({dataset})},stopImmediatePropagation(){}});
  const submit=(id,data={},action='draft')=>listeners.get('submit')({target:{id,data},submitter:{value:action},preventDefault(){},stopImmediatePropagation(){}});
  return {flow,calls,toasts,click,submit,get rendered(){return rendered;},get completed(){return completions;},waitForCompletion(){completionWait=deferred();return completionWait;},
    hold(matches){const response=deferred();hold={matches,response};return response;},releaseHold(){hold=null;},
    async switchAccount(){epoch++;flow.clearSession();hold=null;await flow.load();},
    async prepareImport(){await click({setup:'sample'});await submit('contact-map-form',{name:'0',first_name:'',last_name:'',phone:'1',email:'2',ministry:'3',country:'US',source:'Synthetic list'});},
    restore(){for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}};
}
const operations={
  save:{matches:(path,body)=>path==='/api/setup' && !!body,start:f=>f.submit('church-setup-form'),result:{details:{church_name:'Private old church'},completed:true}},
  coordinator:{matches:path=>path==='/api/setup/coordinator',start:f=>f.click({setup:'coordinator'}),result:{coordinator_ready:true}},
  preview:{prepare:f=>f.click({setup:'sample'}),matches:path=>path==='/api/setup/preview',start:f=>f.submit('contact-map-form',{name:'0',phone:'1',country:'US',source:'Private old source'}),result:{...preview,rows:[{name:'Private old contact',status:'ready'}]}},
  import:{prepare:f=>f.prepareImport(),matches:path=>path==='/api/setup/import',start:f=>f.click({setup:'commit'}),result:{imported:9}},
  delete:{matches:(path,body,options)=>options?.method==='DELETE',start:f=>f.click({removeStaged:'same-id'}),result:{removed:true}},
};
for(const [name,operation]of Object.entries(operations))for(const fails of [false,true]) {
  test(`late ${name} ${fails?'failure':'success'} preserves the next account and its pending mutation`,async()=>{
    const f=fixture();
    try {
      await f.flow.load();await operation.prepare?.(f);
      const old=f.hold(operation.matches),pending=operation.start(f);
      assert.ok(f.calls.at(-1).epoch===1 && operation.matches(f.calls.at(-1).path,f.calls.at(-1).body,f.calls.at(-1).options));
      await f.switchAccount();await f.click({setupStep:'0'});
      const active=f.hold((path,body)=>path==='/api/setup' && !!body),newPending=f.submit('church-setup-form');
      const rendered=f.rendered,toasts=f.toasts.length;
      if(fails)old.reject(Error('Private old account detail'));else old.resolve(operation.result);
      await pending;
      assert.equal(f.flow.details().church_name,'Account 2 church');
      assert.match(f.flow.importScreen(),/Account 2 contact/);
      assert.doesNotMatch(f.flow.screen()+f.flow.importScreen(),/Private old|Coordinator ready|9 contacts/);
      assert.match(f.flow.screen(),/Saving…/);
      assert.equal(f.rendered,rendered);assert.equal(f.toasts.length,toasts);assert.equal(f.completed,0);
      const count=f.calls.length;await f.submit('church-setup-form');assert.equal(f.calls.length,count,'old finally must not unlock the new mutation');
      if(name==='import')assert.equal(f.calls.filter(c=>c.epoch===2 && c.path==='/api/setup/contacts').length,1,'no old import follow-up can read under the next account');
      active.resolve({details:{church_name:'Account 2 saved',country:'US'},completed:false,revision:3});await newPending;
      assert.equal(f.flow.details().church_name,'Account 2 saved');
    }finally{f.restore();}
  });
}
test('import follow-up contact reads cannot replace the next account after the write completed',async()=>{
  const f=fixture();
  try {
    await f.flow.load();await f.prepareImport();
    const old=f.hold(path=>path==='/api/setup/contacts'),pending=f.click({setup:'commit'});
    await Promise.resolve();await Promise.resolve();
    assert.equal(f.calls.at(-1).path,'/api/setup/contacts');
    await f.switchAccount();const before=f.toasts.length;
    old.resolve({contacts:[{id:'old',name:'Private old imported person'}]});await pending;
    assert.match(f.flow.importScreen(),/Account 2 contact/);assert.doesNotMatch(f.flow.importScreen(),/Private old imported/);
    assert.equal(f.toasts.length,before);
  }finally{f.restore();}
});

test('staged DELETE errors deferred during JSON parsing cannot render under the next account',async()=>{
  const f=fixture(),body=deferred(),calls=[];let parsing=false;
  const client=createCoordinatorSession({storage:()=>({setItem(){},removeItem(){}}),fetch:async(path,options)=>{
    calls.push({path,options});return {ok:false,status:400,json:()=>{parsing=true;return body.promise;}};
  }});
  client.set('synthetic-old-access');
  // Use the production session client for DELETE and retain fixture reads.
  const listeners=new Map();
  globalThis.document.addEventListener=(type,fn)=>listeners.set(type,fn);
  const flow=createSetup({api:(path,data,options)=>options?.method==='DELETE'?client.request(path,data,options):Promise.resolve(path==='/api/setup/contacts'?{contacts:[]}:{details:{church_name:client.getEpoch()===1?'Old church':'New church'},completed:true}),
    getMode:()=> 'live',getToken:()=> 'synthetic-access',getSessionEpoch:()=>client.getEpoch(),render(){},toast:value=>f.toasts.push(value)});
  try {
    await flow.load();const pending=listeners.get('click')({target:{closest:()=>({dataset:{removeStaged:'same-id'}})},stopImmediatePropagation(){}});
    await Promise.resolve();await Promise.resolve();assert.equal(parsing,true);
    client.set('synthetic-new-access');flow.clearSession();await flow.load();
    body.resolve({detail:'Private old account contact detail'});await pending;
    assert.equal(flow.details().church_name,'New church');assert.doesNotMatch(flow.importScreen(),/Private old/);
    assert.equal(f.toasts.length,0);assert.equal(calls.length,1);assert.equal(calls[0].options.method,'DELETE');
    assert.equal(calls[0].options.headers.Authorization,'Bearer synthetic-old-access');
  }finally{f.restore();}
});

for(const beforeCallback of [true,false])test(`old first-completion ${beforeCallback?'save':'callback'} cannot finish or unlock a later account`,async()=>{
  const f=fixture({completed:false});
  try {
    await f.flow.load();await f.click({setupStep:'2'});
    const deferredOld=beforeCallback?f.hold((path,body)=>path==='/api/setup' && !!body):f.waitForCompletion();
    const pending=f.submit('church-setup-form',{},'continue');
    if(!beforeCallback){for(let i=0;i<5 && !f.completed;i++)await Promise.resolve();assert.equal(f.completed,1);}
    await f.switchAccount();
    const active=f.hold((path,body)=>path==='/api/setup' && !!body),next=f.submit('church-setup-form');
    const rendered=f.rendered,toasts=f.toasts.length;
    deferredOld.resolve(beforeCallback?{details:{church_name:'Private old completed'},completed:true}:undefined);await pending;
    assert.equal(f.flow.details().church_name,'Account 2 church');assert.equal(f.flow.completed(),false);
    assert.equal(f.completed,beforeCallback?0:1);assert.equal(f.rendered,rendered);assert.equal(f.toasts.length,toasts);
    const count=f.calls.length;await f.submit('church-setup-form');assert.equal(f.calls.length,count);
    active.resolve({details:{church_name:'Account 2 saved'},completed:false});await next;
  }finally{f.restore();}
});
