import test from "node:test";
import assert from "node:assert/strict";
import { seed, applyDemo, processDemoSignup } from "../public/domain.js";

test("cancellation approval reopens only the selected slot and cannot run twice", () => {
  const state = seed(),
    p = state.proposals[0];
  applyDemo(state, p);
  assert.equal(state.assignments.filter((a) => a.shift_id === "s1").length, 1);
  assert.equal(state.assignments.filter((a) => a.shift_id === "s2").length, 2);
  assert.equal(
    state.messages.filter((m) => m.direction === "outbound").length,
    1,
  );
  assert.throws(() => applyDemo(state, p), /already reviewed/);
});
test("an opt-out prevents future reply drafts even after coordinator review", () => {
  const state = seed(),
    p = state.proposals[0];
  state.optouts = [p.phone];
  applyDemo(state, p);
  assert.equal(
    state.messages.filter((m) => m.direction === "outbound").length,
    0,
  );
});
test("a text signup creates an unqualified profile without recorded consent", () => {
  const state = seed();
  const p = {
    id: "new",
    phone: "+12025550199",
    intent: "signup",
    first_name: "Alex",
    last_name: "Morgan",
    status: "pending",
    reply: "Thanks",
  };
  applyDemo(state, p);
  const v = state.volunteers.find((v) => v.phone === p.phone);
  assert.equal(v.consent, false);
  assert.equal(v.qualified, false);
  assert.equal(v.status, "pending");
});


test("signup completes with a name and consent entirely by text", () => {
 const state=seed(),phone="+15555550199";
 const before=state.proposals.length;
 assert.equal(processDemoSignup(state,phone,"JOIN"),true);
 assert.equal(processDemoSignup(state,phone,"Alex Morgan"),true);
 const v=state.volunteers.find(v=>v.phone===phone);
 assert.equal(v.consent,false);assert.equal(v.qualified,false);
 assert.equal(processDemoSignup(state,phone,"YES"),true);
 assert.equal(v.consent,true);assert.equal(v.status,"active");
 assert.equal(v.qualified,false);assert.equal(state.proposals.length,before);
});


test("offline signup collects interests and availability without granting clearance", () => {
 const state=seed(),phone="+12025550191";
 for (const text of ["JOIN", "Avery Sample", "YES", "Greeter", "Sundays 9am, twice a month"])
   assert.equal(processDemoSignup(state,phone,text),true);
 const volunteer=state.volunteers.find(v=>v.phone===phone);
 assert.equal(volunteer.onboarding_stage,"complete");
 assert.equal(volunteer.ministry,"Greeter");
 assert.equal(volunteer.availability,"Sundays 9am, twice a month");
 assert.equal(volunteer.qualified,false);
 assert.equal(state.assignments.some(a=>a.volunteer_id===volunteer.id),false);
});

test("initial command guidance is preserved while completion has no premature RSVP", () => {
 const state=seed(),phone="+12025550187";
 processDemoSignup(state,phone,"JOIN");
 assert.match(state.messages.at(-1).body,/STOP.*HELP/);
 processDemoSignup(state,phone,"Noah Synthetic");
 assert.match(state.messages.at(-1).body,/Reply YES/);
 for (const body of ["YES","ANY","Sundays all day"]){
   processDemoSignup(state,phone,body);
   assert.doesNotMatch(state.messages.at(-1).body,/STOP|HELP/);
 }
 assert.equal(state.messages.at(-1).body,"You’re all set, Noah! We’ve saved your preferences. When a shift matches, we’ll text you the details and ask if you can take it. 🐒");
 assert.doesNotMatch(state.messages.at(-1).body,/YES|NO/);
 const person=state.volunteers.find(v=>v.phone===phone);
 assert.equal(state.assignments.some(a=>a.volunteer_id===person.id),false);
});

test('sample booking cancellation and qualified replacement respect coverage and clearance', async () => {
  const {demoBooking} = await import('../public/domain.js');
  const state = seed();
  demoBooking(state, 's1', 'v1', 'cancel');
  assert.equal(state.assignments.filter(a=>a.shift_id==='s1').length, 1);
  assert.throws(()=>demoBooking(state,'s1','v3','book'), /qualified/);
  assert.throws(()=>demoBooking(state,'s1','v7','book'), /already booked/);
  demoBooking(state,'s1','v1','book');
  assert.equal(state.assignments.filter(a=>a.shift_id==='s1').length, 2);
  assert.throws(()=>demoBooking(state,'s1','v3','book'), /fully staffed/);
  assert.equal(state.messages.length, 1);
});

test('sample booking blocks withdrawn consent, stale clearance and overlapping shifts', async () => {
  const {demoBooking} = await import('../public/domain.js');
  const state = seed();
  demoBooking(state,'s1','v1','cancel');
  state.volunteers[0].background_check_until = '2025-01-01';
  assert.throws(()=>demoBooking(state,'s1','v1','book'), /current clearance/);
  state.volunteers[0].background_check_until = '2027-01-31';
  state.volunteers[0].consent = false;
  assert.throws(()=>demoBooking(state,'s1','v1','book'), /active and opted in/);
  state.volunteers[0].consent = true;
  state.shifts.push({...state.shifts[0], id:'overlap'});
  demoBooking(state,'overlap','v1','book');
  assert.throws(()=>demoBooking(state,'s1','v1','book'), /overlapping/);
  assert.equal(state.assignments.filter(a=>a.shift_id==='s1').length,1);
});

test('Text Monkey signup snapshots use full-body suffix without repeated commands or early RSVP', () => {
  const state=seed(), phone='+12025550192';
  const snapshots=[];
  for(const text of ['JOIN','Alex Sample','YES','Welcome','Sunday mornings']) {
    assert.equal(processDemoSignup(state,phone,text),true);
    snapshots.push(state.messages.at(-1).body);
  }
  assert.deepEqual(snapshots, [
    'Welcome to Text Monkey! What is your first and last name? Reply STOP to stop or HELP for help. 🐒',
    'Thanks, Alex! Reply YES to receive volunteer scheduling texts. 🐒',
    'What would you like to help with? Reply with a role or ministry, or ANY. 🐒',
    'When can you serve, and how often? For example: Sundays at 9am, twice a month. Or FLEXIBLE. 🐒',
    'You’re all set, Alex! We’ve saved your preferences. When a shift matches, we’ll text you the details and ask if you can take it. 🐒',
  ]);
  assert.ok(snapshots.every(body=>body.endsWith('🐒') && !body.includes('🐵') && body.length <= 600));
  assert.ok(snapshots.slice(1).every(body=>! /STOP|HELP/.test(body)));
  assert.ok(snapshots.slice(2).every(body=>! /Reply (YES|NO)/.test(body)));
});
