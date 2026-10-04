const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const hashKeys = ['preview_hash','source_hash','remote_hash','operation_hash'];
const isHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const labels = {
  native_notification_silence_unverified:'Planning Center notification behavior still needs verification.',
  native_edit_coordination_unverified:'Other Planning Center edits still need coordination.',
  fresh_native_preflight_required:'A fresh Planning Center check is required before any future change.',
  notification_policy_not_verified:'Notification behavior is not verified.',
  unowned_remote_frequency_preserved:'The existing Planning Center preference is preserved until ownership is verified.',
  conflict_owned_membership_changed:'The Planning Center role membership has changed and needs review.',
  verified_membership_mapping_required:'A verified role membership mapping is required.',
  native_frequency_not_exactly_representable:'This role frequency cannot be represented exactly in Planning Center.',
  native_role_hours_or_group_context_unsupported:'Role hours and recurring group availability stay in Text Monkey.',
  no_supported_global_frequency_write:'A person-wide frequency limit stays in Text Monkey.',
  removed_cap_requires_reviewed_restore:'Removing a role limit needs a separate reviewed restoration.',
  no_supported_recurring_window_write:'Recurring availability windows stay in Text Monkey.',
  recurring_window_not_representable:'This recurring availability window stays in Text Monkey.',
  source_not_authoritative:'The saved availability source is not verified.',
};
const holdLabel = hold => labels[typeof hold === 'string' ? hold : hold?.reason] || 'A required source, mapping or release check is unresolved.';
const holdList = holds => holds?.length ? `<ul>${[...new Set(holds.map(holdLabel))].map(label=>`<li>${escape(label)}</li>`).join('')}</ul>` : '';
const statusError = failure => [401,403].includes(failure?.status) ? 'Sign in with an authorized admin account to review Planning Center.'
  : failure?.status === 409 ? 'The comparison changed or requires fresh review. Load a current comparison and review it again.'
    : 'Planning Center review is held. The required source, mapping, connection or review configuration is not ready. No changes were applied.';

