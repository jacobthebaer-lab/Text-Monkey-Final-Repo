import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {VoiceBrowser} from '../browser.mjs';

test('fresh connector page preserves synthetic session cookies across profile restart',
 {skip:process.env.VOICE_DOM_SELECTOR_PROOF!=='true'},async t=>{
  const directory=await mkdtemp(join(tmpdir(),'voice-fresh-page-'));
  t.after(()=>rm(directory,{recursive:true,force:true}));
  const server=createServer((req,res)=>{
   if(req.url==='/set')res.setHeader('Set-Cookie','synthetic-session=synthetic-only; HttpOnly; Path=/');
   res.end(req.headers.cookie??'');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const config={directory,executablePath:process.env.VOICE_DOM_BROWSER??'/usr/bin/chromium',allowedPhones:[],demoMode:true};
  const first=new VoiceBrowser(config);
  try{
   await first.start();await first.page.goto(origin+'/set');
  }finally{await first.close();}
  const restarted=new VoiceBrowser(config);
  try{
   await restarted.start();
   assert.equal(restarted.page.url(),'about:blank');
   assert.equal(restarted.context.pages().length,1);
   await restarted.page.goto(origin+'/read');
   assert.equal(await restarted.page.locator('body').innerText(),'synthetic-session=synthetic-only');
  }finally{await restarted.close();}
 });
