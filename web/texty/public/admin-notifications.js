import {presentationText} from './admin-readiness.js';
import {churchLabel} from './church-presentation.js';
export function schedulingState(config = {}) {
  if (config.automationEnabled !== true) return 'paused';
  if (config.automationRunning === true) return 'running';
  return config.automationRunning === false ? 'paused' : 'unverified';
}

const esc = value => String(value ?? '').replace(/[&<>"']/g, char =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

export function textStatusLabel(status) {
  return ({scheduled:'Scheduled, not queued',held:'Held, not queued',
    'awaiting-review':'Awaiting review, not queued',queued:'Queued for Messages',queued_for_mac:'Queued for Messages',
    submitted:'Submitted to Messages, delivery unverified',sent:'Sent, delivery unverified',
    delivered:'Marked delivered', 'verified-delivered':'Delivery verified',
    suppressed:'Suppressed, not queued',blocked_policy:'Suppressed by conversation rules, not queued',
    blocked_opt_out:'Blocked by STOP, not queued',blocked_sensitive:'Blocked by a care hold, not queued',
    blocked_eligibility:'Blocked by eligibility, not queued',blocked_budget:'Blocked by ask limits, not queued',
    blocked_style:'Blocked by message wording, not queued',blocked_transport:'Blocked by Messages, not queued',
    blocked_native_route:'Held by Messages routing, no native send attempted',
    blocked_test_session:'Blocked by Messages session, not queued',blocked_confirmation:'Awaiting review, not queued',
    held_for_approval:'Awaiting review, not queued',held_quiet_hours:'Held for quiet hours, not queued',
    superseded:'Superseded, not queued',rejected:'Rejected, not queued',not_queued:'Not queued',
    dispatching:'Delivery in progress, unverified',uncertain:'Delivery uncertain, needs checking',
    simulated:'Preview only, no real delivery',draft:'Draft, not queued',failed:'Delivery failed',
    cancelled:'Cancelled, no notice due','not-required':'No automatic notice',paused:'Paused',disconnected:'Disconnected'}[status] || 'Status unverified');
}

export function reviewOutcomeLabel(result) {
  if (!result?.delivery) return 'Review saved. Check the current text status; no queue receipt was returned.';
  const label = textStatusLabel(result.delivery);
  const details = (result.notes || []).map(note => String(note).replace(/^Exact message: [^;]*(?:;\s*)?/, '')).filter(Boolean).join(' ');
  return `Review saved. ${label}.${details ? ' '+details : ''}`;
}

export const noticeTypeLabel = notice => notice === 'day_before' ? 'Day-before reminder' : notice === 'scheduled' ? 'Scheduled notice' : 'Shift notice';
export function notificationLabel(row) {
  if (row.delivery_evidence === 'mock_only') return textStatusLabel('simulated');
  const status = row.provider_message_status;
  if (row.cancelled === true && status === 'queued')
    return 'Queued for Messages, cancellation needs checking';
  if (row.state === 'cancelled' &&
      !['queued','dispatching','submitted','sent','delivered','uncertain','failed'].includes(status))
    return textStatusLabel('cancelled');
  if (status?.startsWith('blocked_') || status === 'superseded') return textStatusLabel(status);
  // The current API records mock_only/not_recorded, neither is native proof.
  if (row.state === 'verified-delivered') return 'Delivery unverified';
  if (['submitted','sent','uncertain','failed','dispatching'].includes(status)) return textStatusLabel(status);
  return textStatusLabel(row.state);
}

// Event time sorts the ledger, never determines whether a text was sent.
export function noticeViews(row, now) {
  const start = Date.parse(row.starts_at);
  const cancelledHistory = row.cancelled === true && row.state === 'cancelled' &&
    !['queued','dispatching','uncertain','failed','submitted','sent','delivered'].includes(row.provider_message_status);
  const historical = cancelledHistory || (Number.isFinite(start) && start <= now);
  const status = notificationLabel(row);
  const unresolvedDelivery = (row.cancelled === true && row.provider_message_status === 'queued') ||
    /uncertain|failed|delivery in progress/i.test(status);
  const needsAttention = unresolvedDelivery || (!historical &&
    /held|blocked|suppressed|awaiting review|unverified$|status unverified/i.test(status));
  return {upcoming:!historical, attention:needsAttention, history:historical};
}

export function groupNotices(rows, view, now) {
  const groups = new Map();
  for (const row of rows) {
    if (!noticeViews(row, now)[view]) continue;
    const key = JSON.stringify([row.event_id ?? row.event_title, row.starts_at]);
    if (!groups.has(key)) groups.set(key, {title:row.event_title, startsAt:row.starts_at, rows:[]});
    groups.get(key).rows.push(row);
  }
  const noticeOrder = notice => ({scheduled:0,day_before:1})[notice] ?? 2;
  for (const group of groups.values()) group.rows.sort((a,b) =>
    String(a.recipient_name).localeCompare(String(b.recipient_name)) ||
    String(a.role).localeCompare(String(b.role)) || noticeOrder(a.notice)-noticeOrder(b.notice));
  return [...groups.values()].sort((a,b) => {
    const left=Date.parse(a.startsAt), right=Date.parse(b.startsAt);
    if (!Number.isFinite(left)) return Number.isFinite(right) ? 1 : 0;
    if (!Number.isFinite(right)) return -1;
    return view === 'history' ? right-left : left-right;
  });
}

export function createAdminNotifications({api,getMode,getToken,getSessionEpoch=()=>getToken(),getConfig,render}) {
  let snapshot = null, error = '', loading = false, view = 'upcoming', generation=0, owner=getSessionEpoch();
  const connected = () => getMode() === 'live' && !!getToken();
  const reset = () => { generation++;snapshot = null; error = ''; loading=false;view = 'upcoming';owner=getSessionEpoch(); };
  const ensureAccount=()=>{if(owner!==getSessionEpoch())reset();};
  async function load({more = false} = {}) {
    ensureAccount();
    if (!connected()) { reset(); return; }
    if (loading) return;
    const epoch=getSessionEpoch(), version=generation; loading = true;
    const current=()=>connected() && epoch===getSessionEpoch() && version===generation;
    try {
      const offset = more && Number.isInteger(snapshot?.next_offset) ? snapshot.next_offset : 0;
      const result = await api(`/api/notification-status?limit=100&offset=${offset}`);
      if (current()) {
        const rows = more ? [...(snapshot?.notifications || []), ...(result.notifications || [])] : result.notifications;
        snapshot = {...result, notifications:rows}; error = '';
      }
    } catch (failure) {
      if (current()) { snapshot = null; error = failure.message; }
    } finally { if(current())loading = false; }
  }
  const when = (value, options) => Number.isFinite(Date.parse(value))
    ? esc(new Date(value).toLocaleString(undefined,options)) : 'Not scheduled';
  const dueWhen = value => when(value,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  const eventWhen = value => when(value,{weekday:'short',month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'});
  function noticeRow(row) {
    const status=notificationLabel(row);
    const tone=/held|blocked|suppressed|awaiting|uncertain|failed|status unverified/i.test(status) ? 'attention' : 'neutral';
    return `<details class="notice-row"><summary class="notice-row-summary">
      <span class="notice-person"><strong>${esc(churchLabel(row.recipient_name))}</strong><span>${esc(churchLabel(row.role))}</span></span>
      <span class="notice-type"><span class="notice-mobile-label">Notice</span>${esc(noticeTypeLabel(row.notice))}</span>
      <span class="notice-due"><span class="notice-mobile-label">Due</span>${dueWhen(row.due_at)}</span>
      <span class="notice-status notice-status-${tone}">${esc(status)}</span><span class="notice-expand" aria-hidden="true">⌄</span>
      </summary><div class="notice-detail"><p><strong>Status:</strong> ${esc(status)}</p>
      ${row.reason ? `<p>${esc(presentationText(row.reason))}</p>` : ''}
      ${row.next_step ? `<p><strong>Next step:</strong> ${esc(presentationText(row.next_step))}</p>` : ''}
      <p class="field-hint">Notice due: ${when(row.due_at)}. Event: ${when(row.starts_at)}. Times are local to this browser.</p>
      ${row.state === 'awaiting-review' ? '<button class="quiet small" data-page="volunteers">Review exact text</button>' : ''}</div></details>`;
  }
  function panel() {
    ensureAccount();
    const introduction = '<details class="notice-help"><summary>How shift notices work</summary><p><strong>Scheduled notice</strong> for a recorded shift, followed by one <strong>Day-before reminder</strong>. These notices do not ask volunteers to confirm by text. Signup preferences are saved quietly; the saved completion wording is not automatically sent.</p></details>';
    if (!connected()) return `<section class="panel settings-panel section"><h2>Shift notices</h2>${introduction}<p class="notice">This preview is disconnected. No notices are queued or delivered here.</p></section>`;
    const config = getConfig();
    const scheduler = schedulingState(config);
    const runtime = (scheduler === 'running' ? 'Scheduling is running. Native delivery still needs verification.'
      : scheduler === 'paused' ? 'Scheduling is paused. Planned notices are not proof of queued texts.'
        : 'Scheduling has not been verified. Planned notices are not proof of queued texts.')
      + (!config.aiReady || !config.macBridgeConnected ? ' AI or the laptop Messages connection is disconnected.' : '');
    const rows = snapshot?.notifications || [];
    const checked=Date.parse(snapshot?.generated_at), now=Number.isFinite(checked) ? checked : Date.now();
    const views=[['upcoming','Upcoming'],['attention','Needs attention'],['history','History']];
    const counts=Object.fromEntries(views.map(([key])=>[key,rows.filter(row=>noticeViews(row,now)[key]).length]));
    const groups=groupNotices(rows,view,now);
    const empty=({upcoming:'No upcoming notices in the loaded results.',attention:'No notices need attention in the loaded results.',history:'No historical notices in the loaded results.'})[view];
    return `<section class="panel settings-panel section notice-ledger" aria-labelledby="shift-notices-heading">
      <div class="notice-heading"><div><h2 id="shift-notices-heading">Shift notices</h2><p class="field-hint">Notice status by event. Expand a person for details and next steps.</p></div>
      <button class="quiet" data-notification-refresh ${loading ? 'disabled' : ''}>${loading ? 'Checking…' : 'Refresh notice status'}</button></div>
      <p class="notice-runtime" role="status">${esc(runtime)}</p>
      ${error ? `<p class="error" role="alert">Could not check notice status: ${esc(presentationText(error))}</p>` : ''}
      <div class="notice-filters" role="group" aria-label="Filter shift notices">${views.map(([key,label])=>`<button data-notification-filter="${key}" aria-pressed="${view===key}">${label}<span>${counts[key]}</span></button>`).join('')}</div>
      <div class="notice-results">${groups.map(group=>{
        const attention=group.rows.filter(row=>noticeViews(row,now).attention).length;
        return `<section class="notice-event"><div class="notice-event-heading"><div><h3>${esc(churchLabel(group.title))}</h3><p>${eventWhen(group.startsAt)}</p></div><span class="notice-event-count">${group.rows.length} ${group.rows.length===1?'notice':'notices'}${attention ? ` · ${attention} need attention` : ''}</span></div>
        <div class="notice-columns" aria-hidden="true"><span>Person / role</span><span>Notice</span><span>Due</span><span>Status</span><span></span></div>
        ${group.rows.map(noticeRow).join('')}</section>`;
      }).join('') || `<p class="notice-empty">${!snapshot ? 'Live status is unavailable until the connected backend responds.' : rows.length ? empty : 'No shift notices were returned. This does not establish that texts were sent.'}</p>`}</div>
      ${Number.isInteger(snapshot?.next_offset) ? `<div class="setup-actions section"><button class="quiet" data-notification-more ${loading ? 'disabled' : ''}>Load more notices</button></div>` : ''}
      ${snapshot?.generated_at ? `<p class="field-hint notice-footer">${rows.length} loaded notices. Read-only status checked ${when(snapshot.generated_at)}. Queueing and submission do not establish delivery. Times are local to this browser.</p>` : ''}${introduction}</section>`;
  }
  return {load,reset,panel,filter(next){
    if (['upcoming','attention','history'].includes(next) && view !== next) {
      view=next;render();
      globalThis.document?.querySelector?.(`[data-notification-filter="${next}"]`)?.focus?.();
    }
  },async refresh(){await load();render();},async more(){await load({more:true});render();}};
}
