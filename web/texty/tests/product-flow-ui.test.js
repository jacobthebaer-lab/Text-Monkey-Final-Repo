import test from 'node:test';
import assert from 'node:assert/strict';
import {seed} from '../public/domain.js';

function fixture() {
  const keys=['document','localStorage','sessionStorage','location','history','fetch','setTimeout','setInterval','FormData'];
  const saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  const elements=new Map(['#app','#modal','#toast','#login-error'].map(k=>[k,{innerHTML:'',textContent:'',classList:{add(){},remove(){}},showModal(){},close(){}}]));
  const listeners=new Map(), calls=[], storage=new Map();
  globalThis.document={querySelector:k=>elements.get(k),addEventListener:(event,callback)=>listeners.set(event,callback)};
  globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
  globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  globalThis.location={hash:'',pathname:'/texty'};
  globalThis.history={replaceState(){globalThis.location.hash='';}};
  globalThis.setTimeout=()=>0; globalThis.setInterval=callback=>{listeners.set('poll',callback);return 0;};
  globalThis.FormData=class {constructor(form){this.entries=form.data;} [Symbol.iterator](){return Object.entries(this.entries)[Symbol.iterator]();}};
  return {elements,calls,storage,listeners,click:async dataset=>listeners.get('click')({target:{closest:()=>({dataset,hasAttribute:()=>false})}}),
    submit:async form=>listeners.get('submit')({preventDefault(){},target:form}),
    restore(){for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}};
}

test('registration includes church details; confirmed first login creates workspace and returning login opens Home', async()=>{
  const f=fixture(), state=seed(); let completed=false, available=true;
  const details={church_name:'Text Monkey TEST Church',address:'100 TEST Example Way',city:'Testville',region:'CO',postal_code:'00000',country:'US',timezone:'America/Denver',coordinator_name:'TEST Coordinator',coordinator_role:'Coordinator',coordinator_phone:'+12025550199'};
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options}); let result;
    if(path==='/api/config')result={connected:true,adminReplyAvailable:true,humanConfirmationRequired:false};
    else if(path==='/api/register')result={message:'Check your email to confirm your administrator account, then sign in.'};
    else if(path==='/api/setup')result={details:completed?details:{},completed,revision:completed?1:0,account_setup_available:available};
    else if(path==='/api/setup/from-account'){assert.equal(options.headers.Authorization,'Bearer synthetic-confirmed-session');assert.deepEqual(JSON.parse(options.body),{});completed=true;available=false;result={details,completed:true,revision:1};}
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/state')result=state;
    else throw Error('Unexpected request '+path);
    return {ok:true,json:async()=>result};
  };
  try {
    await import('../public/app.js?product-register');
    await f.click({auth:'register'});
    assert.match(f.elements.get('#app').innerHTML,/name="church_name"/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/affiliation|denomination|Preview with synthetic data/i);
    const form={id:'login-form',data:{email:'coordinator@example.test',password:'synthetic-password-only',confirm_password:'synthetic-password-only',...details},querySelector:()=>({disabled:false}),reset(){}};
    await f.submit(form);
    const payload=JSON.parse(f.calls.find(c=>c.path==='/api/register').options.body);
    assert.equal(payload.church_details.church_name,details.church_name);
    assert.equal(payload.church_details.affiliation,undefined);
    assert.equal(payload.church_details.password,undefined);
    assert.equal(payload.confirm_password,undefined);
    assert.equal(f.storage.size,0);
    assert.ok(!f.calls.some(c=>c.path.startsWith('/api/setup')));
    assert.match(f.elements.get('#login-error').textContent,/confirm/);
    globalThis.location.hash='#access_token=synthetic-confirmed-session';
    await import('../public/app.js?product-first-login');
    assert.equal(f.calls.filter(c=>c.path==='/api/setup/from-account').length,1);
    assert.match(f.elements.get('#app').innerHTML,/data-page="overview" aria-current="page"/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/Finish your account|Text Lab|Human confirmation is active/);
    await import('../public/app.js?product-return-login');
    assert.equal(f.calls.filter(c=>c.path==='/api/setup/from-account').length,1);
    assert.match(f.elements.get('#app').innerHTML,/data-page="overview" aria-current="page"/);
  } finally {f.restore();}
});

