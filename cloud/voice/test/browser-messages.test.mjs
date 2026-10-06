import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {VoiceBrowser,parseAccessibleMessage,normalizeBubbles,selectors} from '../browser.mjs';

const phone='+12025550102',body='Hello 🐵. Reply with your name 🙈.';
const metadata='Tuesday, October 6 2026, 1:58 AM.';
const observed=(text=body,incoming=false)=>({containers:1,accessibleCount:1,bodyCount:1,directionKnown:true,incoming,
 accessible:`Message from ${incoming?'2 0 2 5 5 5 0 1 0 2':'you'}, ${text}, ${metadata} `});
test('accessible protocol preserves exact emoji, punctuation, whitespace and embedded date-like body',()=>{
 for(const text of [body,' leading and trailing  ','A, Tuesday, October 6 2026, 1:58 AM. Then another sentence.','Name\nwith newline']){
  const row=parseAccessibleMessage(observed(text));assert.equal(row.text,text);
  assert.deepEqual(row.timestamps,['2026-10-06T01:58:00.000Z']);
 }
 const row=parseAccessibleMessage(observed('Synthetic Person',true));assert.equal(row.senderPhone,phone);
 const first=normalizeBubbles([row],`t.${phone}`,phone);
 assert.deepEqual(normalizeBubbles([row],`t.${phone}`,phone),first);
 assert.equal(first[0].body,'Synthetic Person');
});
for(const [label,mutate,code] of [
 ['duplicate container',r=>r.containers=2,'message_format_changed'],
 ['duplicate accessible text',r=>r.accessibleCount=2,'message_format_changed'],
 ['missing body',r=>r.bodyCount=0,'message_format_changed'],
 ['ambiguous direction',r=>r.directionKnown=false,'message_format_changed'],
 ['wrong sender direction',r=>r.incoming=true,'message_format_changed'],
 ['contact-name metadata',r=>r.accessible=r.accessible.replace('you','Synthetic Contact'),'message_format_changed'],
 ['relative timestamp',r=>r.accessible=r.accessible.replace(metadata,'Today, 1:58 AM.'),'message_format_changed'],
 ['invalid day',r=>r.accessible=r.accessible.replace('October 6','October 32'),'message_timestamp_unavailable'],
 ['wrong weekday',r=>r.accessible=r.accessible.replace('Tuesday','Monday'),'message_timestamp_unavailable'],
 ['invalid hour',r=>r.accessible=r.accessible.replace('1:58','13:58'),'message_timestamp_unavailable'],
 ['invalid minute',r=>r.accessible=r.accessible.replace('1:58','1:60'),'message_timestamp_unavailable'],
])test('accessible protocol holds '+label,()=>{
 const row=observed();mutate(row);assert.throws(()=>parseAccessibleMessage(row),{code});
});

const item=(text,incoming=false,extra='')=>`<gv-message-item dir="ltr" class="end-of-cluster">
 <div class="full-container end-of-cluster has-status ${incoming?'incoming':'outgoing'} start-of-cluster">
 <div class="container"><div class="status timestamp visible" aria-hidden="true">1:58 AM</div>
 <div class="cdk-visually-hidden">Message from ${incoming?'2 0 2 5 5 5 0 1 0 2':'you'}, ${text}, ${metadata} </div>
 <div class="message-row ${incoming?'':'outgoing'}"><div class="subject-content-container bubble"><div dir="auto">
 <gv-annotation class="content" aria-hidden="true"><img alt="" aria-label="monkey face">Visible body omits glyph codepoints</gv-annotation>
 </div></div></div>${extra}</div></div></gv-message-item>`;
test('observed actual wrapper supplies exact shared acknowledgement and inbound parser',
 {skip:process.env.VOICE_DOM_SELECTOR_PROOF!=='true'},async t=>{
 const native=await chromium.launch({executablePath:'/usr/bin/chromium',chromiumSandbox:true,headless:true});t.after(()=>native.close());
 for(const mode of ['submitted','different exact body','incoming sender mismatch','duplicate accessible sibling'])await t.test(mode,async()=>{
  const page=await native.newPage();try{
   let clicks=0;const browser=new VoiceBrowser({directory:'/unused',allowedPhones:[phone]});browser.page=page;
   t.mock.method(page,'url',()=>`https://voice.google.com/u/0/messages?${new URLSearchParams({itemId:`t.${phone}`})}`);
   await page.setContent(`<textarea placeholder="Type a message"></textarea><button aria-label="Send message">Send</button>`);
   await page.locator('textarea').fill(body);
   const wait=browser.waitForRecipientProof.bind(browser);browser.waitForRecipientProof=(proof,reason)=>wait(proof,reason,300);
   if(mode==='incoming sender mismatch'||mode==='duplicate accessible sibling'){
    let html=item('Synthetic Person',true,mode==='duplicate accessible sibling'?'<div class="cdk-visually-hidden">Duplicate</div>':'');
    if(mode==='incoming sender mismatch')html=html.replace('2 0 2 5 5 5 0 1 0 2','2 0 2 5 5 5 0 1 0 3');
    await page.locator('body').evaluate((element,html)=>element.insertAdjacentHTML('beforeend',html),html);
    await assert.rejects(browser.rows(phone),{code:mode==='incoming sender mismatch'?'message_sender_mismatch':'message_format_changed'});
    return;
   }
   const outgoing=item(mode==='submitted'?body:body+' ');
   await page.locator('button').evaluate((button,html)=>button.addEventListener('click',()=>{
    document.querySelector('textarea').value='';document.body.insertAdjacentHTML('beforeend',html);window.clicks=(window.clicks??0)+1;
   }),outgoing);
   browser.prepared={to:phone,body,before:0};
   const result=await browser.submitSend(phone,body,new Date(Date.now()+30000).toISOString());
   assert.equal(result.status,mode==='submitted'?'submitted':'uncertain');
   clicks=await page.evaluate(()=>window.clicks);assert.equal(clicks,1);
   assert.equal(await page.locator('gv-text-message-item').count(),0);
   if(mode==='submitted'){
    await page.locator('body').evaluate((element,html)=>element.insertAdjacentHTML('beforeend',html),item('Synthetic Person',true));
    const rows=await browser.rows(phone);assert.equal(rows[0].text,body);assert.equal(rows[1].text,'Synthetic Person');
    assert.equal(rows[1].directionKnown,true);assert.equal(rows[1].incoming,true);
    assert.equal(normalizeBubbles(rows,`t.${phone}`,phone)[0].received_at,'2026-10-06T01:58:00.000Z');
    assert.equal(await page.locator(selectors.bubbles).count(),2);
   }
  }finally{await page.close();}
 });
});
