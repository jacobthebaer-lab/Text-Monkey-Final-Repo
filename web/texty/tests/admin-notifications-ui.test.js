import test from 'node:test';
import assert from 'node:assert/strict';
import {createAdminNotifications, notificationLabel, reviewOutcomeLabel, textStatusLabel} from '../public/admin-notifications.js';

const row = overrides => ({id:'assignment:1:scheduled',notice:'scheduled',recipient_name:'Casey Example',role:'Greeter',event_title:'Sunday service',starts_at:'2026-10-04T15:00:00Z',state:'held',reason:'Scheduling is paused.',next_step:'Resume the saved schedule.',delivery_evidence:'not_recorded',...overrides});
const page = overrides => ({generated_at:'2026-10-03T18:00:00Z',read_only:true,notifications:[row()],next_offset:null,...overrides});
const controller = (api, overrides={}) => createAdminNotifications({api,getMode:()=> 'live',getToken:()=> 'fixture-token',getConfig:()=>({aiReady:true,macBridgeConnected:true,automationEnabled:false}),render(){},...overrides});

test('review receipts preserve suppression and distinguish queued from verified delivery',()=>{
  const blocked=reviewOutcomeLabel({delivery:'blocked_policy',notes:['Exact message: blocked_policy; suppressed after review']});
  assert.match(blocked,/Review saved.*Suppressed.*not queued/);
  assert.doesNotMatch(blocked,/Queued for Messages|Delivery verified/);
  assert.equal(textStatusLabel('queued_for_mac'),'Queued for Messages');
  assert.match(textStatusLabel('submitted'),/delivery unverified/);
  assert.match(reviewOutcomeLabel({}),/no queue receipt/);
  assert.match(notificationLabel(row({state:'queued',provider_message_status:'blocked_policy'})),/not queued/);
  assert.match(notificationLabel(row({state:'held',provider_message_status:'submitted'})),/delivery unverified/);
  assert.equal(notificationLabel(row({state:'verified-delivered'})),'Delivery unverified');
  assert.equal(notificationLabel(row({state:'verified-delivered',delivery_evidence:'mock_only'})),'Preview only, no real delivery');
  assert.doesNotMatch(blocked,/Exact message: blocked_policy/);
});

test('status ledger reads paginated assignment notices without writes and explains quiet signup',async()=>{
  const calls=[];
  const flow=controller(async(...args)=>{calls.push(args);return calls.length===1?page({next_offset:100}):page({notifications:[row({id:'assignment:2:day_before',notice:'day_before',state:'suppressed',recipient_name:'<script>',reason:'STOP received.',next_step:'No text will be queued.'})]});});
  await flow.load();
  assert.match(flow.panel(),/Scheduled notice/);assert.match(flow.panel(),/Scheduling is paused/);
  assert.match(flow.panel(),/saved completion wording is not automatically sent/);
  assert.match(flow.panel(),/These notices do not ask volunteers to confirm/);
  assert.match(flow.panel(),/Resume the saved schedule/);assert.match(flow.panel(),/Load more notices/);
  await flow.more();
  assert.deepEqual(calls,[['/api/notification-status?limit=100&offset=0'],['/api/notification-status?limit=100&offset=100']]);
  assert.match(flow.panel(),/Day-before reminder/);assert.match(flow.panel(),/Suppressed, not queued/);
  assert.match(flow.panel(),/Casey Example/);assert.match(flow.panel(),/&lt;script&gt;/);
  assert.doesNotMatch(flow.panel(),/<script>|Load more notices/);
});

test('disconnected preview has no status calls or live actions; failed backend stays unverified',async()=>{
  let calls=0;
  const offline=controller(async()=>{calls++;return page();},{getMode:()=> 'demo',getToken:()=>null});
  await offline.load();assert.equal(calls,0);assert.match(offline.panel(),/preview is disconnected/);
  assert.doesNotMatch(offline.panel(),/data-notification-refresh|data-notification-more/);
  const live=controller(async()=>{throw Error('Fixture unavailable');},{getConfig:()=>({aiReady:false})});
  await live.load();assert.match(live.panel(),/Disconnected/);assert.match(live.panel(),/Fixture unavailable/);
  assert.doesNotMatch(live.panel(),/Delivery verified|Queued for Messages/);
});

test('an account change discards in-flight rows and repeated loads issue one read',async()=>{
  let token='account-a',release,calls=0;
  const flow=controller(()=>{calls++;return new Promise(resolve=>{release=resolve;});},{getToken:()=>token});
  const pending=flow.load();await flow.load();assert.equal(calls,1);
  token='account-b';release(page());await pending;
  assert.doesNotMatch(flow.panel(),/Casey Example/);
});
