const esc = value => String(value ?? '').replace(/[&<>"']/g, char =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const monthLabel = month => /^\d{4}-(0[1-9]|1[0-2])$/.test(month || '')
  ? new Date(`${month}-01T12:00:00Z`).toLocaleDateString(undefined, {month:'long',year:'numeric',timeZone:'UTC'}) : month;

export function normalizeCollection(row) {
  const excluded = row.scope?.excluded_counts || {};
  return {...row, review_hash:row.content_hash, recipients:row.scope?.recipients || [], recipient_count:row.scope?.recipient_count, excluded,
    excluded_count:Object.values(excluded).reduce((total, count) => total + Number(count || 0), 0),
    scope_detail:`Shared church scheduling scope · ${row.scope?.timezone || 'Church timezone'}. Excludes people without current consent, available ask budget, or eligibility; saved availability and care holds are respected.`,
    pending_text_reviews:row.text_review_ids?.length || 0,
    held:row.composition_status === 'held' ? (row.held_reason || row.hold_reason || row.held || 'Text preparation is held. Restore Gloo or the Messages prerequisites, then retry preparation. Individual texts still require review.') : '',
  };
}

export function planningAdapter(api) {
  const path = '/api/planning/availability-collections';
  const unpack = result => normalizeCollection(result.collection);
  return {
    async list() { const result = await api(path); return result.collections.map(normalizeCollection); },
    async request(month, request_id) { return unpack(await api(path, {month,request_id})); },
    async decide(id, decision, hash) {
      return unpack(await api(`${path}/${encodeURIComponent(id)}/${decision}`, {content_hash:hash}));
    },
  };
}

export function collectionCard(row, busy = false) {
  const pending = row.status === 'pending';
  const recipients = row.recipients || [];
  const canDecide = pending && !!row.review_hash && !busy && !row.stale;
  const preparation = row.status === 'approved' && ['not_started','reviews_pending','held'].includes(row.composition_status);
  const preparationLabel = row.composition_status === 'held' ? 'Retry preparing texts'
    : row.composition_status === 'reviews_pending' ? 'Prepare next text for review' : 'Prepare texts for review';
  const states = {pending:'Needs your approval',approved:'Collection approved',rejected:'Collection rejected',expired:'Review expired'};
  return `<article class="section" aria-labelledby="collection-${esc(row.id)}"><h3 id="collection-${esc(row.id)}">${esc(monthLabel(row.month))} availability</h3>
    <p><strong>${esc(states[row.status] || row.status)}</strong></p>
    <p>${esc(row.recipient_count ?? recipients.length)} volunteer${(row.recipient_count ?? recipients.length) === 1 ? '' : 's'} in the reviewed collection scope${row.excluded_count ? ` · ${esc(row.excluded_count)} excluded` : ''}.</p>
    ${row.scope_detail ? `<p>${esc(row.scope_detail)}</p>` : ''}
    ${row.excluded_count ? `<details><summary>Why people are excluded</summary><ul>${Object.entries(row.excluded || {}).filter(([,count]) => count > 0).map(([reason,count]) => `<li>${esc(reason.replaceAll('_',' '))}: ${esc(count)}</li>`).join('')}</ul></details>` : ''}
    ${pending && row.expires_at ? `<p class="field-hint">Review expires ${esc(new Date(row.expires_at).toLocaleString())} (your local time). A changed scope needs a new review.</p>` : ''}
    ${row.status === 'approved' && row.authorization_expires_at ? `<p class="field-hint">Collection authorization expires ${esc(new Date(row.authorization_expires_at).toLocaleString())} (your local time).</p>` : ''}
    <details ${pending ? 'open' : ''}><summary>Review recipients</summary><ul>${recipients.map(v => `<li>${esc(v.name)} <span class="muted">${esc(v.phone)}</span></li>`).join('') || '<li>No eligible recipients in this scope.</li>'}</ul></details>
    ${row.held ? `<p class="notice" role="status">${esc(row.held)}</p>` : ''}
    ${row.retry_at ? `<p class="field-hint">Preparation can retry after ${esc(new Date(row.retry_at).toLocaleString())} (your local time).</p>` : ''}
    ${row.status === 'approved' && Number.isInteger(row.remaining_recipient_count) ? `<p class="field-hint">${row.remaining_recipient_count} recipient${row.remaining_recipient_count === 1 ? '' : 's'} ready for text preparation. Held preparation is shown separately above.</p>` : ''}
    ${row.stale ? '<p class="error">This scope changed. Use the month form to request a new review.</p>' : ''}
    ${pending ? `<p>Approve this month and recipient scope first. Approval saves the collection; it does not prepare or send texts.</p><div class="setup-actions section"><button class="primary" data-planning-id="${esc(row.id)}" data-planning-decision="approve" data-planning-hash="${esc(row.review_hash)}" ${!canDecide || !recipients.length ? 'disabled' : ''}>Approve collection</button><button data-planning-id="${esc(row.id)}" data-planning-decision="reject" data-planning-hash="${esc(row.review_hash)}" ${!canDecide ? 'disabled' : ''}>Reject collection</button></div>` : ''}
    ${row.status === 'approved' ? `<p>Collection approval is recorded. ${row.pending_text_reviews || 0} individual text review${row.pending_text_reviews === 1 ? '' : 's'} prepared. Approval of the collection does not approve these texts. Queueing and delivery are shown separately in Messages.</p>${row.composition_status === 'no_remaining_recipients' ? '<p>No remaining recipients need a new availability request.</p>' : '<p>Prepare one text at a time with Gloo, then review its exact recipient and body in Messages.</p>'}<div class="setup-actions section"><button class="quiet" data-page="messages">Review individual texts</button>${preparation ? `<button class="quiet" data-planning-id="${esc(row.id)}" data-planning-decision="retry" data-planning-hash="${esc(row.review_hash)}" ${busy || !row.review_hash || row.stale ? 'disabled' : ''}>${preparationLabel}</button>` : ''}</div>` : ''}
  </article>`;
}

export function createPlanningWorkflows({adapter, getMode, getToken, render, onChanged = async () => {}}) {
  let rows = [], month = '', busy = false, loaded = false, error = '', notice = '', requestId = '', requestMonth = '';
  const signedIn = () => getMode() === 'live' && !!getToken();
  const reset = () => { rows = []; month = ''; loaded = false; error = notice = requestId = requestMonth = ''; };
  const sameAccount = token => signedIn() && token === getToken();
  const merge = row => { rows = [row, ...rows.filter(old => String(old.id) !== String(row.id))]; };
  async function load({preserveError = false} = {}) {
    if (!signedIn()) { reset(); return; }
    const token = getToken();
    try {
      const result = await adapter.list();
      if (!sameAccount(token)) { reset(); return; }
      rows = result; loaded = true; if (!preserveError) error = '';
    } catch (failure) {
      if (sameAccount(token)) { rows = []; loaded = false; error = failure.message; }
    }
  }
  async function mutate(operation, {newRequest = false, id = null} = {}) {
    if (!signedIn()) { reset(); throw new Error('Sign in to the connected admin console before requesting a collection.'); }
    if (busy) return;
    const token = getToken(); busy = true; error = notice = ''; render();
    try {
      const result = await operation();
      if (!sameAccount(token)) { reset(); return; }
      merge(result); loaded = true;
      if (newRequest) requestId = '';
      notice = result.status === 'approved' ? (result.composition_status === 'not_started' ? 'Collection scope approved. Choose Prepare texts for review when ready.' : result.held ? 'Collection approved. Text preparation is held; follow the status below.' : 'Collection approved. Review each individual text in Messages.')
        : result.status === 'rejected' ? 'Collection rejected. No collection was started.'
          : 'Review the proposed month and recipients before approving.';
      await onChanged();
    } catch (failure) {
      if (sameAccount(token)) {
        error = failure.message;
        if (failure.status === 409) requestId = '';
        await load({preserveError:true});
        if (failure.status === 409 && id !== null) rows = rows.map(row => String(row.id) === String(id) ? {...row,stale:true} : row);
      }
    } finally { busy = false; if (sameAccount(token)) render(); }
  }
  return {
    load, reset,
    setMonth(value) { month = value; },
    async request(value) {
      if (busy) return;
      month = value;
      if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month || '')) { error = 'Choose a specific month.'; render(); return; }
      if (requestMonth !== month) { requestId = ''; requestMonth = month; }
      requestId ||= crypto.randomUUID();
      await mutate(() => adapter.request(month,requestId), {newRequest:true});
    },
    async decide(id, hash, decision) {
      const row = rows.find(row => String(row.id) === String(id));
      const allowed = decision === 'retry' ? row?.status === 'approved' && ['not_started','reviews_pending','held'].includes(row.composition_status)
        : row?.status === 'pending' && ['approve','reject'].includes(decision);
      if (!allowed || row.stale || !hash || hash !== row.review_hash) {
        error = 'This review changed. Refresh and review the current month and recipients.'; render(); return;
      }
      await mutate(() => adapter.decide(id, decision, hash), {id});
    },
    panel() {
      if (!signedIn()) return '<section class="panel settings-panel section"><h2>Collect monthly availability</h2><p>This preview is disconnected. Open the signed-in admin console to request and review a real collection.</p></section>';
      return `<section class="panel settings-panel section" aria-labelledby="planning-heading"><h2 id="planning-heading">Collect monthly availability</h2><p>Choose a month, review who will be asked, then approve or reject the collection. After approval, prepare each text explicitly with Gloo and review it separately in Messages.</p>
        ${error ? `<p class="error" role="alert">${esc(error)}</p>` : ''}${notice ? `<p class="notice" role="status">${esc(notice)}</p>` : ''}
        <form id="planning-month-form"><label for="planning-month">Month to collect</label><input id="planning-month" name="month" type="month" value="${esc(month)}" required ${busy ? 'disabled' : ''}><p class="field-hint">Choose the current month or a future month within the next year. Requesting an updated scope replaces the previous collection and its unapproved text reviews.</p><div class="setup-actions section"><button class="primary" ${busy || !loaded ? 'disabled' : ''}>${busy ? 'Working…' : 'Review collection scope'}</button></div></form>
        <div class="setup-actions section"><button class="quiet" data-planning-refresh ${busy ? 'disabled' : ''}>Refresh collections</button></div>
        ${rows.map(row => collectionCard(row, busy)).join('') || (loaded ? '<p class="field-hint">No collections have been requested yet.</p>' : '<p class="field-hint">Collection controls become available after the connected backend loads.</p>')}</section>`;
    },
  };
}
