// Opt-in real Chromium accessibility selector proof, synthetic DOM only.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {VoiceBrowser} from '../browser.mjs';

test('first-recipient proof resolves implicit named sections and rejects other labels',
 {skip:process.env.VOICE_DOM_SELECTOR_PROOF!=='true'},async t=>{
  const chromiumBrowser=await chromium.launch({executablePath:'/usr/bin/chromium',chromiumSandbox:true,headless:true});
  t.after(()=>chromiumBrowser.close());
  const phone='+12025550102';
  for(const label of ['Search results','Messages','Search results elsewhere']) {
   await t.test(label,async()=>{
    const page=await chromiumBrowser.newPage();
    try {
     page.setDefaultTimeout(100);
     await page.setContent(`<div role="button" aria-label="Google Account: synthetic">Account</div>
      <gv-thread-list><section aria-labelledby="thread-list-heading" class="container">
       <h3 id="thread-list-heading" style="position:absolute;width:1px;height:1px;overflow:hidden">${label}</h3>
       <div class="no-threads"><p gv-test-id="no-threads-text" tabindex="-1" class="no-threads-text">No search results found for ${phone}</p></div>
      </section></gv-thread-list>`);
     const browser=new VoiceBrowser({directory:'/unused',allowedPhones:[phone],demoMode:true});
     browser.page=page;browser.navigate=async()=>{};
     t.mock.method(page,'url',()=>`https://voice.google.com/u/0/search?${new URLSearchParams({from:'[]',q:JSON.stringify([phone])})}`);
     let drafts=0;
     browser.prepareRecipient=async()=>{drafts++;throw Error('synthetic draft boundary');};
     if(label==='Search results') {
      // The actual observed markup has neither a role nor aria-label attribute.
      assert.equal(await page.locator('[role="region"][aria-label="Search results"]').count(),0);
      await assert.rejects(browser.verifyEmptyFirstRecipient(phone),{message:'synthetic draft boundary'});
      assert.equal(drafts,1);
     } else {
      await assert.rejects(browser.verifyEmptyFirstRecipient(phone),{code:'thread_not_observable_search_load'});
      assert.equal(drafts,0);
     }
    } finally {await page.close();}
   });
  }
 });
