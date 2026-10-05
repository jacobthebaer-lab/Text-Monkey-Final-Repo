const escape = (value) => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const policyLabel = 'Google Voice automation held';

export function createCloudTexting({api, getMode, getToken, getConfig, render, disconnectedPreview = false}) {
  let role = false, status = null, error = '', busy = false, generation = 0;
  const preview = () => disconnectedPreview && getMode() === 'demo';
  const available = () => (getMode() === 'live' || preview()) && Boolean(getToken()) && Boolean(getConfig().cloudTextingAvailable);
  const request = (path, body) => api(path, body, {keepSessionOnForbidden: true});
  const current = (version, token) => version === generation && token === getToken() && available();
  const clearInput = () => {
    // Discard any credential field left by an older page without reading it.
    const input = globalThis.document?.querySelector('#cloud-session-cookies');
    if (input) input.value = '';
  };
  function reset() { generation += 1; role = false; status = null; error = ''; busy = false; clearInput(); }
  function failure(cause) {
    if ([401, 403].includes(cause?.status)) { role = false; status = null; error = ''; }
    else error = preview() ? 'Sample cloud status could not be loaded.' : 'Saved cloud status could not be loaded. Google Voice automation remains held.';
  }
  async function load() {
    if (!available()) { reset(); return; }
    const version = generation, token = getToken();
    try {
      const identity = await request('/api/auth/me');
      if (!current(version, token)) return;
      role = identity.superadmin === true;
      if (!role) { status = null; error = ''; clearInput(); return; }
      const next = await request('/api/cloud-texting');
      if (!current(version, token)) return;
      status = next; error = '';
    } catch (cause) { if (current(version, token)) failure(cause); }
  }
  async function mutatePreview(path, body) {
    if (!preview() || !available() || !role || busy) return;
    const version = generation, token = getToken();
    busy = true; error = ''; render();
    try {
      const next = await request(path, body);
      if (current(version, token)) status = next;
    } catch (cause) { if (current(version, token)) failure(cause); }
    finally { if (current(version, token)) { busy = false; render(); } }
  }
  async function mutateDemo(path, body) {
    if (!available() || !role || busy || status?.demo_mode !== true || preview()) return;
    const version = generation, token = getToken();
    busy = true; error = ''; render();
    try {
      let next = await request(path, body);
      if (path.startsWith('/api/proposals/')) next = await request('/api/cloud-texting');
      if (current(version, token)) status = next;
    } catch (cause) {
      if (current(version, token)) {
        if ([401,403].includes(cause?.status)) failure(cause);
        else error = 'Demo step could not complete. Refresh its saved status before proceeding. An uncertain submission must not be retried.';
      }
    } finally { if (current(version, token)) { busy = false; render(); } }
  }
  async function submit(form) {
    if (status?.demo_mode === true && !preview()) {
      if (form?.id === 'cloud-demo-recipient-form') {
        const digits = (form.querySelector('[name="phone"]')?.value || '').replace(/\D/g, '');
        const phone = digits.length === 10 ? `+1${digits}` : `+${digits}`;
        await mutateDemo('/api/cloud-texting/demo/recipients', {phone, name:form.querySelector('[name="name"]')?.value?.trim() || 'Demo participant'});
        return;
      }
      if (form?.id === 'cloud-demo-window-form') {
        await mutateDemo('/api/cloud-texting/demo/window', {minutes:Number(form.querySelector('[name="minutes"]')?.value),submission_budget:Number(form.querySelector('[name="submission_budget"]')?.value)});
        return;
      }
      if (form?.id === 'cloud-demo-compose-form') {
        await mutateDemo('/api/cloud-texting/demo/compose', {phone:form.querySelector('[name="phone"]')?.value, instruction:form.querySelector('[name="instruction"]')?.value});
        return;
      }
    }
    const input = form?.querySelector('[name="cookies"]');
    let cookies;
    if (status?.demo_mode === true && !preview()) {
      try { cookies = JSON.parse(input?.value || ''); }
      catch { error = 'Provide a valid session cookie array for the configured dedicated sender.'; }
    }
    if (input) input.value = '';
    clearInput();
    if (cookies) await mutateDemo('/api/cloud-texting/session', {cookies});
    else render();
  }
  async function action(name) {
    if (!available() || !role || busy) return;
    clearInput();
    if (name === 'refresh') { await load(); render(); }
    if (status?.demo_mode === true && !preview()) {
      if (name === 'window-stop') await mutateDemo('/api/cloud-texting/demo/window/stop', {});
      if (name === 'intake') await mutateDemo('/api/cloud-texting/demo/intake', {});
      if (name === 'manual-pause') await mutateDemo('/api/cloud-texting/pause', {paused: !status.paused});
      if (name.startsWith('approve:')) {
        const selected = (status.pending_reviews || []).find(row => String(row.id) === name.slice(8));
        if (selected) await mutateDemo(`/api/proposals/${selected.id}/approve`, {content_hash:selected.content_hash});
      }
      if (name.startsWith('dispatch:')) {
        const selected = (status.reviewed_messages || []).find(row => String(row.id) === name.slice(9));
        if (selected) await mutateDemo('/api/cloud-texting/demo/dispatch', {message_id: selected.id, body_hash: selected.body_hash});
      }
    }
    if (name === 'pause' && preview() && status) await mutatePreview('/api/cloud-texting/pause', {paused: !status.paused});
  }
  function summary() {
    if (!available()) return null;
    return {connected:status?.demo_mode === true && status?.connection?.connected === true, label:preview() ? 'Disconnected preview' : status?.demo_mode === true ? 'Google Voice bounded demo' : policyLabel};
  }
  function screen() {
    if (!available() || !role) return '';
    const queue = status?.queue || {};
    const held = status?.held_inbound || {};
    const heldCount = key => Number.isSafeInteger(held[key]) && held[key] >= 0 ? held[key] : 0;
    const counts = ['queued','dispatching','submitted','uncertain','rejected'].map(key => [key, Number.isSafeInteger(queue[key]) && queue[key] >= 0 ? queue[key] : 0]);
    if (!preview() && status?.demo_mode === true) {
      const step = status.step_result;
      const result = ['intake','compose'].includes(step?.action) ? step.message : step?.action === 'dispatch' ?
        `Selected message status: ${step.status}. Submitted means visible in Google Voice, with device delivery unverified.` : '';
      const messages = Array.isArray(status.reviewed_messages) ? status.reviewed_messages : [];
      const participants = Array.isArray(status.participants) ? status.participants : [];
      const reviews = Array.isArray(status.pending_reviews) ? status.pending_reviews : [];
      return `<section class="panel settings-panel section cloud-texting-panel" aria-labelledby="cloud-texting-title">
        <div class="section-heading"><h2 id="cloud-texting-title">Google Voice bounded demo</h2><span class="pill amber">${status.demo_window?.active ? 'Church demo window active' : 'Manual steps'}</span></div>
        <p>Manual diagnostic steps are the default. An operator can enable a temporary church demo window to process natural replies and submit only exact approved texts. This candidate does not establish provider permission or competition certification.</p>
        ${error ? `<p class="error" role="alert">${escape(error)}</p>` : ''}
        ${result ? `<p role="status">${escape(result)}</p>` : ''}
        <dl class="profile-details"><div><dt>Connection</dt><dd>${escape(status.connection?.state)}</dd></div>
        <div><dt>Verified sender</dt><dd>${escape(status.connection?.account_email || 'Not yet verified')} ${escape(status.connection?.number || '')}</dd></div>
        <div><dt>Manual outgoing step</dt><dd>${status.paused ? 'Paused' : 'Enabled for one reviewed text at a time'}</dd></div>
        <div><dt>Gloo</dt><dd>${status.gloo_ready ? 'Configured; per-message composition proof required' : 'Unavailable; replies held'}</dd></div></dl>
        <div class="setup-actions"><button data-cloud-action="refresh" ${busy ? 'disabled' : ''}>Refresh saved status</button>
        <button data-cloud-action="intake" ${busy ? 'disabled' : ''}>Check inbox once</button>
        <button data-cloud-action="manual-pause" ${busy ? 'disabled' : ''}>${status.paused ? 'Enable manual send step' : 'Pause manual send step'}</button></div>
        <p class="field-hint">The first inbox check establishes a baseline and skips prior history. Check again within 90 seconds before each send step. Importing or reconnecting a session keeps outgoing steps paused and never checks the inbox.</p>
        <form id="cloud-session-form"><label>Dedicated sender session cookies<textarea id="cloud-session-cookies" name="cookies" autocomplete="off" spellcheck="false" required></textarea></label>
        <button type="submit" ${busy ? 'disabled' : ''}>Verify dedicated sender session</button></form>
        <h3>Temporary church demo window</h3><p>${status.demo_window?.active ? `Enabled until ${escape(status.demo_window.until)}` : 'Off. Startup and reconnect never resume it.'} Reserved submissions: ${Number(status.demo_window?.reserved_submissions) || 0} of ${Number(status.demo_window?.submission_budget) || 0}.</p>
        <form id="cloud-demo-window-form"><label>Minutes (1–30)<input name="minutes" type="number" min="1" max="30" value="15" required></label><label>Submission budget (1–1000)<input name="submission_budget" type="number" min="1" max="1000" value="100" required></label><button type="submit" ${busy ? 'disabled' : ''}>Enable church demo window</button></form>
        <button data-cloud-action="window-stop" ${busy || !status.demo_window?.active ? 'disabled' : ''}>Stop church demo window</button>
        <p class="field-hint">Only this requested window checks registered participant replies in the cloud. Each Gloo draft still requires exact approval, then at most one approved text is submitted per tick. Pause, reconnect, expiry, restart or uncertainty stops the window. No background fill, broad outreach or production transport runs.</p>
        <h3>Add a participant for one name invitation</h3>
        <form id="cloud-demo-recipient-form"><label>Mobile number<input name="phone" type="tel" autocomplete="off" placeholder="(303) 555-0123" required></label><label>Display name (optional)<input name="name" maxlength="80" autocomplete="off"></label>
        <button type="submit" ${busy ? 'disabled' : ''}>Register participant</button></form>
        <p class="field-hint">Registration records pending signup and a two-hour session. Their first-and-last-name reply after the reviewed initial invitation is opt-in. The first message says Text STOP to stop. Registration sends nothing and does not clear an opt-out.</p>
        ${participants.length ? `<ul>${participants.map(row=>`<li>${escape(row.name)} ${escape(row.phone)}: ${escape(row.consent_state === 'name_reply_opted_in' ? 'Name reply opted in' : 'Awaiting name reply')} until ${escape(row.expires_at)}</li>`).join('')}</ul>` : '<p>No registered participants yet.</p>'}
        <h3>Compose a demo text with Gloo</h3>
        <form id="cloud-demo-compose-form"><label>Participant<select name="phone" required>${participants.filter(row=>row.active).map(row=>`<option value="${escape(row.phone)}">${escape(row.name)} ${escape(row.phone)}</option>`).join('')}</select></label>
        <label>Demo text request<textarea name="instruction" maxlength="500" required placeholder="For a new participant, compose the approved name invitation. After opt-in, describe the tailored demo text."></textarea></label>
        <button type="submit" ${busy || !status.gloo_ready || !participants.some(row=>row.active) ? 'disabled' : ''}>Compose with Gloo</button></form>
        <h3>Review exact Gloo drafts</h3>${reviews.length ? reviews.map(row=>`<article class="section"><p>${escape(row.phone)}</p><p style="white-space:pre-wrap">${escape(row.body)}</p><button data-cloud-action="approve:${escape(row.id)}" ${busy ? 'disabled' : ''}>Approve this exact text</button></article>`).join('') : '<p>No draft awaits review.</p>'}
        <h3>Exact reviewed queued texts</h3>${messages.length ? messages.map(row => `<article class="section"><p>${escape(row.phone)}</p><p style="white-space:pre-wrap">${escape(row.body)}</p>
          <button data-cloud-action="dispatch:${escape(row.id)}" ${busy || status.paused || !status.live_enabled || !status.gloo_ready || !status.connection?.connected || !status.demo_inbox_fresh ? 'disabled' : ''}>Send this reviewed text</button></article>`).join('') : '<p>No exact reviewed queued text is available. Compose and approve a text through the existing review flow.</p>'}
        <dl class="profile-details cloud-queue">${counts.map(([key,count]) => `<div><dt>${escape(key)}</dt><dd>${count}</dd></div>`).join('')}</dl>
        <p class="field-hint">Submitted is a Google Voice UI acknowledgement, not device delivery. Uncertain submissions require manual review and are never automatically retried.</p></section>`;
    }
    if (preview()) return `<section class="panel settings-panel section cloud-texting-panel" aria-labelledby="cloud-texting-title">
      <div class="section-heading"><h2 id="cloud-texting-title">Cloud texting</h2><span class="pill amber">Disconnected preview</span></div>
      <p>Previewing superadmin controls with sample data. Google Voice, Gloo and live delivery are disconnected.</p>
      <p>Live Google Voice automation remains held by provider policy. These controls simulate sample queue changes only.</p>
      <dl class="profile-details"><div><dt>Google Voice</dt><dd>Not connected</dd></div><div><dt>Gloo AI</dt><dd>${heldCount('held_gloo') ? 'Simulated outage; sample replies held' : 'Not connected; samples are scripted'}</dd></div><div><dt>Real recipients</dt><dd>0</dd></div><div><dt>Sample queue</dt><dd>${Number.isSafeInteger(queue.queued) && queue.queued >= 0 ? queue.queued : 0}</dd></div><div><dt>Sample replies held for Gloo</dt><dd>${heldCount('held_gloo')}</dd></div><div><dt>Outgoing simulation</dt><dd>${status?.paused ? 'Paused' : 'Resumed, but disconnected'}</dd></div></dl>
      <div class="setup-actions"><button data-cloud-action="refresh" ${busy ? 'disabled' : ''}>Refresh preview</button>${status ? `<button data-cloud-action="pause" ${busy ? 'disabled' : ''}>${status.paused ? 'Simulate resume' : 'Simulate pause'}</button>` : ''}</div>
      <p class="field-hint">Resuming changes this sample queue only. No credentials are accepted, no messages can leave this preview, and no sample is marked delivered.</p></section>`;
    return `<section class="panel settings-panel section cloud-texting-panel" aria-labelledby="cloud-texting-title">
      <div class="section-heading"><h2 id="cloud-texting-title">Cloud texting</h2><span class="pill amber">${policyLabel}</span></div>
      <p>Google Voice automation is blocked by provider policy, including after account or ID approval. This provider does not access your Google account, read incoming messages or send texts.</p>
      <p><a href="https://support.google.com/voice/answer/9230450" target="_blank" rel="noopener noreferrer">Google Voice's acceptable use policy</a> prohibits script-based and automatic messaging. Google Voice remains a manual option. Automated cloud texting needs an approved SMS provider and recipient opt-in.</p>
      ${error ? `<p class="error" role="alert">${escape(error)}</p>` : ''}
      <dl class="profile-details"><div><dt>Google Voice</dt><dd>Provider policy hold</dd></div><div><dt>Account verification</dt><dd>Does not unlock automation</dd></div><div><dt>Outgoing delivery</dt><dd>Held</dd></div><div><dt>Gloo AI</dt><dd>${status?.gloo_ready ? 'Configuration saved; Google Voice processing remains held' : 'Gloo is required for future approved automated messaging'}</dd></div></dl>
      <div class="setup-actions"><button data-cloud-action="refresh" ${busy ? 'disabled' : ''}>Refresh saved status</button></div>
      <dl class="profile-details cloud-queue" aria-label="Inactive cloud message records">${counts.map(([key,count]) => `<div><dt>${escape({queued:'Saved queued records',dispatching:'Saved pending submissions',submitted:'Historical submission records',uncertain:'Needs manual review',rejected:'Rejected records'}[key])}</dt><dd>${count}</dd></div>`).join('')}</dl>
      <p class="field-hint">These records remain inactive. Historical submission records do not prove delivery. No account connection, cookie import or delivery resume is available for Google Voice.</p>
      <p class="field-hint">Saved incoming records held for Gloo: ${heldCount('held_gloo')}. Saved incoming records held after test expiry: ${heldCount('held_expired_session')}.</p>
      </section>`;
  }
  return {load, reset, screen, summary, submit, action};
}
