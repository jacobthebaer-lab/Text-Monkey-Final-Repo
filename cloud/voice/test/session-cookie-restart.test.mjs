// Opt-in native Chromium + connector proof using only fresh synthetic profiles.
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp,rm,readFile,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {VoiceBrowser} from '../browser.mjs';

const closeWindow = `
import ctypes
x=ctypes.CDLL('libX11.so.6')
D=ctypes.c_void_p; W=ctypes.c_ulong
x.XOpenDisplay.restype=D
x.XDefaultRootWindow.argtypes=[D]; x.XDefaultRootWindow.restype=W
x.XQueryTree.argtypes=[D,W,ctypes.POINTER(W),ctypes.POINTER(W),ctypes.POINTER(ctypes.POINTER(W)),ctypes.POINTER(ctypes.c_uint)]
x.XGetWMProtocols.argtypes=[D,W,ctypes.POINTER(ctypes.POINTER(W)),ctypes.POINTER(ctypes.c_int)]
x.XInternAtom.argtypes=[D,ctypes.c_char_p,ctypes.c_int];x.XInternAtom.restype=W
x.XFree.argtypes=[ctypes.c_void_p]
x.XFlush.argtypes=[D];x.XCloseDisplay.argtypes=[D]
class Data(ctypes.Union): _fields_=[('l',ctypes.c_long*5)]
class Client(ctypes.Structure): _fields_=[('type',ctypes.c_int),('serial',ctypes.c_ulong),('send_event',ctypes.c_int),('display',D),('window',W),('message_type',W),('format',ctypes.c_int),('data',Data)]
class Event(ctypes.Union): _fields_=[('client',Client),('padding',ctypes.c_long*24)]
x.XSendEvent.argtypes=[D,W,ctypes.c_int,ctypes.c_long,ctypes.POINTER(Event)]
d=x.XOpenDisplay(None);assert d
root=x.XDefaultRootWindow(d);parent=W();root_out=W();children=ctypes.POINTER(W)();count=ctypes.c_uint()
assert x.XQueryTree(d,root,ctypes.byref(root_out),ctypes.byref(parent),ctypes.byref(children),ctypes.byref(count))
closed=0
# This fresh X display contains only this synthetic Chromium process. Close its
# top-level WM_DELETE_WINDOW clients, without killing the X connection/process.
delete=x.XInternAtom(d,b'WM_DELETE_WINDOW',0)
for i in range(count.value):
 protocols=ctypes.POINTER(W)();n=ctypes.c_int()
 if x.XGetWMProtocols(d,children[i],ctypes.byref(protocols),ctypes.byref(n)):
  match=any(protocols[j]==delete for j in range(n.value));x.XFree(protocols)
  if match:
   e=Event();e.client.type=33;e.client.send_event=1;e.client.display=d;e.client.window=children[i]
   e.client.message_type=x.XInternAtom(d,b'WM_PROTOCOLS',0);e.client.format=32
   e.client.data.l[0]=delete
   assert x.XSendEvent(d,children[i],0,0,ctypes.byref(e));closed+=1
x.XFree(children);x.XFlush(d);x.XCloseDisplay(d)
print(closed)
`;

const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(predicate) {
 const deadline=Date.now()+20000;
 while(Date.now()<deadline) {if(predicate())return;await sleep(50);}
 throw Error('Synthetic browser observation timed out');
}

async function native(t, directory, url, restore) {
 if(restore) execFileSync('python3',['-c',`import importlib.util;from pathlib import Path;s=importlib.util.spec_from_file_location("synthetic_login","/proof/browser-login.py");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.prepare_session_restoration(Path(${JSON.stringify(join(directory,'profile'))}))`]);
 const child=spawn('/usr/bin/chromium',[
  `--user-data-dir=${join(directory,'profile')}`,'--no-first-run','--no-default-browser-check',
  '--window-size=1280,900',url,
 ],{stdio:'ignore'});
 t.after(()=>{if(child.exitCode===null)child.kill('SIGKILL');});
 const exited=new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',(code,signal)=>resolve({code,signal}));});
 return async()=>{
  await until(()=>Number(execFileSync('python3',['-c',closeWindow],{encoding:'utf8'}).trim())>0);
  let timer;
  const result=await Promise.race([exited,new Promise((_resolve,reject)=>{timer=setTimeout(()=>reject(Error('Native Chromium clean exit timed out')),15000);})]).finally(()=>clearTimeout(timer));
  assert.deepEqual(result,{code:0,signal:null});
  const preferences=JSON.parse(await readFile(join(directory,'profile','Default','Preferences'),'utf8'));
  assert.equal(preferences.profile.exit_type,'Normal');
 };
}

