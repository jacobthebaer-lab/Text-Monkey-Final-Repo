// Real Chromium, offline synthetic DOM. No Google profile or requests.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {VoiceBrowser} from '../browser.mjs';

const phone='+12025550102',other='+12025550103';
test('recipient proofs wait for asynchronous DOM and hold wrong, ambiguous or stale state',
 {skip:process.env.VOICE_DOM_SELECTOR_PROOF!=='true'},async t=>{
  const chromiumBrowser=await chromium.launch({executablePath:'/usr/bin/chromium',chromiumSandbox:true,headless:true});
  t.after(()=>chromiumBrowser.close());
  for(const [name,options,code] of [
   ['delayed choice label and selected chip',{},null],
   ['keyboard-only suggestions ignore fill but render after supported typing',{keyboardOnly:true},null],
   ['wrong choice label',{choice:other},'recipient_choice_not_verified'],
   ['multiple choices',{multipleChoices:true},'recipient_choice_not_verified'],
   ['wrong selected chip',{chip:other},'recipient_selected_not_verified'],
   ['multiple selected chips',{multipleChips:true},'recipient_selected_not_verified'],
   ['nonnumeric selected chip',{chip:'Synthetic contact'},'recipient_selected_not_verified'],
   ['hidden selected chip',{hiddenChip:true},'recipient_selected_not_verified'],
   ['stale selected route',{item:'t.'+other},'recipient_selected_not_verified'],
   ['stale body after correct recipient',{body:'Unrelated synthetic draft'},'thread_not_observable_draft_recipient_proof'],
   ['old conversation bubble',{bubbles:true},'thread_not_observable_draft_recipient_proof'],
  ]) await t.test(name,async()=>{
   const page=await chromiumBrowser.newPage();
   try{
    page.setDefaultTimeout(1000);
    const browser=new VoiceBrowser({directory:'/unused',allowedPhones:[phone],demoMode:true});
    browser.page=page;
    const wait=browser.waitForRecipientProof.bind(browser);
    // Exercise the production polling helper; shorten only this offline bound.
    browser.waitForRecipientProof=(proof,reason)=>wait(proof,reason,500);
    let url='',fills=0;
    t.mock.method(page,'url',()=>url);
    page.on('request',()=>assert.fail('Synthetic proof must never request a network origin'));
    browser.navigate=async path=>{
     url=`https://voice.google.com/u/0/${path}`;
     if(path.startsWith('search?')){
      await page.setContent(`<div role="button" aria-label="Google Account: synthetic">Account</div>
       <section aria-labelledby="search-heading"><h3 id="search-heading">Search results</h3>
       <p gv-test-id="no-threads-text">No search results found for ${phone}</p></section>`);
      return;
     }
     url=`https://voice.google.com/u/0/messages?${new URLSearchParams({itemId:options.item??'draft'})}`;
     await page.setContent(`<div role="button" aria-label="Google Account: synthetic">Account</div>
      <div role="button" aria-label="Send new message">New message</div>
      <input placeholder="Type a name or phone number"><button id="send-to-button">Suggestion</button>
      <textarea placeholder="Type a message"></textarea>`);
     await page.evaluate(options=>{
      const input=document.querySelector('input');
      if(options.keyboardOnly)document.querySelector('#send-to-button').style.display='none';
      document.querySelector('textarea').value=options.body??'';
      document.querySelector('textarea').addEventListener('input',()=>window.bodyFills=(window.bodyFills??0)+1);
      input.addEventListener(options.keyboardOnly?'keyup':'input',()=>{
       if(input.value!=='+12025550102'||window.suggestionScheduled)return;
       window.suggestionScheduled=true;
       setTimeout(()=>{
       const choice=document.querySelector('#send-to-button');
       choice.style.display='';
       choice.insertAdjacentHTML('beforeend',`<div aria-hidden="true" class="send-to-label">Send to ${options.choice??'(202) 555-0102'}</div>`);
       if(options.multipleChoices)choice.insertAdjacentElement('afterend',choice.cloneNode(true));
       },75);
      });
      document.querySelector('#send-to-button').addEventListener('click',()=>setTimeout(()=>{
       const region=document.createElement('div');region.setAttribute('role','region');region.setAttribute('aria-label','Select recipients');
       const chip=`<mat-chip-row><div class="chip-name" aria-hidden="true" ${options.hiddenChip?'style="display:none"':''}>${options.chip??'\u202a(202) 555-0102\u202c'}</div></mat-chip-row>`;
       region.innerHTML=chip+(options.multipleChips?chip:'');document.body.append(region);
       if(options.bubbles)document.body.insertAdjacentHTML('beforeend','<gv-text-message-item>Old synthetic item</gv-text-message-item>');
      },75));
     },options);
    };
    if(options.keyboardOnly){
     await browser.navigate('messages');
     await page.locator('input').fill(phone);
     await page.waitForTimeout(100);
     assert.equal(await page.locator('#send-to-button').isVisible(),false);
     assert.equal(await page.locator('.send-to-label').count(),0);
    }
    if(code)await assert.rejects(browser.verifyEmptyFirstRecipient(phone),{code});
    else{
     await browser.verifyEmptyFirstRecipient(phone);
     assert.equal(await browser.recipientVerified(phone),true);
     assert.equal(await page.locator('textarea').inputValue(),'');
    }
    fills=await page.evaluate(()=>window.bodyFills??0);
    assert.equal(fills,0);assert.equal(await page.locator('button[aria-label="Send message"]').count(),0);
    assert.equal(browser.prepared??null,null);
   }finally{await page.close();}
  });
 });