export function createPlanningCenterReview({api,getMode,getToken,getVolunteers,render}) {
  let selected='', preview=null, proposals=new Map(), error='', busy=false, generation=0, identity=null;
  const connected = () => getMode()==='live' && !!getToken();
  const roster = () => (getVolunteers() || []).filter(v=>/^[1-9][0-9]*$/.test(String(v.id)) && Number.isSafeInteger(Number(v.id)));
  const volunteer = () => roster().find(v=>String(v.id)===selected);
  const clear = () => { preview=null;proposals=new Map();error=''; };
  function reset() { generation++;selected='';busy=false;identity=null;clear(); }
  function ensureAccount() { if (identity!==getToken()) { reset();identity=getToken(); } }
  function select(value) { ensureAccount();generation++;selected=connected() && roster().some(v=>String(v.id)===String(value)) ? String(value) : '';busy=false;clear();render(); }
  const current = (version,token,id) => connected() && generation===version && getToken()===token && selected===id && !!volunteer();
  async function load() {
    ensureAccount();
    if (!connected() || !volunteer() || busy) return;
    const version=++generation,token=getToken(),id=selected;busy=true;clear();render();
    try {
      const result=await api('/api/planning-center/held-previews',{volunteer_id:Number(id)});
      if (!current(version,token,id)) return;
      if (result.execution_enabled!==false || !Array.isArray(result.operations) || !Array.isArray(result.holds) || !Array.isArray(result.release_holds)) throw Error('Invalid held preview');
      const next=new Map();
      for (const operation of result.operations) {
        if (operation.kind!=='membership_frequency' || !['PATCH','NONE'].includes(operation.method) || !isHash(operation.intent_key)) continue;
        const proposal=await api(`/api/planning-center/frequency-reviews/${operation.intent_key}`);
        if (!current(version,token,id)) return;
        const native=proposal.native_snapshot;
        const currentFrequency=native?.schedule_preference;
        const proposedFrequency=operation.method==='NONE' ? currentFrequency : proposal.operation?.body?.data?.attributes?.schedule_preference;
        const known = value => typeof value==='string' && value.trim() && value.length<=160;
        const comparison={role_name:proposal.membership?.role_name,current_frequency:known(currentFrequency)?currentFrequency:'Unavailable, comparison held',
          proposed_frequency:known(proposedFrequency)?proposedFrequency:'Unavailable, comparison held',observed_at:native?.saved_at};
        const reviewable=known(currentFrequency) && known(proposedFrequency) && Number.isFinite(Date.parse(native?.saved_at))
          && proposal.operation?.body?.data?.type==='PersonTeamPositionAssignment';
        if (proposal.execution_enabled!==false || proposal.intent_key!==operation.intent_key || !hashKeys.every(key=>isHash(proposal[key]))
            || proposal.source_hash!==result.source_hash || proposal.remote_hash!==result.remote_hash
            || proposal.operation?.kind!=='membership_frequency' || proposal.operation.logical_key!==operation.logical_key
            || proposal.operation.method!==operation.method || !Array.isArray(proposal.release_holds)
            || !known(comparison.role_name)) throw Error('Incomplete comparison');
        next.set(operation.intent_key,{comparison:{...comparison},method:operation.method,
          exact:reviewable ? Object.fromEntries(hashKeys.map(key=>[key,proposal[key]])) : null,matched:operation.method==='NONE' && proposal.operation.state==='noop' && known(currentFrequency) && Number.isFinite(Date.parse(native?.saved_at)),holds:proposal.release_holds,receipt:null});
      }
      if (current(version,token,id)) {preview={holds:result.holds.map(hold=>typeof hold==='string'?hold:{reason:hold?.reason}),release_holds:result.release_holds,otherOperations:result.operations.filter(op=>op.kind!=='membership_frequency').length};proposals=next;}
    } catch (failure) {if (current(version,token,id)) {clear();error=statusError(failure);}}
    finally {if (current(version,token,id)) {busy=false;render();}}
  }
  async function record(intent) {
    ensureAccount();
    const proposal=proposals.get(intent);
    if (!connected() || !volunteer() || busy || !proposal?.exact || proposal.method!=='PATCH' || proposal.receipt) return;
    const version=generation,token=getToken(),id=selected;busy=true;error='';render();
    const exact={...proposal.exact};
    try {
      const result=await api(`/api/planning-center/frequency-reviews/${intent}`,exact);
      if (!current(version,token,id)) return;
      if (result.state!=='reviewed_held' || typeof result.receipt_id!=='string' || !result.receipt_id || !isHash(result.receipt_hash) || result.execution_enabled!==false || !Number.isFinite(Date.parse(result.expires_at)) || !Array.isArray(result.release_holds)) throw Error('Unverified review');
      proposal.exact=null;proposal.receipt={expires_at:result.expires_at,holds:result.release_holds};
    } catch (failure) {if (current(version,token,id)) {clear();error=statusError(failure);}}
    finally {if (current(version,token,id)) {busy=false;render();}}
  }
  function panel() {
    ensureAccount();
    if (!connected()) { reset();return '<section class="panel settings-panel section"><h2>Planning Center review</h2><p class="notice">This synthetic preview is disconnected. Planning Center comparisons and review controls are available only in the signed-in console.</p></section>'; }
    if (selected && !volunteer()) reset();
    return `<section class="panel settings-panel section" aria-labelledby="pco-review-heading"><h2 id="pco-review-heading">Planning Center review</h2><p>Compare a volunteer’s saved role frequency with Planning Center. Recording a review cannot apply changes, schedule anyone or send a text. Planning Center frequency is a preference, not a guaranteed monthly cap.</p><label for="pco-review-volunteer">Volunteer to compare</label><select id="pco-review-volunteer" data-pco-volunteer ${busy?'disabled':''}><option value="">Choose a volunteer</option>${roster().map(v=>`<option value="${escape(v.id)}" ${String(v.id)===selected?'selected':''}>${escape([v.first_name,v.last_name].filter(Boolean).join(' ') || v.name || 'Volunteer')}</option>`).join('')}</select><div class="setup-actions section"><button class="quiet" data-pco-load ${busy || !volunteer()?'disabled':''}>${busy?'Checking…':'Load current comparison'}</button></div>${error?`<p class="error" role="alert">${escape(error)}</p>`:''}
      ${preview?`<p class="notice">Comparison loaded. All Planning Center changes remain held.</p>${holdList(preview.holds)}${preview.otherOperations?'<p class="field-hint">Other availability changes remain held. This panel records role-frequency reviews only.</p>':''}${!proposals.size?'<p>No reviewable role-frequency proposal was returned. No change was applied.</p>':''}${[...proposals].map(([intent,proposal])=>`<article class="section"><h3>${escape(proposal.comparison.role_name)}</h3><dl class="profile-details"><div><dt>Saved Planning Center preference</dt><dd>${escape(proposal.comparison.current_frequency)}</dd></div><div><dt>Proposed preference</dt><dd>${escape(proposal.comparison.proposed_frequency)}</dd></div></dl>${proposal.comparison.observed_at && Number.isFinite(Date.parse(proposal.comparison.observed_at))?`<p class="field-hint">Native snapshot saved ${escape(new Date(proposal.comparison.observed_at).toLocaleString())}. Recording a review is not a fresh native preflight.</p>`:''}${proposal.matched?'<p>Already matches the saved native value. No change is needed; no review receipt is required.</p>':!proposal.exact && !proposal.receipt?'<p class="notice">A complete saved native comparison is required before recording a review.</p>':proposal.receipt?`<p role="status">Review recorded, still held. Nothing was applied, scheduled or sent. Receipt expires ${escape(new Date(proposal.receipt.expires_at).toLocaleString())}.</p>`:`<div class="setup-actions section"><button class="quiet" data-pco-record="${escape(intent)}" ${busy?'disabled':''}>Record review</button></div>`}${holdList(proposal.receipt?.holds || proposal.holds)}</article>`).join('')}${!proposals.size?holdList(preview.release_holds):''}`:''}</section>`;
  }
  return {panel,select,load,record,reset};
}
