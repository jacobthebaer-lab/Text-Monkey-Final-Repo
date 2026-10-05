// Explicit synthetic process proof, run only in the isolated browser container.
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp,rm,lstat} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const childSource = `
import assert from 'node:assert/strict';
import {VoiceBrowser} from './browser.mjs';
import {installShutdown} from './server.mjs';
const origin=process.env.SYNTHETIC_ORIGIN;
assert.equal(new URL(origin).hostname,'127.0.0.1');
const browser=new VoiceBrowser({directory:process.env.SYNTHETIC_PROFILE,executablePath:process.env.VOICE_BROWSER_PATH,allowedPhones:[],demoMode:true});
await browser.start();
assert.equal(process.listenerCount('SIGTERM'),0);
assert.equal(process.listenerCount('SIGINT'),0);
installShutdown({close:()=>browser.close()});
assert.equal(process.listenerCount('SIGTERM'),1);
assert.equal(process.listenerCount('SIGINT'),1);
assert.ok(process.listenerCount('SIGHUP')>0);
await browser.page.goto(origin);
if(process.env.SYNTHETIC_OPERATION==='write') {
 await browser.context.addCookies([{name:'synthetic-marker',value:'synthetic-only',url:origin,expires:Math.floor(Date.now()/1000)+3600}]);
 await browser.page.evaluate(()=>localStorage.setItem('synthetic-marker','synthetic-only'));
} else {
 assert.equal(await browser.page.evaluate(()=>localStorage.getItem('synthetic-marker')),'synthetic-only');
 assert.equal((await browser.context.cookies(origin)).find(cookie=>cookie.name==='synthetic-marker')?.value,'synthetic-only');
}
process.stdout.write('SYNTHETIC_READY\\n');
setInterval(()=>{},1000);
`;

async function launch(t,directory,origin,operation) {
 const child=spawn(process.execPath,['--input-type=module','-e',childSource],{
  cwd:new URL('..',import.meta.url),env:{...process.env,SYNTHETIC_PROFILE:directory,SYNTHETIC_ORIGIN:origin,SYNTHETIC_OPERATION:operation},stdio:['ignore','pipe','pipe'],
 });
 t.after(()=>{if(child.exitCode===null)child.kill('SIGKILL');});
 let stderr='';child.stderr.on('data',chunk=>{stderr+=chunk;});
 const exited=new Promise((resolve,reject)=>{
  child.once('error',reject);child.once('exit',(code,signal)=>resolve({code,signal}));
 });
 const ready=new Promise((resolve,reject)=>{
  let output='';
  const timer=setTimeout(()=>reject(new Error('Synthetic browser startup timed out: '+stderr)),20000);
  child.stdout.on('data',chunk=>{output+=chunk;if(output.includes('SYNTHETIC_READY')){clearTimeout(timer);resolve();}});
  exited.then(result=>{clearTimeout(timer);reject(new Error('Synthetic browser exited before ready: '+JSON.stringify(result)+' '+stderr));},reject);
 });
 await ready;
 return {child,exited};
}

for(const signal of ['SIGTERM','SIGINT']) {
 test(`actual ${signal} closes Chromium once, removes profile locks and preserves synthetic state after process restart`,
  {skip:process.env.VOICE_PROCESS_SIGNAL_PROOF!=='true',timeout:60000},async t=>{
   const directory=await mkdtemp(join(tmpdir(),'voice-signal-proof-'));
   t.after(()=>rm(directory,{recursive:true,force:true}));
   const server=createServer((_request,response)=>{response.setHeader('Content-Type','text/html');response.end('<!doctype html><title>Synthetic persistence proof</title>');});
   await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
   t.after(()=>new Promise(resolve=>server.close(resolve)));
   const origin=`http://127.0.0.1:${server.address().port}/synthetic`;
   for(const operation of ['write','read']) {
    const {child,exited}=await launch(t,directory,origin,operation);
    assert.ok((await lstat(join(directory,'profile','SingletonLock'))).isSymbolicLink());
    child.kill(signal);
    const result=await Promise.race([exited,new Promise((_resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Synthetic shutdown timed out')),15000);timer.unref();})]);
    assert.deepEqual(result,{code:0,signal:null});
    for(const name of ['SingletonLock','SingletonSocket','SingletonCookie']) {
     await assert.rejects(()=>lstat(join(directory,'profile',name)),{code:'ENOENT'});
    }
   }
  });
}
