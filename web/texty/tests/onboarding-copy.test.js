import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createCopyDraft, renderCopy, COPY_LABELS, VISIBLE_FIELDS, upgradeSavedDefaults, startCopyEditor, copyReturnPath} from '../public/onboarding-copy.js';

const defaults = JSON.parse(fs.readFileSync(new URL('../public/onboarding-copy-defaults.json', import.meta.url)));
const snapshot = messages => ({messages, defaults, revision:0});

test('known cached defaults upgrade while unrelated user edits are preserved', () => {
  const upgraded=upgradeSavedDefaults({interests:'Thanks, {first_name}! What would you like to help with? {roles}. Reply with names or numbers, or Anything. Some roles need coordinator clearance.',availability:'My custom question'},defaults);
  assert.equal(upgraded.interests,defaults.interests);
  assert.equal(upgraded.availability,'My custom question');
  assert.equal(upgraded.welcome,defaults.welcome);
  assert.equal(upgradeSavedDefaults({clarification:'When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. You can also say FLEXIBLE.'}, defaults).clarification, '');
});

test('all four editable fields use the canonical defaults and escaped placeholder values', () => {
  assert.deepEqual(Object.keys(COPY_LABELS), Object.keys(defaults));
  assert.deepEqual(VISIBLE_FIELDS, ['welcome','interests','availability','completion']);
  assert.ok(!VISIBLE_FIELDS.includes('clarification'));
  assert.equal(renderCopy('{roles}'), '1: Greeter, 2: Usher, 3: Production, 4: Coffee, 5: Child Care');
  assert.equal(renderCopy('Hi {first_name}: {roles}', {first_name:'Alex',roles:'1: Greeter'}), 'Hi Alex: 1: Greeter');
});

test('edits, save, reset and reload retain separate saved and draft state', async () => {
  const model = createCopyDraft(snapshot(defaults));
  model.edit('availability', 'Which days work for you?');
  assert.equal(model.dirty(), true);
  let submitted;
  await model.save(async data => {
    submitted = data;
    return {...snapshot(data.messages), revision:1};
  });
  assert.equal(submitted.messages.availability, 'Which days work for you?');
  assert.equal(submitted.revision, 0);
  assert.equal(model.revision(), 1);
  assert.equal(model.dirty(), false);
  model.reset();
  assert.equal(model.messages().availability, defaults.availability);
  assert.equal(model.dirty(), true);
  model.replace({...snapshot(submitted.messages), revision:1});
  assert.equal(model.messages().availability, 'Which days work for you?');
  assert.equal(model.dirty(), false);
});

test('failed save keeps copy and revision for reload or correction', async () => {
  const model = createCopyDraft(snapshot(defaults));
  model.edit('clarification', 'What frequency works for you?');
  await assert.rejects(model.save(async () => {throw Error('Conflict');}), /Conflict/);
  assert.equal(model.messages().clarification, 'What frequency works for you?');
  assert.equal(model.revision(), 0);
  assert.equal(model.dirty(), true);
});

test('state cannot be mutated through a returned message object', () => {
  const model = createCopyDraft(snapshot(defaults));
  model.messages().completion = 'wrong';
  assert.equal(model.messages().completion, defaults.completion);
  assert.throws(() => model.edit('owner_id','different owner'), /Unknown/);
});

