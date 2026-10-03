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
  async function submit(form) {
    // Legacy form events cannot import cookies, even with stale connected flags.
    const input = form?.querySelector('[name="cookies"]');
    if (input) input.value = '';
    clearInput();
  }
  async function action(name) {
    if (!available() || !role || busy) return;
    clearInput();
    if (name === 'refresh') { await load(); render(); }
    if (name === 'pause' && preview() && status) await mutatePreview('/api/cloud-texting/pause', {paused: !status.paused});
  }
  function summary() {
    if (!available()) return null;
    return {connected:false, label:preview() ? 'Disconnected preview' : policyLabel};
  }
  function screen() {
    if (!available() || !role) return '';
    const queue = status?.queue || {};
    const held = status?.held_inbound || {};
    const heldCount = key => Number.isSafeInteger(held[key]) && held[key] >= 0 ? held[key] : 0;
    const counts = ['queued','dispatching','submitted','uncertain','rejected'].map(key => [key, Number.isSafeInteger(queue[key]) && queue[key] >= 0 ? queue[key] : 0]);
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
