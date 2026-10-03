const escape = (value) => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));

export function parseSessionCookies(raw) {
  if (typeof raw !== 'string' || !raw.trim() || raw.length > 60000) throw new Error('Paste a Google session cookie array, up to 60 KB.');
  let cookies;
  try { cookies = JSON.parse(raw); } catch { throw new Error('Use a JSON array of Google session cookies.'); }
  if (!Array.isArray(cookies) || !cookies.length || cookies.length > 100 || cookies.some(cookie => !cookie || typeof cookie !== 'object' || typeof cookie.name !== 'string' || typeof cookie.value !== 'string' || typeof cookie.domain !== 'string')) {
    throw new Error('Use a JSON array of cookies with name, value and domain fields.');
  }
  return cookies;
}

const connectionLabels = {
  ready: 'Google Voice session connected', connected: 'Google Voice session connected',
  reconnect_required: 'Reconnect Google Voice', authentication_required: 'Reconnect Google Voice',
  verification_required: 'Finish Google verification', setup_required: 'Finish Google Voice setup',
  unreachable: 'Cloud connector unavailable', unavailable: 'Cloud connector unavailable',
  disabled: 'Cloud texting disabled', paused: 'Outgoing texts paused',
  live_disabled: 'Live texting disabled', gloo_unavailable: 'Gloo unavailable; texts held',
};

