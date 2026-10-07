import {presentationText} from './admin-readiness.js';
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
    paused:'Paused',disconnected:'Disconnected'}[status] || 'Status unverified');
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
  if (status?.startsWith('blocked_') || status === 'superseded') return textStatusLabel(status);
  // The current API records mock_only/not_recorded, neither is native proof.
  if (row.state === 'verified-delivered') return 'Delivery unverified';
  if (['submitted','sent','uncertain','failed'].includes(status)) return textStatusLabel(status);
  return textStatusLabel(row.state);
}

export function createAdminNotifications({api,getMode,getToken,getConfig,render}) {
  let snapshot = null, error = '', loading = false;
  const connected = () => getMode() === 'live' && !!getToken();
  const reset = () => { snapshot = null; error = ''; };
  async function load({more = false} = {}) {
    if (!connected()) { reset(); return; }
    if (loading) return;
    const token = getToken(); loading = true;
    try {
      const offset = more && Number.isInteger(snapshot?.next_offset) ? snapshot.next_offset : 0;
      const result = await api(`/api/notification-status?limit=100&offset=${offset}`);
      if (connected() && getToken() === token) {
        const rows = more ? [...(snapshot?.notifications || []), ...(result.notifications || [])] : result.notifications;
        snapshot = {...result, notifications:rows}; error = '';
      }
    } catch (failure) {
      if (connected() && getToken() === token) { snapshot = null; error = failure.message; }
    } finally { loading = false; }
  }
  const when = value => value ? esc(new Date(value).toLocaleString()) : 'Not scheduled';
  function panel() {
    const introduction = '<p><strong>Scheduled notice</strong> for a recorded shift, followed by one <strong>Day-before reminder</strong>. These notices do not ask volunteers to confirm by text. Signup preferences are saved quietly; the saved completion wording is not automatically sent.</p>';
    if (!connected()) return `<section class="panel settings-panel section"><h2>Shift notices</h2>${introduction}<p class="notice">This preview is disconnected. No notices are queued or delivered here.</p></section>`;
    const config = getConfig();
    const runtime = !config.aiReady || !config.macBridgeConnected ? 'Disconnected. AI and the laptop Messages connection are required.'
      : !config.automationEnabled ? 'Scheduling is paused. Planned notices are not proof of queued texts.'
        : 'Scheduling is configured. Running automation and native delivery still need verification.';
    const rows = snapshot?.notifications || [];
    return `<section class="panel settings-panel section" aria-labelledby="shift-notices-heading"><h2 id="shift-notices-heading">Shift notices</h2>${introduction}<p class="notice" role="status">${esc(runtime)}</p>
      ${error ? `<p class="error" role="alert">Could not check notice status: ${esc(presentationText(error))}</p>` : ''}
      <div class="setup-actions section"><button class="quiet" data-notification-refresh ${loading ? 'disabled' : ''}>Refresh notice status</button></div>
      ${rows.map(row => `<article class="section"><h3>${esc(noticeTypeLabel(row.notice))}</h3><p>${esc(row.recipient_name)} · ${esc(row.role)} · ${esc(row.event_title)}</p><p><strong>${esc(notificationLabel(row))}</strong></p><p class="field-hint">Event: ${when(row.starts_at)}${row.due_at ? `<br>Notice due: ${when(row.due_at)}` : ''} (your local time)</p>${row.reason ? `<p>${esc(presentationText(row.reason))}</p>` : ''}${row.next_step ? `<p><strong>Next step:</strong> ${esc(presentationText(row.next_step))}</p>` : ''}${row.state === 'awaiting-review' ? '<button class="quiet" data-page="volunteers">Review exact text</button>' : ''}</article>`).join('') || (!snapshot ? '<p class="field-hint">Live status is unavailable until the connected backend responds.</p>' : '<p class="field-hint">No shift notices were returned. This does not establish that texts were sent.</p>')}
      ${Number.isInteger(snapshot?.next_offset) ? `<div class="setup-actions section"><button class="quiet" data-notification-more ${loading ? 'disabled' : ''}>Load more notices</button></div>` : ''}
      ${snapshot?.generated_at ? `<p class="field-hint">Read-only status checked ${when(snapshot.generated_at)}. Queueing and submission do not establish delivery.</p>` : ''}</section>`;
  }
  return {load,reset,panel,async refresh(){await load();render();},async more(){await load({more:true});render();}};
}
