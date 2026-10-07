import test from 'node:test';
import assert from 'node:assert/strict';
import {createGoogleCalendar} from '../public/google-calendar.js';

function fixture() {
  let mode='live', token='synthetic', epoch=1;
  const data=new Map(), calls=[], nav=[];
  const status={configured:true,connected:true,account_email:'admin@example.test',calendar_id:'church',calendar_name:'Church'};
  let hold, failure, restrictedStorage=false;
  const ui=createGoogleCalendar({getMode:()=>mode,getToken:()=>token,getSessionEpoch:()=>epoch,render:()=>{},
    onChanged:async()=>calls.push('schedule-refreshed'),storage:()=>{
      if(restrictedStorage) throw new Error('Browser storage restricted');
      return {getItem:k=>data.get(k),setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)};
    },navigate:url=>nav.push(url),
    api:async(path,body)=>{
      calls.push([path,body]);
      if(path===failure) throw new Error('Google unavailable');
      if(hold) {const current=hold;hold=null;await current;}
      if(path.endsWith('/connect')) return {authorization_url:'https://accounts.google.com/o/oauth2/v2/auth?state=synthetic',flow_id:'synthetic-flow'};
      if(path.endsWith('/calendars')) return {calendars:[{id:'church',name:'Church <safe>'},{id:'output',name:'Text Monkey'}]};
      if(path.endsWith('/publish')) return {...status,publish_calendar_id:'output',publish_counts:{published:2,removed:0}};
      if(path.endsWith('/sync')) return {...status,last_sync_at:'2026-10-01T12:00:00Z',counts:{created:1,updated:0,protected:0,cancelled:0,skipped:0,unknown:0}};
      return status;
    }});
  return {ui,calls,nav,data,mode:value=>mode=value,session:()=>{token='other';epoch++;ui.reset();},hold:promise=>hold=promise,fail:path=>failure=path,restrictStorage:()=>restrictedStorage=true};
}

test('preview makes no API calls and sign-in controls are explicit',async()=>{
  const f=fixture();f.mode('demo');await f.ui.load();await f.ui.action('sync');
  assert.equal(f.calls.length,0);assert.match(f.ui.panel(),/Preview only/);
});

test('sync imports before publication, refreshes church state and escapes options',async()=>{
  const f=fixture();await f.ui.load();assert.match(f.ui.panel(),/Church &lt;safe&gt;/);
  await f.ui.action('sync');
  assert.deepEqual(f.calls.slice(-3),[['/api/google-calendar/sync',{}],'schedule-refreshed',['/api/google-calendar/publish',{}]]);
  assert.match(f.ui.panel(),/Calendar sync complete/);
  assert.doesNotMatch(f.ui.panel(),/value="output"/);
});

test('OAuth returns finalize with tab-held flow, and logout clears private state',async()=>{
  const f=fixture();await f.ui.action('connect');assert.equal(f.nav.length,1);
  await f.ui.returned('ready');assert.equal(f.data.size,0);
  assert.ok(f.calls.some(call=>Array.isArray(call) && call[0]==='/api/google-calendar/finish' && call[1].flow_id==='synthetic-flow'));
  f.session();assert.doesNotMatch(f.ui.panel(),/admin@example.test/);
});

test('a session change during import prevents publication with the next account',async()=>{
  const f=fixture();await f.ui.load();let resolve;
  f.hold(new Promise(r=>resolve=r));const running=f.ui.action('sync');
  f.session();resolve();await running;
  assert.ok(!f.calls.some(call=>call[0]==='/api/google-calendar/publish'));
  assert.doesNotMatch(f.ui.panel(),/admin@example.test/);
});

test('failed publication clears an earlier success notice',async()=>{
  const f=fixture();await f.ui.load();await f.ui.action('publish');
  assert.match(f.ui.panel(),/Schedule published to Google Calendar/);
  f.fail('/api/google-calendar/publish');await f.ui.action('publish');
  assert.match(f.ui.panel(),/Google unavailable/);
  assert.doesNotMatch(f.ui.panel(),/Schedule published to Google Calendar/);
  assert.doesNotMatch(f.ui.panel(),/Working…/);
});

test('failed publication after import keeps its receipt without claiming work is ongoing',async()=>{
  const f=fixture();await f.ui.load();f.fail('/api/google-calendar/publish');await f.ui.action('sync');
  assert.match(f.ui.panel(),/Google unavailable/);
  assert.match(f.ui.panel(),/1 new/);
  assert.doesNotMatch(f.ui.panel(),/Publishing the schedule|Calendar sync complete|Working…/);
});

test('restricted browser storage cannot interrupt logout cleanup or reveal a prior account',async()=>{
  const f=fixture();await f.ui.load();assert.match(f.ui.panel(),/admin@example.test/);
  f.restrictStorage();assert.doesNotThrow(()=>f.session());
  assert.doesNotMatch(f.ui.panel(),/admin@example.test|Church &lt;safe&gt;/);
});

test('restricted storage blocks OAuth navigation with a visible error',async()=>{
  const f=fixture();f.restrictStorage();await f.ui.action('connect');
  assert.equal(f.nav.length,0);assert.match(f.ui.panel(),/Browser storage restricted/);
  await f.ui.returned('denied');assert.match(f.ui.panel(),/permission was declined/);
});