test('standard restoration keeps synthetic session cookies across native UI close and connector restart',
 {skip:process.env.VOICE_SESSION_COOKIE_PROOF!=='true',timeout:120000},async t=>{
  const display=spawn('Xvfb',[':99','-screen','0','1280x900x24','-nolisten','tcp','-ac'],{stdio:'ignore'});
  t.after(()=>display.kill('SIGTERM'));
  for(let i=0;i<100;i++) {
   try {execFileSync('xdpyinfo',['-display',':99'],{stdio:'ignore'});break;}
   catch {await sleep(50);if(i===99)throw Error('Synthetic display unavailable');}
  }
  let phase='write';let issued=false;let setCookieCount=0;const observations=[];
  const server=createServer((request,response)=>{
   if(request.url.startsWith('/observe')) {
    const cookies=request.headers.cookie||'';
    observations.push({phase,session:/(?:^|; )synthetic-session=synthetic-only(?:;|$)/.test(cookies),
      persistent:/(?:^|; )synthetic-persistent=synthetic-only(?:;|$)/.test(cookies)});
    response.end('observed');return;
   }
   if(request.url==='/write'&&!issued) {
    issued=true;setCookieCount++;
    response.setHeader('Set-Cookie',['synthetic-session=synthetic-only; Path=/',
      'synthetic-persistent=synthetic-only; Path=/; Max-Age=3600']);
   }
   response.setHeader('Content-Type','text/html');
   response.end('<!doctype html><title>Synthetic session proof</title><script>fetch("/observe")</script>');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  const origin=`http://127.0.0.1:${server.address().port}`;
  for(const restore of [false,true]) {
   const directory=await mkdtemp(join(tmpdir(),'voice-session-proof-'));
   t.after(()=>rm(directory,{recursive:true,force:true}));
   await mkdir(join(directory,'profile','Default'),{recursive:true});
   // Compare the observed New Tab setting with ordinary Continue-where-you-left-off.
   await writeFile(join(directory,'profile','Default','Preferences'),JSON.stringify({session:{restore_on_startup:5},unrelated:{synthetic_marker:true}}));
   phase='warmup';issued=false;setCookieCount=0;observations.length=0;
   let close=await native(t,directory,origin+'/warmup',restore);
   await until(()=>observations.some(row=>row.phase==='warmup'));await close();
   // The authorized cloud profile is existing, so initialize this fixture first.
   phase='write';observations.length=0;
   close=await native(t,directory,origin+'/write',restore);
   await until(()=>observations.some(row=>row.phase==='write'&&row.session&&row.persistent));
   await close();
   phase='native-read';observations.length=0;
   close=await native(t,directory,origin+'/read',restore);
   await until(()=>observations.some(row=>row.phase==='native-read'));
   assert.ok(observations.every(row=>row.persistent&&row.session===restore),JSON.stringify({restore,observations}));
   assert.equal(setCookieCount,1); // Restored tabs never reset the test cookie.
   await close();
   if(restore) {
    phase='connector-read';observations.length=0;
    const target=join(directory,'profile','Default','Preferences');
    const prefs=JSON.parse(await readFile(target,'utf8'));
    assert.deepEqual(prefs.unrelated,{synthetic_marker:true});
    await writeFile(target,JSON.stringify({...prefs,session:{...prefs.session,restore_on_startup:5}}));
    const browser=new VoiceBrowser({directory,executablePath:'/usr/bin/chromium',allowedPhones:[],demoMode:true});
    try {
     await browser.start();await browser.page.goto(origin+'/read');
     await until(()=>observations.some(row=>row.phase==='connector-read'));
     assert.ok(observations.every(row=>row.session&&row.persistent));
     assert.equal(setCookieCount,1);
     const configured=JSON.parse(await readFile(target,'utf8'));
     assert.equal(configured.session.restore_on_startup,1);assert.deepEqual(configured.unrelated,{synthetic_marker:true});
    } finally {await browser.close();}
   }
  }
 });
