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

const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const loginForm=()=>{
  const button={disabled:false,textContent:'Sign in'},error={textContent:''};let resets=0;
  const form={id:'login-form',data:{email:'coordinator@example.test',password:'synthetic-password-only'},querySelector:s=>s==='.error'?error:button,reset(){resets++;}};
  return {form,button,error,get resets(){return resets;}};
};

for(const oldStatus of [200,503])test(`older workspace completion cannot release a newer login, optional response ${oldStatus}`,async()=>{
  const f=fixture(),oldCheck=deferred(),oldStarted=deferred(),newSetup=deferred(),newStarted=deferred();
  let account=0;
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options});let result,status=200;
    if(path==='/api/config')result={connected:true};
    else if(path==='/api/login')result={access_token:`synthetic-account-${++account}`};
    else if(path==='/api/logout')result={};
    else if(path==='/api/state')result=seed();
    else if(path==='/api/setup'){
      if(account===2){newStarted.resolve();await newSetup.promise;}
      result={details:{church_name:`TEST Account ${account}`},completed:true};
    }else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/setup/admin-texts'){
      if(account===1){oldStarted.resolve();await oldCheck.promise;status=oldStatus;}
      result=status===200?{enabled:false}:{detail:'Old account private failure'};
    }else throw Error('Unexpected request '+path);
    return {ok:status===200,status,json:async()=>result};
  };
  const a=loginForm(),b=loginForm();let first,second;
  try{
    await import(`../public/app.js?product-login-account-race-${oldStatus}`);
    first=f.submit(a.form);await oldStarted.promise;
    assert.match(f.elements.get('#app').innerHTML,/Account 1/);
    await f.click({action:'logout'});
    second=f.submit(b.form);await newStarted.promise;
    oldCheck.resolve();await first;
    assert.equal(a.resets,0);
    assert.equal(b.button.disabled,true);assert.equal(b.button.textContent,'Opening workspace…');
    const states=f.calls.filter(c=>c.path==='/api/state').length;
    await f.listeners.get('poll')();await f.submit(b.form);await f.click({auth:'register'});
    assert.equal(f.calls.filter(c=>c.path==='/api/state').length,states);
    assert.equal(f.calls.filter(c=>c.path==='/api/login').length,2);
    assert.match(f.elements.get('#app').innerHTML,/id="login-form"/);
    assert.doesNotMatch(f.elements.get('#app').innerHTML,/Account 1|Old account private failure/);
    newSetup.resolve();await second;
    assert.equal(b.resets,1);assert.equal(b.button.disabled,false);
    assert.match(f.elements.get('#app').innerHTML,/Account 2/);
    assert.equal([...f.storage.values()][0],'synthetic-account-2');
  }finally{oldCheck.resolve();newSetup.resolve();await Promise.allSettled([first,second].filter(Boolean));f.restore();}
});