test('standalone copy editor restores and refreshes the structured coordinator session',async()=>{
  const keys=['document','sessionStorage','localStorage','fetch','location'], saved=Object.fromEntries(keys.map(key=>[key,globalThis[key]]));
  const elements=new Map(['#copy-fields','#copy-preview','#copy-status','#copy-error','#copy-form','#copy-reset','#copy-reload','#copy-save','#copy-back','#copy-scope']
    .map(key=>[key,{innerHTML:'',textContent:'',querySelectorAll:()=>[],addEventListener(){}}]));
  const calls=[], storage=new Map([['texty.coordinator.session.v1',JSON.stringify({access_token:'old-access',refresh_token:'refresh-secret',expires_at:1})]]);
  globalThis.document={getElementById:()=>({querySelector:selector=>elements.get(selector)})};
  globalThis.sessionStorage={getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
  globalThis.localStorage={};
  globalThis.location={hostname:'text-monkey-demo.pages.dev',pathname:'/onboarding-copy.html',search:'?return_to=%2Ftexty'};
  globalThis.fetch=async(path,options)=>{
    calls.push({path,options});
    const result=path==='/api/config'?{connected:true}:path==='/api/session/refresh'?{access_token:'new-access',refresh_token:'rotated-secret',expires_in:3600}
      :path==='/onboarding-copy-defaults.json'?defaults:{...snapshot(defaults),preview:{first_name:'Alex'}};
    return {ok:true,json:async()=>result};
  };
  try {
    const editor=await startCopyEditor();
    assert.ok(editor.model());
    assert.equal(elements.get('#copy-back').href,'/');
    assert.equal(calls.find(call=>call.path==='/api/setup/onboarding-copy').options.headers.Authorization,'Bearer new-access');
    assert.equal(JSON.parse(calls.find(call=>call.path==='/api/session/refresh').options.body).refresh_token,'refresh-secret');
    assert.equal(JSON.parse(storage.get('texty.coordinator.session.v1')).refresh_token,'rotated-secret');
    assert.ok(!calls.some(call=>/approve|send/.test(call.path)));
  }finally{for(const[key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});

test('copy Back uses the canonical public root and retains the local app entry path',()=>{
  assert.equal(copyReturnPath({hostname:'text-monkey-demo.pages.dev',pathname:'/onboarding-copy.html'}),'/');
  assert.equal(copyReturnPath({hostname:'text-monkey-demo.pages.dev',pathname:'/texty/onboarding-copy.html',search:'?return_to=%2Ftexty'}),'/');
  assert.equal(copyReturnPath({hostname:'localhost',pathname:'/onboarding-copy.html',search:'?return_to=%2Ftexty'}),'/texty');
  assert.equal(copyReturnPath({hostname:'localhost',pathname:'/texty/onboarding-copy.html'}),'/texty');
  assert.equal(copyReturnPath({pathname:'/onboarding-copy.html',search:'?return_to=https%3A%2F%2Fevil.example'}),'/');
  assert.equal(copyReturnPath({pathname:'/onboarding-copy.html',search:'?return_to=%2F%2Fevil.example'}),'/');
  assert.equal(copyReturnPath({pathname:'/onboarding-copy.html',search:'?return_to=%2Fapprovals'}),'/');
});

test('actual copy settings link preserves its app entry without transferring account tokens',async()=>{
  const keys=['document','location','MutationObserver'],saved=Object.fromEntries(keys.map(k=>[k,globalThis[k]]));
  try {
    for(const [hostname,pathname,expected]of [['text-monkey-demo.pages.dev','/','/'],['text-monkey-demo.pages.dev','/texty','/'],['localhost','/texty','/texty']]) {
      let link;
      const panel={querySelector:()=>link,append:value=>{link=value;}};
      globalThis.document={getElementById:()=>({querySelector:()=>panel}),createElement:()=>({dataset:{}})};
      globalThis.location={hostname,pathname,hash:'#access_token=must-not-copy',search:'?refresh_token=must-not-copy'};
      globalThis.MutationObserver=class {observe(){}};
      await import(`../public/onboarding-copy-nav.js?${hostname}-${encodeURIComponent(pathname)}`);
      const url=new URL(link.href,'https://text-monkey-demo.pages.dev');
      assert.equal(url.pathname,'/onboarding-copy.html');assert.equal(url.searchParams.get('return_to'),expected);
      assert.doesNotMatch(link.href,/must-not-copy|access_token|refresh_token/);
      assert.equal(copyReturnPath({hostname,pathname:url.pathname,search:url.search}),expected);
    }
  }finally{for(const[k,v]of Object.entries(saved)){if(v===undefined)delete globalThis[k];else globalThis[k]=v;}}
});

test('the last numbered default upgrades to the five numbered role intro', () => {
  const interests = 'Thanks {first_name}! What would you like to help with? {roles}. Reply with names or numbers, or "Anything". Some roles need coordinator clearance.';
  assert.equal(upgradeSavedDefaults({interests}, defaults).interests, defaults.interests);
  assert.equal(renderCopy(defaults.interests, {first_name:'Clyde'}), 'Thanks Clyde! What would you like to help with? 1: Greeter, 2: Usher, 3: Production, 4: Coffee, 5: Child Care. Reply with numbers 1–5.');
  assert.equal(upgradeSavedDefaults({interests:'My custom intro'}, defaults).interests, 'My custom intro');
});
