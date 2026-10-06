import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,readFile,writeFile,rm,symlink,stat} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {prepareSessionRestoration} from '../browser.mjs';

async function fixture(t,prefs) {
 const directory=await mkdtemp(join(tmpdir(),'voice-session-prefs-'));
 t.after(()=>rm(directory,{recursive:true,force:true}));
 const target=join(directory,'profile','Default','Preferences');
 await mkdir(join(directory,'profile','Default'),{recursive:true});
 if(prefs!==undefined)await writeFile(target,prefs);
 return {directory,target};
}

test('durable session restoration preserves unrelated preferences and is idempotent',async t=>{
 const prefs={session:{restore_on_startup:5,other:'preserved'},signin:{other:'preserved'},unrelated:{value:'synthetic-only'}};
 const {directory,target}=await fixture(t,JSON.stringify(prefs));
 await prepareSessionRestoration(directory);
 assert.deepEqual(JSON.parse(await readFile(target,'utf8')),{...prefs,session:{...prefs.session,restore_on_startup:1},signin:{...prefs.signin,allowed_on_next_startup:false}});
 assert.equal((await stat(target)).mode&0o777,0o600);
 const modified=(await stat(target)).mtimeMs;
 await prepareSessionRestoration(directory);assert.equal((await stat(target)).mtimeMs,modified);
});

test('a new dedicated profile receives the same standard startup setting',async t=>{
 const {directory,target}=await fixture(t);
 await prepareSessionRestoration(directory);
 assert.deepEqual(JSON.parse(await readFile(target,'utf8')),{session:{restore_on_startup:1},signin:{allowed_on_next_startup:false}});
});

test('existing restoration alone does not skip the browser-profile sign-in opt-out',async t=>{
 const {directory,target}=await fixture(t,'{"session":{"restore_on_startup":1},"signin":{"allowed_on_next_startup":true,"other":"preserved"}}');
 await prepareSessionRestoration(directory);
 assert.deepEqual(JSON.parse(await readFile(target,'utf8')),{session:{restore_on_startup:1},signin:{allowed_on_next_startup:false,other:'preserved'}});
});

for(const original of ['invalid JSON','[]','{"session":null}','{"signin":null}']) {
 test(`malformed preferences are held unchanged (${original})`,async t=>{
  const {directory,target}=await fixture(t,original);
  await assert.rejects(()=>prepareSessionRestoration(directory),{code:'profile_state_unavailable'});
  assert.equal(await readFile(target,'utf8'),original);
 });
}

for(const path of ['SingletonLock','Default/Preferences']) {
 test(`profile symlink/lock prevents preference mutation (${path})`,async t=>{
  const {directory,target}=await fixture(t,'{"session":{"restore_on_startup":5}}');
  const original=await readFile(target,'utf8');
  if(path==='Default/Preferences')await rm(target);
  await symlink('/synthetic-missing-target',join(directory,'profile',path));
  await assert.rejects(()=>prepareSessionRestoration(directory),{code:path==='SingletonLock'?'profile_in_use':'profile_state_unavailable'});
  if(path==='SingletonLock')assert.equal(await readFile(target,'utf8'),original);
 });
}