for(const scenario of ['expired-grant','rejected-workspace'])test(`workspace startup handles ${scenario} without a stuck login`,async()=>{
  const f=fixture();let attempts=0;
  globalThis.fetch=async(path,options)=>{
    f.calls.push({path,options});let result,status=200;
    if(path==='/api/config')result={connected:true};
    else if(path==='/api/login'){
      attempts++;result=scenario==='expired-grant'?{access_token:'synthetic-expired',refresh_token:'synthetic-parent',expires_at:1}:{access_token:`synthetic-login-${attempts}`};
    }else if(path==='/api/session/refresh')result={access_token:'synthetic-rotated',refresh_token:'synthetic-child',expires_in:3600};
    else if(path==='/api/state'){
      if(scenario==='rejected-workspace'&&attempts===1){status=401;result={detail:'Expired session'};}
      else{result=seed();if(scenario==='expired-grant')assert.equal(options.headers.Authorization,'Bearer synthetic-rotated');}
    }else if(path==='/api/setup')result={details:{church_name:'TEST Church'},completed:true};
    else if(path==='/api/setup/contacts')result={contacts:[]};
    else if(path==='/api/setup/admin-texts')result={enabled:false};
    else throw Error('Unexpected request '+path);
    return {ok:status===200,status,json:async()=>result};
  };
  try{
    await import(`../public/app.js?product-startup-${scenario}`);
    const first=loginForm();await f.submit(first.form);
    if(scenario==='rejected-workspace'){
      assert.equal(f.storage.size,0);assert.equal(first.resets,0);
      assert.match(f.elements.get('#app').innerHTML,/id="login-form"/);
      assert.doesNotMatch(f.elements.get('#app').innerHTML,/Signed-in admin/);
      const second=loginForm();await f.submit(second.form);
      assert.equal(second.resets,1);assert.equal(second.button.disabled,false);
    }else{
      assert.equal(f.calls.filter(c=>c.path==='/api/session/refresh').length,1);
      assert.equal(JSON.parse([...f.storage.values()][0]).access_token,'synthetic-rotated');
      assert.equal(first.resets,1);assert.equal(first.button.disabled,false);
    }
    assert.match(f.elements.get('#app').innerHTML,/data-page="overview" aria-current="page"/);
  }finally{f.restore();}
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
    await f.click({volunteer:'1'});
    let html=f.elements.get('#app').innerHTML;
    assert.match(html,/Queue text/);
    assert.doesNotMatch(html,/Incoming simulator|Run synthetic|Text Lab|Create text for review|Human confirmation is active|data-simulate|id="simulate-form"/i);
    await f.submit(form);
    assert.equal(error.textContent,'transient response failure');
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

test('Add a volunteer sends a canonical phone to the connected API without granting consent', async () => {
  const f = fixture(), state = seed();
  globalThis.location.hash = '#access_token=synthetic-confirmed-session';
  globalThis.fetch = async (path, options) => {
    f.calls.push({path, options});
    let result;
    if (path === '/api/config') result = {connected:true};
    else if (path === '/api/setup') result = {details:{church_name:'TEST Church',country:'US'},completed:true,revision:1};
    else if (path === '/api/setup/contacts') result = {contacts:[]};
    else if (path === '/api/state') result = state;
    else if (path === '/api/volunteers') result = {id:99};
    else throw Error('Unexpected request ' + path);
    return {ok:true,json:async () => result};
  };
  const error = {textContent:''};
  const form = {id:'volunteer-form',dataset:{id:''},
    data:{first_name:'Alex',last_name:'Sample',phone:'(202) 555-0199',ministry:'Welcome'},
    elements:{consent:{checked:false}},querySelector:s => s === '.error' ? error : {disabled:false}};
  try {
    await import('../public/app.js?volunteer-local-phone');
    await f.click({page:'volunteers'});
    await f.click({action:'add'});
    assert.match(f.elements.get('#modal').innerHTML, /placeholder="\(303\) 555-0123"/);
    await f.submit(form);
    assert.equal(error.textContent, '');
    const payload = JSON.parse(f.calls.find(c => c.path === '/api/volunteers').options.body);
    assert.equal(payload.phone, '+12025550199');
    assert.equal(payload.consent, false);
    assert.ok(!f.calls.some(c => /signup-invitations|\/send|\/reply/.test(c.path)));
  } finally {f.restore();}
});

test('Shifts shows each actual required qualification, not a background-check guess', async () => {
  const f=fixture(),state=seed();
  state.assignments=[];state.proposals=[];
  const examples=[
    {role:'Production',required_qualifications:['sound_training']},
    {role:'Child Care',required_qualifications:['background_check','child_safety_training']},
    {role:'Greeter',required_qualifications:[]},
    {role:'Legacy role'},
    {role:'Custom role',required_qualifications:['custom_<training>']},
  ];
  state.shifts=examples.map((example,index)=>({...state.shifts[0],id:`qualification-${index}`,sensitive:true,...example}));
  globalThis.localStorage.getItem=key=>key==='texty.synthetic.v1'?JSON.stringify(state):null;
  globalThis.fetch=async path=>{
    f.calls.push({path});assert.equal(path,'/api/config');
    return {ok:true,json:async()=>({publicDemo:true,connected:false})};
  };
  try {
    await import('../public/app.js?shift-requirement-names');
    await f.click({action:'demo'});await f.click({page:'schedule'});
    const html=f.elements.get('#app').innerHTML;
    const table=html.match(/<table><thead><tr><th>Role<\/th>[\s\S]*?<\/table>/)[0];
    const rows=[...table.matchAll(/<tr><td><strong>(.*?)<\/strong>([\s\S]*?)<\/tr>/g)];
    const byRole=Object.fromEntries(rows.map(([,role,body])=>[role,body]));
    assert.match(byRole.Production,/<small>Requires sound training<\/small>/);
    assert.doesNotMatch(byRole.Production,/background check/i);
    assert.match(byRole['Child Care'],/<small>Requires background check, child safety training<\/small>/);
    assert.doesNotMatch(byRole.Greeter,/Requires|Qualifications required/);
    assert.match(byRole['Legacy role'],/<small>Qualifications required<\/small>/);
    assert.match(byRole['Custom role'],/Requires custom &lt;training&gt;/);
    assert.doesNotMatch(table,/Background check required|<training>|\u2014/);
    assert.deepEqual(f.calls,[{path:'/api/config'}]);
  } finally {f.restore();}
});
