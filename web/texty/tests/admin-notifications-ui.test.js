import test from 'node:test';
import assert from 'node:assert/strict';
import {createAdminNotifications, groupNotices, noticeViews, notificationLabel, reviewOutcomeLabel, textStatusLabel} from '../public/admin-notifications.js';

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

test('event groups separate old notices from upcoming consent holds without implying delivery',async()=>{
  const now=Date.parse('2026-10-06T18:00:00Z');
  const expired=row({event_id:1,event_title:'Synthetic Women’s Group 7 PM',starts_at:'2026-10-05T01:00:00Z',recipient_name:'Megan Example',reason:'The selected recipient session is expired.'});
  const held=row({event_id:2,starts_at:'2026-10-11T15:00:00Z',recipient_name:'George Example',reason:'Recipient consent is unavailable.'});
  const second=row({...held,id:'assignment:2:day_before',notice:'day_before',recipient_name:'Caroline Example'});
  const uncertain=row({...expired,provider_message_status:'uncertain',recipient_name:'Taylor Example'});
  assert.deepEqual(noticeViews(expired,now),{upcoming:false,attention:false,history:true});
  assert.deepEqual(noticeViews(held,now),{upcoming:true,attention:true,history:false});
  assert.equal(noticeViews(uncertain,now).attention,true);
  const flow=controller(async()=>page({generated_at:new Date(now).toISOString(),notifications:[expired,held,second,uncertain]}));
  await flow.load();
  assert.match(flow.panel(),/Upcoming<span>2<\/span>/);
  assert.match(flow.panel(),/Needs attention<span>3<\/span>/);
  assert.match(flow.panel(),/History<span>2<\/span>/);
  assert.doesNotMatch(flow.panel(),/Megan Example|Taylor Example/);
  assert.match(flow.panel(),/George Example|Caroline Example/);
  assert.equal((flow.panel().match(/class="notice-event"/g)||[]).length,1);
  assert.match(flow.panel(),/<details class="notice-row">/);
  assert.match(flow.panel(),/Held, not queued/);
  assert.doesNotMatch(flow.panel(),/Marked delivered|Delivery verified/);
  flow.filter('history');
  assert.match(flow.panel(),/Women’s Group 7 PM/);
  assert.doesNotMatch(flow.panel(),/Synthetic|George Example|Caroline Example/);
  flow.filter('attention');
  assert.doesNotMatch(flow.panel(),/Megan Example/);
  assert.match(flow.panel(),/Taylor Example/);
});

test('same-name events remain separate, sorted by date; missing dates stay visible',()=>{
  const now=Date.parse('2026-10-01T18:00:00Z');
  const early=row({event_id:2,recipient_name:'Zed Example'}), alpha=row({event_id:2,recipient_name:'Alex Example'});
  const late=row({event_id:3,starts_at:'2026-10-11T15:00:00Z'});
  const sameName=row({event_id:4}), undated=row({event_id:5,starts_at:null});
  const groups=groupNotices([late,early,undated,alpha,sameName],'upcoming',now);
  assert.equal(groups.length,4);
  assert.equal(groups[0].rows[0].recipient_name,'Alex Example');
  assert.equal(groups[0].rows[1].recipient_name,'Zed Example');
  assert.equal(groups.at(-1).startsAt,null);
  assert.equal(noticeViews(undated,now).attention,true);
});

test('manual placements without an automatic notice are neutral while reminders stay scheduled',()=>{
  const now=Date.parse('2026-10-06T18:00:00Z');
  const manual=row({starts_at:'2026-10-11T15:00:00Z',state:'not-required'});
  const reminder=row({...manual,notice:'day_before',state:'scheduled'});
  assert.equal(notificationLabel(manual),'No automatic notice');
  assert.deepEqual(noticeViews(manual,now),{upcoming:true,attention:false,history:false});
  assert.equal(notificationLabel(reminder),'Scheduled, not queued');
  assert.equal(groupNotices([manual,reminder],'attention',now).length,0);
  assert.equal(groupNotices([manual,reminder],'upcoming',now)[0].rows.length,2);
});

test('future cancelled notices without active delivery are neutral history, not upcoming attention',()=>{
  const now=Date.parse('2026-10-06T18:00:00Z');
  const cancelled=row({starts_at:'2026-10-18T15:00:00Z',cancelled:true,state:'cancelled',message_id:null});
  assert.equal(notificationLabel(cancelled),'Cancelled, no notice due');
  assert.deepEqual(noticeViews(cancelled,now),{upcoming:false,attention:false,history:true});
  assert.equal(groupNotices([cancelled],'upcoming',now).length,0);
  assert.equal(groupNotices([cancelled],'attention',now).length,0);
  assert.equal(groupNotices([cancelled],'history',now).length,1);
  const superseded={...cancelled,message_id:7,provider_message_status:'superseded'};
  assert.equal(notificationLabel(superseded),'Cancelled, no notice due');
  assert.deepEqual(noticeViews(superseded,now),{upcoming:false,attention:false,history:true});
  // Cancellation is an explicit API fact, never inferred from saved reason text.
  assert.equal(noticeViews(row({...cancelled,cancelled:false,state:'suppressed'}),now).attention,true);
  for (const status of ['queued','uncertain','failed','dispatching']) {
    const attempt=row({...cancelled,state:'held',message_id:7,provider_message_status:status});
    assert.equal(noticeViews(attempt,now).attention,true);
    assert.equal(noticeViews({...attempt,starts_at:'2026-10-05T15:00:00Z'},now).attention,true);
    assert.doesNotMatch(notificationLabel(attempt),/Cancelled, no notice due|Delivery verified/);
    if (status === 'queued') {
      assert.equal(notificationLabel(attempt),'Queued for Messages, cancellation needs checking');
      assert.doesNotMatch(notificationLabel(attempt),/not queued/);
    }
  }
  const submitted=row({...cancelled,state:'held',message_id:7,provider_message_status:'submitted'});
  assert.match(notificationLabel(submitted),/Submitted to Messages, delivery unverified/);
});

test('filters perform no backend writes, preserve selection after refresh, reset on logout and escape details',async()=>{
  const calls=[], renders=[];
  const flow=controller(async(...args)=>{calls.push(args);return page({notifications:[row({event_title:'<img src=x>',role:'<script>role</script>',reason:'<script>reason</script>',next_step:'<b>step</b>',starts_at:'2026-10-02T15:00:00Z'})]});},{render(){renders.push(true);}});
  await flow.load();flow.filter('history');flow.filter('invalid');
  assert.equal(calls.length,1);assert.equal(renders.length,1);
  assert.match(flow.panel(),/data-notification-filter="history" aria-pressed="true"/);
  assert.match(flow.panel(),/&lt;img src=x&gt;|&lt;script&gt;role/);
  assert.match(flow.panel(),/&lt;script&gt;reason&lt;\/script&gt;/);
  assert.match(flow.panel(),/&lt;b&gt;step&lt;\/b&gt;/);
  assert.doesNotMatch(flow.panel(),/<script>|<img src=x>|<b>step/);
  await flow.refresh();
  assert.match(flow.panel(),/data-notification-filter="history" aria-pressed="true"/);
  assert.deepEqual(calls,[['/api/notification-status?limit=100&offset=0'],['/api/notification-status?limit=100&offset=0']]);
  flow.reset();
  assert.match(flow.panel(),/data-notification-filter="upcoming" aria-pressed="true"/);
  assert.doesNotMatch(flow.panel(),/reason&lt;\/script&gt;/);
});
