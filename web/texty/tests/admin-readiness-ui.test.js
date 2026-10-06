import test from 'node:test';
import assert from 'node:assert/strict';
import {adminReadiness} from '../public/admin-readiness.js';
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

test('readiness gives setup and mobile actions and explains owner-only connections', () => {
  const html = adminReadiness({connection_check_ready:false, ready:false, checks:[
    {code:'setup',label:'Church setup',ready:false,detail:'Finish your profile.',action:'setup'},
    {code:'recipient',label:'Your mobile',ready:false,detail:'Save your number.',action:'mobile'},
    {code:'gloo',label:'Gloo AI',ready:false,detail:'Gloo is disconnected.',action:'connection-help',next_step:'Have the owner restore Gloo.'},
  ]}, esc);
  assert.match(html, /data-page="setup"/);
  assert.match(html, /data-action="focus-admin-mobile"/);
  assert.match(html, /Have the owner restore Gloo/);
  assert.match(html, /Complete the steps below/);
  assert.match(html, /Refresh connection status/);
  assert.doesNotMatch(html, /Send me|simulate|data-action="send/);
});

test('one-time readiness does not claim paused scheduled updates are running', () => {
  const html = adminReadiness({connection_check_ready:true,ready:false,checks:[
    {code:'bridge',label:'Laptop online',ready:true,detail:'Checked in.'},
    {code:'scheduler',label:'Scheduled updates',ready:false,detail:'Scheduling is paused.',next_step:'Ask the owner to start scheduling.'},
  ],session_starts_at:'2026-10-03T16:00:00Z',session_expires_at:'2026-10-03T17:00:00Z'},esc);
  assert.match(html, /Ready for a one-time connection check/);
  assert.match(html, /Scheduled updates need attention/);
  assert.match(html, /Scheduling is paused/);
  assert.match(html, /1 connection check ready/);
  assert.match(html, /Messages session starts.*expires.*your local time/);
});

test('server readiness and next steps are escaped before rendering', () => {
  const html = adminReadiness({checks:[{ready:false,label:'<img>',detail:'<script>',next_step:'<button>',action:'connection-help'}]},esc);
  assert.doesNotMatch(html, /<img>|<script>|<button>/);
  assert.match(html, /&lt;script&gt;/);
});

test('missing actual events leads to Schedule without disabling a connection check', () => {
  const html = adminReadiness({connection_check_ready:true,ready:false,checks:[
    {code:'event_schedule',label:'Upcoming events',ready:false,detail:'No upcoming events are saved.',
      action:'schedule',next_step:'Review an actual future event <safely>.'},
  ]},esc);
  assert.match(html, /Ready for a one-time connection check/);
  assert.match(html, /Scheduled updates need attention/);
  assert.match(html, /data-page="schedule"/);
  assert.match(html, /Review an actual future event &lt;safely&gt;/);
  assert.doesNotMatch(html, /data-action="send/);
});
