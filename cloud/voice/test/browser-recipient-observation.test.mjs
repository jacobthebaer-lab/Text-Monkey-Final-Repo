import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {VoiceBrowser} from '../browser.mjs';
import {Connector} from '../core.mjs';

const phone='+12025550102',id='a'.repeat(32);
test('real offline DOM observation captures alternate recipient controls and visible errors, without unrelated text',async t=>{
 const browser=await chromium.launch({...(process.env.VOICE_DOM_BROWSER?{executablePath:process.env.VOICE_DOM_BROWSER}:{}),chromiumSandbox:true,headless:true});
 t.after(()=>browser.close());const page=await browser.newPage();t.after(()=>page.close());
 await page.route('**/*',route=>route.abort());
 const voice=new VoiceBrowser({directory:'/unused',allowedPhones:[phone],demoMode:true});voice.page=page;
 for(const [name,html,expected] of [
  ['alternate option, no old contact-list',`<div role="option" id="numeric-option" class="candidate"><span>(202) 555-0102</span></div>
    <button id="unrelated-person">PRIVATE CONTACT NAME</button><gv-message-item>PRIVATE HISTORY BODY</gv-message-item>`,{contact_list_count:0,id:'numeric-option',numeric:true}],
  ['hidden numeric choice and visible error',`<gv-contact-list><button id="send-to-button" style="display:none"><span>(202) 555-0102</span></button></gv-contact-list>
    <div role="alert" id="validation-error">Invalid phone number PRIVATE DETAIL</div>`,{contact_list_count:1,id:'send-to-button',numeric:true,visible:false,error:true}],
  ['actual visible numeric choice',`<gv-contact-list><button id="send-to-button"><div class="send-to-label" aria-hidden="true">Send to (202) 555-0102</div></button></gv-contact-list>`,{contact_list_count:1,id:'send-to-button',numeric:true}],
 ])await t.test(name,async()=>{
  await page.setContent(`<form id="recipient-form"><div class="recipient-wrapper"><input placeholder="Type a name or phone number" value="${phone}"></div>${html}</form>`);
  const result=await voice.observeRecipient(phone);
  assert.equal(result.recipient_input_exact,true);assert.equal(result.recipient_input_visible,true);
  assert.equal(result.contact_list_count,expected.contact_list_count);
  const control=result.controls.find(row=>row.id===expected.id);assert.ok(control);assert.equal(control.numeric_target,expected.numeric);
  if(expected.visible!==undefined)assert.equal(control.visible,expected.visible);
  if(expected.error){assert.equal(result.statuses[0].hint,'invalid_input');assert.equal(result.statuses[0].visible,true);}
  assert.ok(result.ancestors.length);assert.equal(JSON.stringify(result).includes('PRIVATE'),false);
 });
 await t.test('wrong value or hidden field prevents reading surrounding structures',async()=>{
  await page.setContent(`<form><input placeholder="Type a name or phone number" value="+12025550103"><button>PRIVATE</button></form>`);
  assert.deepEqual(await voice.observeRecipient(phone),{recipient_input_count:1,recipient_input_visible:true,recipient_input_exact:false});
  await page.locator('input').evaluate(field=>{field.value='+12025550102';field.style.display='none';});
  assert.deepEqual(await voice.observeRecipient(phone),{recipient_input_count:1,recipient_input_visible:false,recipient_input_exact:true});
 });
});

test('recipient observer validates scope, existing sender and pending preparation without mutation',async()=>{
 const now='2026-10-06T04:45:00Z',sessions={[phone]:{id,starts_at:'2026-10-06T04:00:00Z',expires_at:'2026-10-06T05:00:00Z'}};
 let observations=0;
 const store={data:{demo_sessions:sessions,sends:{},baseline_at:now,next_cursor:1},save:async()=>assert.fail('No store mutation')};
 const browser={identity:async()=>({email:'synthetic@example.test',phone:'+12025550101'}),
  verifyPreparedIdentity:async expected=>assert.deepEqual(expected,{email:'synthetic@example.test',phone:'+12025550101'}),
  observeRecipient:async target=>{assert.equal(target,phone);observations++;return {recipient_input_exact:true};}};
 const connector=new Connector({store,browser,expectedEmail:'synthetic@example.test',expectedPhone:'+12025550101',allowedPhones:[phone],testSessions:sessions,demoMode:true,now:()=>now});
 await assert.rejects(connector.recipientObservation({phone,session_id:'b'.repeat(32)}),{code:'invalid_recipient_observation'});
 connector.preparation={key:'protected',not_after:'2026-10-06T04:45:20Z'};
 await assert.rejects(connector.recipientObservation({phone,session_id:id}),{code:'preparation_in_progress'});
 connector.preparation=null;
 assert.deepEqual(await connector.recipientObservation({phone,session_id:id}),{native_submission_attempted:false,observation:{recipient_input_exact:true}});
 assert.equal(observations,1);assert.deepEqual(store.data.sends,{});
 browser.verifyPreparedIdentity=async()=>{throw Error('synthetic sender mismatch');};
 await assert.rejects(connector.recipientObservation({phone,session_id:id}));assert.equal(observations,1);
});
