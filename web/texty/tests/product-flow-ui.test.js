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
  globalThis.setTimeout=()=>0; globalThis.setInterval=()=>0;
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
    data:{first_name:'Alex',last_name:'Sample',phone:'(202) 555-0199'},
    elements:{consent:{checked:false}},querySelector:s => s === '.error' ? error : {disabled:false}};
  try {
    await import('../public/app.js?volunteer-local-phone');
    await f.click({page:'volunteers'});
    await f.click({action:'add'});
    assert.match(f.elements.get('#modal').innerHTML, /placeholder="\(303\) 555-0123"/);
    assert.doesNotMatch(f.elements.get('#modal').innerHTML, /Preferred ministry|name="ministry"/);
    await f.submit(form);
    assert.equal(error.textContent, '');
    const payload = JSON.parse(f.calls.find(c => c.path === '/api/volunteers').options.body);
    assert.equal(payload.phone, '+12025550199');
    assert.equal(payload.consent, false);
    assert.equal(payload.ministry, undefined);
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