// Google session material is used for one request only. It never enters browser storage.
export function createCloudTexting({api, getMode, getToken, getConfig, render}) {
  let role = false, status = null, error = '', busy = false, generation = 0;
  const available = () => getMode() === 'live' && Boolean(getToken()) && Boolean(getConfig().cloudTextingAvailable);
  const request = (path, body) => api(path, body, {keepSessionOnForbidden: true});
  const current = (version, token) => version === generation && token === getToken() && available();
  const clearInput = () => {
    const input = globalThis.document?.querySelector('#cloud-session-cookies');
    if (input) input.value = '';
  };
  function reset() { generation += 1; role = false; status = null; error = ''; busy = false; clearInput(); }
  function failure(cause) {
    if ([401, 403].includes(cause?.status)) { role = false; status = null; error = ''; }
    else error = cause?.status === 422 || cause?.status === 400
      ? 'The session could not be accepted. Check the Google cookie export and try again.'
      : 'Cloud status could not be confirmed. Retry the check before changing the connection.';
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
  async function mutate(path, body) {
    if (!available() || !role || busy) return;
    const version = generation, token = getToken();
    busy = true; error = ''; render();
    try {
      const next = await request(path, body);
      if (current(version, token)) status = next;
    } catch (cause) { if (current(version, token)) failure(cause); }
    finally { if (current(version, token)) { busy = false; render(); } }
  }
  async function submit(form) {
    const input = form.querySelector('[name="cookies"]');
    let raw = input?.value || '';
    clearInput();
    if (input) input.value = '';
    if (!available() || !role || busy) { raw = ''; return; }
    try {
      const cookies = parseSessionCookies(raw);
      raw = '';
      await mutate('/api/cloud-texting/session', {cookies});
    } catch (cause) { raw = ''; error = cause.message; render(); }
  }
  async function action(name) {
    if (!available() || !role || busy) return;
    clearInput();
    if (name === 'refresh') { await load(); render(); }
    if (name === 'pause' && status) await mutate('/api/cloud-texting/pause', {paused: !status.paused});
  }
  function summary() {
    if (!available()) return null;
    if (!role || !status) return {connected:false, label:'Cloud connection not checked'};
    if (error) return {connected:false, label:'Cloud status unavailable'};
    const connected = status.connection?.connected === true && status.state === 'ready' && !status.paused && status.enabled === true && status.live_enabled === true;
    return {connected, label:connectionLabels[status.state] || 'Cloud connection needs attention'};
  }
  function screen() {
    if (!available() || !role) return '';
    const connection = status?.connection || {};
    const label = connectionLabels[status?.state] || 'Cloud connection not checked';
    const connectionLabel = connectionLabels[connection.state] || (connection.connected ? 'Google Voice session connected' : 'Google Voice connection not confirmed');
    const queue = status?.queue || {};
    const held = status?.held_inbound || {};
    const heldCount = key => Number.isSafeInteger(held[key]) && held[key] >= 0 ? held[key] : 0;
    const counts = ['queued','dispatching','submitted','uncertain','rejected'].map(key => [key, Number.isSafeInteger(queue[key]) && queue[key] >= 0 ? queue[key] : 0]);
    return `<section class="panel settings-panel section cloud-texting-panel" aria-labelledby="cloud-texting-title"><div class="section-heading"><h2 id="cloud-texting-title">Cloud texting</h2><span class="pill ${summary()?.connected ? 'green' : 'amber'}">${escape(label)}</span></div><p>Superadmin connection controls. The cloud server can process texts while your computer and this page are closed.</p>${error ? `<p class="error" role="alert">${escape(error)}</p>` : ''}<dl class="profile-details"><div><dt>Google Voice</dt><dd>${escape(connectionLabel)}</dd></div>${connection.account_email ? `<div><dt>Google account</dt><dd>${escape(connection.account_email)}</dd></div>` : ''}${connection.number ? `<div><dt>Texting number</dt><dd>${escape(connection.number)}</dd></div>` : ''}<div><dt>Gloo AI</dt><dd>${status?.gloo_ready ? 'Configured; replies still require a successful Gloo response' : 'Not ready; messages remain held'}</dd></div><div><dt>Approved test recipients</dt><dd>${Number.isSafeInteger(status?.test_recipients) ? status.test_recipients : 0}</dd></div></dl><div class="setup-actions"><button data-cloud-action="refresh" ${busy ? 'disabled' : ''}>Refresh cloud status</button>${status ? `<button data-cloud-action="pause" ${busy ? 'disabled' : ''}>${status.paused ? 'Resume outgoing texts' : 'Pause outgoing texts'}</button>` : ''}</div><p class="field-hint">Pausing holds outgoing texts; incoming STOP and review processing can continue. Resume does not enable live texting or background scheduling when they are disabled on the server. Existing consent, quiet hours and review rules still apply.</p><dl class="profile-details cloud-queue" aria-label="Cloud message queue">${counts.map(([key,count]) => `<div><dt>${escape({queued:'Queued',dispatching:'Submitting',submitted:'Submitted to Google Voice',uncertain:'Needs delivery check',rejected:'Rejected'}[key])}</dt><dd>${count}</dd></div>`).join('')}</dl><p class="field-hint">Incoming held for Gloo: ${heldCount('held_gloo')}. Incoming held after test expiry: ${heldCount('held_expired_session')}.</p><p class="field-hint">Submitted means Google Voice accepted the request. It does not prove delivery to the recipient’s phone. Uncertain sends are held for review to prevent duplicates.</p><details class="section"><summary>Connect or reconnect Google Voice</summary><p>First finish Google Voice signup in the intended Google account, including any mobile and ID verification Google requests. Call forwarding can be disabled and the linked mobile removed afterward in Google Voice settings.</p><p>Use a dedicated session for this Google account. Paste only its Google cookie JSON export below. These credentials grant account access; the form clears after every attempt and they are never saved in this browser’s storage. Do not enter your password or verification code.</p><form id="cloud-session-form" autocomplete="off"><label for="cloud-session-cookies">Google session cookies (JSON array)</label><textarea id="cloud-session-cookies" name="cookies" rows="5" maxlength="60000" autocomplete="off" autocapitalize="off" spellcheck="false" required></textarea><div class="setup-actions"><button class="primary" ${busy ? 'disabled' : ''}>${busy ? 'Checking connection…' : 'Connect Google session'}</button></div></form></details><p class="field-hint">Free hosting depends on provider allowances and available capacity. Google sign-in may expire and require reconnection.</p></section>`;
  }
  return {load, reset, screen, summary, submit, action};
}
