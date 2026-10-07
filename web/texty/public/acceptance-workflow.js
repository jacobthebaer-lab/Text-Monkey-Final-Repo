import {presentationText} from './admin-readiness.js';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Resolve the displayed church-local date, independent of the admin browser's zone.
// Require a unique instant; a skipped/repeated DST hour needs a different choice.
export function eventInstant(local, zone) {
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(local)) throw new Error('Choose the event date and time.');
  const anchor = Date.parse(local + ':00Z');
  if (!Number.isFinite(anchor)) throw new Error('Choose a valid date.');
  const formatter = new Intl.DateTimeFormat('en-CA', {timeZone:zone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
  const matches = [];
  for (let minutes = -14*60; minutes <= 14*60; minutes += 15) {
    const instant = new Date(anchor + minutes*60000);
    const parts = Object.fromEntries(formatter.formatToParts(instant).map(p => [p.type,p.value]));
    if (`${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}` === local) matches.push(instant.toISOString());
  }
  if (matches.length !== 1) throw new Error('This church-local time is invalid or repeats during a clock change. Choose another time.');
  return matches[0];
}

export function createAcceptanceWorkflow({api,getMode,getToken,getConfig,render}) {
  let snapshot = null, error = '', busy = false;
  const enabled = () => getMode() === 'live' && getToken() && getConfig().acceptanceEventAvailable;
  const reset = () => {snapshot = null; error = '';};
  async function load() {
    if (!enabled()) {reset(); return;}
    const token = getToken();
    try {const result = await api('/api/acceptance-event'); if (getToken() === token) {snapshot = result; error = '';}}
    catch (failure) {if (getToken() === token) {snapshot = null; error = failure.message;}}
  }
  function panel() {
    if (!enabled()) return '';
    if (!snapshot) return `<section class="panel settings-panel section"><h2>Event walkthrough</h2><p class="error" role="alert">${esc(presentationText(error || 'Loading the event.'))}</p><button class="quiet" data-acceptance-action="refresh">Refresh event</button></section>`;
    const s = snapshot, disabled = busy ? 'disabled' : '';
    const button = (action,label,extra='') => `<button class="quiet" data-acceptance-action="${action}" ${extra} ${disabled}>${label}</button>`;
    return `<section class="panel settings-panel section" aria-labelledby="event-test-title"><h2 id="event-test-title">Event walkthrough</h2>
      <p>Participant: <strong>${esc(s.participant)}</strong>. Times use ${esc(s.zone)}. This walkthrough changes only its one fictional event and participant assignment.</p>
      ${error ? `<p class="error" role="alert">${esc(presentationText(error))}</p>` : ''}
      ${!s.event && !s.reviews.length ? `<form id="acceptance-event-form"><div class="fields"><label>Fictional event title<input name="title" value="Volunteer acceptance" required maxlength="200"></label><label>Role<select name="role_id" required>${s.roles.map(r=>`<option value="${r.id}">${esc(r.name)}</option>`).join('')}</select></label><label>Event start<input name="start" type="datetime-local" required></label><label>Event end<input name="end" type="datetime-local" required></label></div><button class="primary" ${disabled}>Review event</button></form>` : ''}
      ${s.event ? `<p><strong>${esc(s.event.title)}</strong>, ${esc(new Date(s.event.starts_at).toLocaleString(undefined,{timeZone:s.zone}))}. Event ${s.event.id}${s.assignment_id ? `, assignment ${s.assignment_id}` : ''}.</p>` : ''}
      ${s.reviews.map(r=>`<article class="section"><h3>${r.kind === 'confirm_text' ? 'Exact AI reminder' : 'Record review'}</h3><p>${esc(presentationText(r.reason))} <strong>${esc(r.status)}</strong></p>${r.body ? `<pre class="message-body">${esc(r.body)}</pre>` : `<pre>${esc(JSON.stringify(r.after,null,2))}</pre>`}${r.status === 'pending' ? button('approve','Approve this exact action',`data-review-id="${r.id}" data-content-hash="${esc(r.content_hash)}"`) : ''}</article>`).join('')}
      <div class="setup-actions section">${button('refresh','Refresh event')}${s.event && !s.assignment_id ? button('assignment','Review participant assignment') : ''}${s.assignment_id ? button('prepare','Prepare due AI reminder') : ''}</div>
      ${s.reminder_state ? `<p>Reminder: ${esc(s.reminder_state)}.</p>` : ''}
      ${s.message ? `<article class="section"><h3>Reviewed reminder</h3><pre class="message-body">${esc(s.message.body)}</pre><p>Status: ${esc(s.message.status)}. Submission does not establish phone delivery.</p>${s.message.status === 'queued' ? `${button('dispatch','Submit this exact reminder')}${s.timer_available ? `<form id="acceptance-timer-form"><label>Send after this real time (your local time)<input name="due" type="datetime-local" required></label><button class="primary" ${disabled}>Arm this reminder only</button></form>` : ''}` : ''}</article>` : ''}
      <p>Background reminder: ${esc(s.timer.state || 'off')}. ${s.timer.enabled ? button('stop-timer','Stop this reminder job') : ''}</p>
      <p class="field-hint">Consent, availability, qualifications, capacity, quiet hours and fresh inbox checks still apply. Silence keeps the saved assignment. A cancellation or STOP holds its reminder. No general scheduler runs from these controls.</p></section>`;
  }
  async function perform(action, payload) {
    if (!enabled() || busy) return;
    busy = true; error = '';
    try {const token = getToken(); const result = await api('/api/acceptance-event/' + action, payload); if (getToken() === token) snapshot = result;}
    catch (failure) {error = failure.message;}
    finally {busy = false; render();}
  }
  const source = () => ({event_id:snapshot.event.id, assignment_id:snapshot.assignment_id});
  const message = () => ({...source(),message_id:snapshot.message.id,body_hash:snapshot.message.body_hash});
  async function action(button) {
    if (!enabled() || busy) return;
    const name = button.dataset.acceptanceAction;
    if (name === 'refresh') {await load(); render();}
    else if (name === 'approve') await perform('approve',{review_id:Number(button.dataset.reviewId),content_hash:button.dataset.contentHash});
    else if (name === 'assignment') await perform(name,{event_id:snapshot.event.id});
    else if (name === 'prepare') await perform(name,source());
    else if (name === 'dispatch') await perform(name,message());
    else if (name === 'stop-timer') await perform('timer',{enabled:false});
  }
  async function submit(form) {
    try {
      const data = Object.fromEntries(new FormData(form));
      if (form.id === 'acceptance-event-form') await perform('event',{title:data.title,role_id:Number(data.role_id),zone:snapshot.zone,
        starts_at:eventInstant(data.start,snapshot.zone),ends_at:eventInstant(data.end,snapshot.zone)});
      else if (form.id === 'acceptance-timer-form') {
        const due = new Date(data.due).toISOString();
        await perform('timer',{...message(),enabled:true,due_at:due});
      }
    } catch (failure) {error = failure.message; render();}
  }
  return {load,reset,panel,action,submit};
}
