// Render only server-derived readiness; this component never sends a text.
export function adminReadiness(status, esc) {
  if (!status?.checks) return status?.issues?.length
    ? `<ul class="connection-issues">${status.issues.map(issue => `<li>${esc(issue)}</li>`).join('')}</ul>` : '';
  const missing = status.checks.filter(check => !check.ready);
  const ready = status.checks.filter(check => check.ready);
  const action = check => check.action === 'setup'
    ? '<button class="quiet small" data-page="setup">Finish church setup</button>'
    : check.action === 'mobile'
      ? '<button class="quiet small" data-action="focus-admin-mobile">Set up my mobile</button>'
      : check.action === 'schedule'
        ? `<button class="quiet small" data-page="schedule">Review upcoming events</button>${check.next_step ? `<p>${esc(check.next_step)}</p>` : ''}`
      : check.next_step
        ? `<details><summary>View next step</summary><p>${esc(check.next_step)}</p></details>` : '';
  const sessionTime = (value) => esc(new Date(value).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}));
  return `<section aria-label="Admin connection checklist"><h3>Connection checklist</h3>
    <p>${status.connection_check_ready ? 'Ready for a one-time connection check.' : 'Complete the steps below before sending a connection check.'} ${status.ready ? 'Scheduled updates are ready.' : 'Scheduled updates need attention.'}</p>
    ${missing.length ? `<ul class="connection-issues">${missing.map(check => `<li><strong>${esc(check.label)}</strong><p>${esc(check.detail)}</p>${action(check)}</li>`).join('')}</ul>` : ''}
    ${ready.length ? `<details><summary>${ready.length} connection check${ready.length === 1 ? '' : 's'} ready</summary><ul>${ready.map(check => `<li><strong>${esc(check.label)}</strong>: ${esc(check.detail)}</li>`).join('')}</ul></details>` : ''}
    ${status.session_starts_at ? `<p class="field-hint">Messages session starts ${sessionTime(status.session_starts_at)} and expires ${sessionTime(status.session_expires_at)} (your local time).</p>` : ''}
    ${status.pending_check ? `<p class="notice" role="status">Your connection check is waiting. Retry after ${sessionTime(status.pending_check.retry_at)} (your local time). The saved request is reused to prevent duplicates; Gloo and quiet hours still apply.</p>` : ''}
    <button class="quiet small" data-action="reload-admin-texts">Refresh connection status</button></section>`;
}
