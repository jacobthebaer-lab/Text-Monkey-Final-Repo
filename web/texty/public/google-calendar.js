const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const flowKey = 'texty.google-calendar.flow';
const date = value => value ? new Date(value).toLocaleString() : 'Not yet';

export function createGoogleCalendar({api, getMode, getToken, getSessionEpoch, render, onChanged,
  storage = () => sessionStorage, navigate = url => location.assign(url)}) {
  let status = null, calendars = [], busy = false, error = '', notice = '', epoch;
  const active = () => getMode() === 'live' && !!getToken();
  const current = value => active() && value === getSessionEpoch();
  function forgetFlow() {
    try { storage().removeItem(flowKey); } catch { /* Browser storage restrictions must not interrupt logout. */ }
  }
  function reset() {
    status = null; calendars = []; busy = false; error = ''; notice = ''; epoch = undefined;
    forgetFlow();
  }
  async function run(operation) {
    if (!active() || busy) return;
    const startingEpoch = getSessionEpoch();
    epoch = startingEpoch; busy = true; error = ''; notice = ''; render();
    try { await operation(startingEpoch); }
    catch (e) { if (current(startingEpoch)) { error = e.message; notice = ''; } }
    finally { if (current(startingEpoch) && epoch === startingEpoch) { busy = false; render(); } }
  }
  async function list(startingEpoch) {
    const result = await api('/api/google-calendar/calendars');
    if (current(startingEpoch)) calendars = result.calendars;
  }
  async function load() {
    await run(async start => {
      const result = await api('/api/google-calendar');
      if (!current(start)) return;
      status = result;
      if (status.connected) await list(start);
    });
  }
  async function returned(result) {
    if (result === 'denied') { forgetFlow(); error = 'Google Calendar permission was declined. You can connect again.'; render(); return; }
    await run(async start => {
      const flow = storage().getItem(flowKey);
      if (!flow) throw new Error('Start connecting Google Calendar from this browser tab.');
      const result = await api('/api/google-calendar/finish', {flow_id:flow});
      if (!current(start)) return;
      forgetFlow(); status = result;
      notice = status.calendar_id ? 'Google account connected. Your source calendar is ready to sync.' : 'Google account connected. Choose your church calendar.';
      await list(start);
    });
  }
  async function action(name, calendarId) {
    if (name === 'refresh') return load();
    await run(async start => {
      if (name === 'connect') {
        const result = await api('/api/google-calendar/connect', {});
        if (!current(start)) return;
        const url = new URL(result.authorization_url);
        if (url.origin !== 'https://accounts.google.com' || url.pathname !== '/o/oauth2/v2/auth') throw new Error('Invalid Google sign-in destination.');
        storage().setItem(flowKey, result.flow_id); navigate(url.href); return;
      }
      if (name === 'select') {
        const result = await api('/api/google-calendar/select', {calendar_id:calendarId});
        if (current(start)) { status = result; notice = 'Source calendar saved. Ready to sync.'; }
      }
      if (name === 'disconnect') {
        const result = await api('/api/google-calendar/disconnect', {});
        if (current(start)) { status = result; calendars = []; forgetFlow(); notice = 'Google Calendar disconnected. Existing events are kept.'; }
      }
      if (name === 'sync' || name === 'publish') {
        if (name === 'sync') {
          const result = await api('/api/google-calendar/sync', {});
          if (!current(start)) return;
          status = result; notice = 'Import complete. Publishing the schedule…';
          await onChanged();
          if (!current(start)) return;
        }
        const result = await api('/api/google-calendar/publish', {});
        if (!current(start)) return;
        status = result; notice = name === 'sync' ? 'Calendar sync complete.' : 'Schedule published to Google Calendar.';
      }
    });
  }
  function panel() {
    const intro = '<p>Bring church events into Text Monkey and publish the schedule with staffing coverage to your own Text Monkey calendar in Google.</p>';
    if (getMode() !== 'live') return `<section class="panel settings-panel section"><h2>Google Calendar</h2>${intro}<p class="notice">Preview only. Sign in to connect your Google account and sync real events.</p></section>`;
    const disabled = busy ? 'disabled' : '';
    return `<section class="panel settings-panel section" aria-labelledby="google-calendar-title"><h2 id="google-calendar-title">Google Calendar</h2>${intro}
      ${error ? `<p class="error" role="alert">${esc(error)}</p>` : ''}${notice ? `<p role="status">${esc(notice)}</p>` : ''}
      ${!status ? `<button data-calendar-action="refresh" ${disabled}>Check calendar connection</button>` : !status.configured ? '<p class="notice">Google Calendar setup is needed. Ask your app owner to configure the Google connection.</p>' : !status.connected ? `<button class="primary" data-calendar-action="connect" ${disabled}>Connect Google Calendar</button>` : `
      <p>Connected as <strong>${esc(status.account_email)}</strong></p>
      <form id="google-calendar-form"><label for="google-calendar-source">Church calendar to import</label><select id="google-calendar-source" name="calendar_id" required ${disabled}><option value="">Choose a calendar</option>${calendars.filter(c => c.id !== status.publish_calendar_id).map(c => `<option value="${esc(c.id)}" ${c.id === status.calendar_id ? 'selected' : ''}>${esc(c.name)}${c.primary ? ' (primary)' : ''}</option>`).join('')}</select><p class="field-hint">Choose a church calendar. Imported events enter the shared church schedule. Timed events in the next eight weeks use your existing staffing rules. Assigned events with changed times or cancellations wait for review.</p><button ${disabled}>Save source calendar</button></form>
      <p>Published schedule: <strong>${status.publish_calendar_id ? 'Text Monkey calendar' : 'Created when you first publish'}</strong>. Includes event times and coverage counts.</p>
      <dl class="profile-details"><div><dt>Last import</dt><dd>${esc(date(status.last_sync_at))}</dd></div><div><dt>Last publication</dt><dd>${esc(date(status.last_publish_at))}</dd></div></dl>
      ${status.counts ? `<p>${esc(status.counts.created)} new, ${esc(status.counts.updated)} updated, ${esc(status.counts.protected)} held for review, ${esc(status.counts.cancelled)} cancelled, ${esc(status.counts.skipped)} skipped. ${esc(status.counts.unknown)} ${status.counts.unknown === 1 ? "needs" : "need"} a staffing recipe.</p>` : ''}
      ${status.publish_counts ? `<p>${esc(status.publish_counts.published)} ${status.publish_counts.published === 1 ? "event" : "events"} published, ${esc(status.publish_counts.removed)} removed.</p>` : ''}
      <div class="setup-actions"><button class="primary" data-calendar-action="sync" ${busy || !status.calendar_id ? 'disabled' : ''}>${busy ? 'Working…' : 'Sync now'}</button><button data-calendar-action="publish" ${disabled}>Publish schedule</button><button data-calendar-action="refresh" ${disabled}>Refresh calendars</button><button data-calendar-action="disconnect" ${disabled}>Disconnect</button></div><p class="field-hint">Sync runs when you choose it. Google source events stay unchanged. No guests are invited and no texts are sent. To remove Google’s saved authorization too, use your Google account’s connected-app settings.</p>`}</section>`;
  }
  return {reset, load, returned, action, panel};
}
