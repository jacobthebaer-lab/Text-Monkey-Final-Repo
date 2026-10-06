import test from 'node:test';
import assert from 'node:assert/strict';
import {VoiceBrowser,selectors} from '../browser.mjs';
import {Hold} from '../core.mjs';

const phases=['navigation','open','input','choice','selection','escape','verification','composer'];
const phone='+12025550102';
function fixture(failure,error=new Error('synthetic private page details')) {
 const seen=[];
 const step=phase=>{seen.push(phase);if(phase===failure)throw error;};
 const browser=new VoiceBrowser({directory:'/unused',allowedPhones:[phone],demoMode:true});
 browser.navigate=async path=>{assert.equal(path,'messages');step('navigation');};
 browser.recipientVerified=async recipient=>{assert.equal(recipient,phone);step('verification');return true;};
 const composer={count:async()=>{step('composer');return 1;}};
 const choice={waitFor:async()=>step('choice'),count:async()=>1,click:async()=>step('selection'),
  locator:selector=>{assert.equal(selector,selectors.recipientChoiceLabel);return {
   count:async()=>1,isVisible:async()=>true,textContent:async()=> 'Send to (202) 555-0102'};}};
 browser.page={keyboard:{press:async key=>{assert.equal(key,'Escape');step('escape');}},locator:selector=>{
  if(selector===selectors.newMessage)return {click:async()=>step('open')};
  if(selector===selectors.recipient)return {fill:async recipient=>{assert.equal(recipient,phone);step('input');}};
  if(selector===selectors.recipientChoice)return choice;
  if(selector===selectors.compose)return composer;
  throw Error('Unexpected operation');
 }};
 return {browser,seen,composer};
}

for(const phase of phases)test('recipient '+phase+' failure exposes only its fixed phase and stops subsequent steps',async()=>{
 const {browser,seen}=fixture(phase);
 await assert.rejects(browser.prepareRecipient(phone),error=>{
  assert.ok(error instanceof Hold);assert.equal(error.code,'recipient_'+phase+'_unavailable');
  assert.equal(error.message,error.code);assert.equal(error.cause,undefined);return true;
 });
 assert.deepEqual(seen,phases.slice(0,phases.indexOf(phase)+1));
});

for(const [phase,code] of [['navigation','reconnect_required'],['choice','recipient_not_verified']])
 test('existing '+code+' Hold remains the identical error',async()=>{
  const original=new Hold(code,409);const {browser}=fixture(phase,original);
  await assert.rejects(browser.prepareRecipient(phone),error=>error===original);
 });

test('successful recipient preparation preserves the original operation order and returns the composer',async()=>{
 const {browser,seen,composer}=fixture();
 assert.equal(await browser.prepareRecipient(phone),composer);assert.deepEqual(seen,phases);
});