test('slow login keeps the form stable, blocks duplicate submits and polling, and opens Home before optional checks', async()=>{
  const f=fixture(), state=seed();
  const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
  const login=deferred(), setup=deferred(), status=deferred();
  const setupStarted=deferred(), statusStarted=deferred();
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options}); let result;
    if(path==='/api/config') result={connected:true,cloudTextingAvailable:true};
    else if(path==='/api/login') result=await login.promise;
    else if(path==='/api/state') result=state;
    else if(path==='/api/setup') {setupStarted.resolve();result=await setup.promise;}
    else if(path==='/api/setup/contacts') result={contacts:[]};
    else if(path==='/api/setup/admin-texts') {statusStarted.resolve();result=await status.promise;}
    else if(path==='/api/auth/me') result={superadmin:false};
    else throw Error('Unexpected request '+path);
    return {ok:true,status:200,json:async()=>result};
  };
  const button={disabled:false,textContent:'Sign in'}, error={textContent:'Old error'};
  let resets=0;
  const form={id:'login-form',data:{email:'coordinator@example.test',password:'synthetic-password-only'},querySelector:s=>s==='.error'?error:button,reset(){resets++;}};
  try {
    await import('../public/app.js?product-slow-login');
    const submitting=f.submit(form);
    assert.equal(button.textContent,'Signing in…');
    assert.equal(error.textContent,'');
    await f.submit(form);
    await f.click({auth:'register'});
    assert.equal(f.calls.filter(c=>c.path==='/api/login').length,1);
    login.resolve({access_token:'synthetic-slow-login'});
    await setupStarted.promise;
    assert.equal(button.textContent,'Opening workspace…');
    assert.equal(resets,0);
    await f.listeners.get('poll')();
    assert.equal(f.calls.filter(c=>c.path==='/api/state').length,1);
    assert.match(f.elements.get('#app').innerHTML,/id="login-form"/);
    setup.resolve({details:{church_name:'TEST Church'},completed:true,revision:1});
    await statusStarted.promise;
    assert.match(f.elements.get('#app').innerHTML,/data-page="overview" aria-current="page"/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/id="login-form"/);
    status.resolve({enabled:false});
    await submitting;
    assert.equal(resets,1);
    assert.equal(f.storage.size,1);
  } finally {f.restore();}
});

test('rejected login keeps entered credentials and shows an inline error with a retryable button',async()=>{
  const f=fixture();
  globalThis.fetch=async path=>path==='/api/config'
    ? {ok:true,json:async()=>({connected:true})}
    : {ok:false,status:401,json:async()=>({detail:'Unable to sign in. Check your email and password.'})};
  const button={disabled:false,textContent:'Sign in'}, error={textContent:''};let resets=0;
  const form={id:'login-form',data:{email:'coordinator@example.test',password:'synthetic-invalid-password'},querySelector:s=>s==='.error'?error:button,reset(){resets++;}};
  try {
    await import('../public/app.js?product-rejected-login');
    await f.submit(form);
    assert.equal(resets,0);
    assert.equal(button.disabled,false);
    assert.equal(button.textContent,'Sign in');
    assert.match(error.textContent,/Check your email and password/);
    assert.equal(f.storage.size,0);
    assert.match(f.elements.get('#app').innerHTML,/id="login-form"/);
  } finally {f.restore();}
});

test('normal Messages retries the same request ID, clears after queuing, and exposes no simulation controls', async()=>{
  const f=fixture(), state=seed(); state.volunteers[0].id='1';state.proposals=[];
  globalThis.location.hash='#access_token=synthetic-confirmed-session';
  let fail=true;
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options});let result;
    if(path==='/api/config')result={connected:true,adminReplyAvailable:true,humanConfirmationRequired:false};
    else if(path==='/api/setup')result={details:{church_name:'TEST Church'},completed:true,revision:1};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/state')result=state;
    else if(path==='/api/reply'){
      if(fail)return {ok:false,status:503,json:async()=>({detail:'Synthetic transient response failure'})};
      result={delivery:'queued_for_mac',message_id:10};
    } else throw Error('Unexpected request '+path);
    return {ok:true,json:async()=>result};
  };
  const error={textContent:''};
  const form={id:'admin-reply-form',data:{volunteer_id:'1',body:'  TEST exact words.\n🐒  '},querySelector:s=>s==='.error'?error:{disabled:false}};
  try {
    await import('../public/app.js?product-normal-messages');
    await f.click({page:'schedule'});
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/text lab|sample booking|demo-booking/i);
    await f.click({page:'import'});
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/data-setup="sample"|Try a synthetic sample/);
    await f.click({page:'messages'});
    let html=f.elements.get('#app').innerHTML;
    assert.match(html,/Queue text/);
    assert.doesNotMatch(html,/Incoming simulator|Run synthetic|Text Lab|Create text for review|Human confirmation is active|data-simulate|id="simulate-form"/i);
    await f.submit(form);
    assert.equal(error.textContent,'Synthetic transient response failure');
    fail=false; await f.submit(form);
    const payloads=f.calls.filter(c=>c.path==='/api/reply').map(c=>JSON.parse(c.options.body));
    assert.equal(payloads.length,2);
    assert.deepEqual(payloads[0],payloads[1]);
    assert.match(payloads[0].request_id,/^[0-9a-f-]{36}$/);
    assert.equal(payloads[0].body,form.data.body);
    assert.match(f.elements.get('#app').innerHTML,/Text queued/);
    assert.match(f.elements.get('#app').innerHTML,/<textarea[^>]+><\/textarea>/);
    assert.ok(!f.calls.some(c=>c.path.includes('/approve') || c.path.includes('/simulate')));
    form.data.body='Another TEST text';
    await f.listeners.get('input')({target:{id:'reply-body',value:form.data.body}});
    await f.submit(form);
    const last=JSON.parse(f.calls.filter(c=>c.path==='/api/reply').at(-1).options.body);
    assert.notEqual(last.request_id,payloads[0].request_id);
  } finally {f.restore();}
});
