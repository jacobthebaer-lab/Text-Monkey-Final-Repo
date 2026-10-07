import {createPlanningCenterBlockouts} from './planning-center-blockouts.js';
import {churchLabel} from './church-presentation.js';
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

export function createPlanningCenterReview({api,getMode,getToken,getSessionEpoch=()=>getToken(),getVolunteers,render}) {
  const roleBindings = createPlanningCenterRoleBindings({api,getMode,getToken,render});
  let selected='', preview=null, proposals=new Map(), error='', busy=false, generation=0, identity=null;
  const connected = () => getMode()==='live' && !!getToken();
  const roster = () => (getVolunteers() || []).filter(v=>/^[1-9][0-9]*$/.test(String(v.id)) && Number.isSafeInteger(Number(v.id)));
  const volunteer = () => roster().find(v=>String(v.id)===selected);
  const blockouts=createPlanningCenterBlockouts({api,getMode,getToken,getSessionEpoch,getVolunteer:volunteer,render});
  const clear = () => { preview=null;proposals=new Map();error=''; };
  function reset() { generation++;selected='';busy=false;identity=null;clear();roleBindings.reset();blockouts.reset(); }
  function ensureAccount() { if (identity!==getSessionEpoch()) { reset();identity=getSessionEpoch(); } }
  function select(value) { ensureAccount();generation++;selected=connected() && roster().some(v=>String(v.id)===String(value)) ? String(value) : '';busy=false;clear();blockouts.reset();render(); }
  const current = (version,epoch,id) => connected() && generation===version && getSessionEpoch()===epoch && selected===id && !!volunteer();
  async function load() {
    ensureAccount();
    if (!connected() || !volunteer() || busy) return;
    const version=++generation,epoch=getSessionEpoch(),id=selected;busy=true;clear();render();
    try {
      const result=await api('/api/planning-center/held-previews',{volunteer_id:Number(id)});
      if (!current(version,epoch,id)) return;
      if (result.execution_enabled!==false || !Array.isArray(result.operations) || !Array.isArray(result.holds) || !Array.isArray(result.release_holds)) throw Error('Invalid held preview');
      const next=new Map();
      for (const operation of result.operations) {
        if (operation.kind!=='membership_frequency' || !['PATCH','NONE'].includes(operation.method) || !isHash(operation.intent_key)) continue;
        const proposal=await api(`/api/planning-center/frequency-reviews/${operation.intent_key}`);
        if (!current(version,epoch,id)) return;
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
      if (current(version,epoch,id)) {preview={holds:result.holds.map(hold=>typeof hold==='string'?hold:{reason:hold?.reason}),release_holds:result.release_holds,otherOperations:result.operations.filter(op=>op.kind!=='membership_frequency').length};proposals=next;}
    } catch (failure) {if (current(version,epoch,id)) {clear();error=statusError(failure);}}
    finally {if (current(version,epoch,id)) {busy=false;render();}}
  }
  async function record(intent) {
    ensureAccount();
    const proposal=proposals.get(intent);
    if (!connected() || !volunteer() || busy || !proposal?.exact || proposal.method!=='PATCH' || proposal.receipt) return;
    const version=generation,epoch=getSessionEpoch(),id=selected;busy=true;error='';render();
    const exact={...proposal.exact};
    try {
      const result=await api(`/api/planning-center/frequency-reviews/${intent}`,exact);
      if (!current(version,epoch,id)) return;
      if (result.state!=='reviewed_held' || typeof result.receipt_id!=='string' || !result.receipt_id || !isHash(result.receipt_hash) || result.execution_enabled!==false || !Number.isFinite(Date.parse(result.expires_at)) || !Array.isArray(result.release_holds)) throw Error('Unverified review');
      proposal.exact=null;proposal.receipt={expires_at:result.expires_at,holds:result.release_holds};
    } catch (failure) {if (current(version,epoch,id)) {clear();error=statusError(failure);}}
    finally {if (current(version,epoch,id)) {busy=false;render();}}
  }
  function panel() {
    ensureAccount();
    if (!connected()) { reset();return '<section class="panel settings-panel section"><h2>Planning Center review</h2><p class="notice">This preview is disconnected. Planning Center comparisons and review controls are available only in the signed-in console.</p></section>'; }
    if (selected && !volunteer()) reset();
    return `<section class="panel settings-panel section" aria-labelledby="pco-review-heading"><h2 id="pco-review-heading">Planning Center review</h2><p>Compare a volunteer’s saved role frequency with Planning Center. Recording a review cannot apply changes, schedule anyone or send a text. Planning Center frequency is a preference, not a guaranteed monthly cap.</p><label for="pco-review-volunteer">Volunteer to review</label><select id="pco-review-volunteer" data-pco-volunteer ${busy?'disabled':''}><option value="">Choose a volunteer</option>${roster().map(v=>`<option value="${escape(v.id)}" ${String(v.id)===selected?'selected':''}>${escape(churchLabel([v.first_name,v.last_name].filter(Boolean).join(' ') || v.name || 'Volunteer'))}</option>`).join('')}</select><div class="setup-actions section"><button class="quiet" data-pco-load ${busy || !volunteer()?'disabled':''}>${busy?'Checking…':'Load current comparison'}</button></div>${error?`<p class="error" role="alert">${escape(error)}</p>`:''}
      ${preview?`<p class="notice">Comparison loaded. Role-frequency changes remain held.</p>${holdList(preview.holds)}${preview.otherOperations?'<p class="field-hint">Other operations in this comparison remain held. Automatic blockout sync uses the separate setting below.</p>':''}${!proposals.size?'<p>No reviewable role-frequency proposal was returned. No change was applied.</p>':''}${[...proposals].map(([intent,proposal])=>`<article class="section"><h3>${escape(churchLabel(proposal.comparison.role_name))}</h3><dl class="profile-details"><div><dt>Saved Planning Center preference</dt><dd>${escape(proposal.comparison.current_frequency)}</dd></div><div><dt>Proposed preference</dt><dd>${escape(proposal.comparison.proposed_frequency)}</dd></div></dl>${proposal.comparison.observed_at && Number.isFinite(Date.parse(proposal.comparison.observed_at))?`<p class="field-hint">Native snapshot saved ${escape(new Date(proposal.comparison.observed_at).toLocaleString())}. Recording a review is not a fresh native preflight.</p>`:''}${proposal.matched?'<p>Already matches the saved native value. No change is needed; no review receipt is required.</p>':!proposal.exact && !proposal.receipt?'<p class="notice">A complete saved native comparison is required before recording a review.</p>':proposal.receipt?`<p role="status">Review recorded, still held. Nothing was applied, scheduled or sent. Receipt expires ${escape(new Date(proposal.receipt.expires_at).toLocaleString())}.</p>`:`<div class="setup-actions section"><button class="quiet" data-pco-record="${escape(intent)}" ${busy?'disabled':''}>Record review</button></div>`}${holdList(proposal.receipt?.holds || proposal.holds)}</article>`).join('')}${!proposals.size?holdList(preview.release_holds):''}`:''}</section>` + blockouts.panel() + roleBindings.panel();
  }
  return {panel,select,load,record,reset,blockouts};
}

export function createPlanningCenterRoleBindings({api,getMode,getToken,render}) {
  let catalogue=null, fields={}, proposal=null, busy=false, error='', result=null, generation=0, identity=getToken();
  const connected=()=>getMode()==='live' && !!getToken();
  function reset() {generation++;catalogue=null;fields={};proposal=null;busy=false;error='';result=null;identity=getToken();}
  function account() {if(identity!==getToken()) reset();}
  const current=(version,token)=>connected() && generation===version && getToken()===token;
  function select(name,value) {account();if(busy)return;generation++;fields={...fields,[name]:value};proposal=null;result=null;error='';render();}
  function mapping() {
    const shift=catalogue?.shifts.find(s=>String(s.id)===fields.shift);
    const role=catalogue?.roles.find(r=>String(r.id)===fields.role);
    const position=catalogue?.positions.find(p=>`${p.service_type_id}:${p.position_id}`===fields.position);
    if(!shift || !role || !position || shift.service_type_id!==position.service_type_id)return null;
    return {shift_id:shift.id,local_role_id:role.id,team_id:position.team_id,
      position_id:position.position_id,plan_time_id:shift.plan_time_id};
  }
  async function perform(action) {
    account();if(!connected() || busy)return;
    const selected=mapping();
    if(action!=='load' && !selected)return;
    if(action==='apply' && (!proposal || Date.parse(proposal.review_token?.expires_at || '')<=Date.now()))return;
    const version=generation,token=getToken();busy=true;error='';render();
    try {
      if(action==='load') {
        const response=await api('/api/planning-center/role-bindings/catalogue');
        if(!current(version,token))return;
        if(response.native_writes!==false || response.execution_enabled!==false ||
            !['roles','shifts','positions'].every(k=>Array.isArray(response[k])))throw Error('Invalid catalogue');
        catalogue=response;fields={};proposal=null;result=null;
      } else if(action==='review') {
        const response=await api('/api/planning-center/role-bindings/proposal',selected);
        if(!current(version,token))return;
        if(!isHash(response.review_hash) || !isHash(response.review_token?.signature) ||
            typeof response.review_token.document!=='string' || response.native_writes!==false || response.execution_enabled!==false)throw Error('Invalid review');
        const signed=JSON.parse(response.review_token.document);
        const snapshot=response.snapshot;
        if(!Number.isFinite(Date.parse(signed.expires_at)) || typeof snapshot?.native?.position_name!=='string' ||
            typeof snapshot?.local?.role?.name!=='string' || !Array.isArray(snapshot.local.role.required_qualifications) ||
            !Array.isArray(snapshot.local.shifts) || !snapshot.local.binding || !snapshot.local.event)throw Error('Invalid review context');
        proposal={...response,review_token:{...response.review_token,expires_at:signed.expires_at}};result=null;
      } else {
        const response=await api('/api/planning-center/role-bindings', {...selected,review_hash:proposal.review_hash,
          review_token:{document:proposal.review_token.document,signature:proposal.review_token.signature}});
        if(!current(version,token))return;
        if(response.native_writes!==false || response.execution_enabled!==false || response.local_role_id!==selected.local_role_id)throw Error('Invalid applied mapping');
        result='Local role mapping saved. Qualifications and availability remain enforced. Nothing was scheduled or sent.';proposal=null;
      }
    } catch(failure) {if(current(version,token)){proposal=null;error=[401,403].includes(failure?.status)
      ? 'Sign in with an authorized admin account.' : 'Role mapping needs current owner configuration or fresh review.';}}
    finally {if(current(version,token)){busy=false;render();}}
  }
  function panel() {
    account();if(!connected())return '';
    const options=(rows,key,label,value)=>rows.map(row=>`<option value="${escape(key(row))}" ${String(key(row))===value?'selected':''}>${escape(churchLabel(label(row)))}</option>`).join('');
    const snap=proposal?.snapshot;
    return `<section class="card"><h2>Planning Center role mapping</h2><p>Connect an imported position to an existing local role. Its qualification and availability rules stay enforced.</p>
      <button class="quiet" data-pco-role-action="load" ${busy?'disabled':''}>Load role mapping choices</button>
      ${catalogue?`<label>Imported service slot<select data-pco-role-field="shift" ${busy?'disabled':''}><option value="">Select a service slot</option>${options(catalogue.shifts,s=>s.id,s=>`${churchLabel(s.title)}, slot ${s.id}`,fields.shift)}</select></label>
      <label>Planning Center position<select data-pco-role-field="position" ${busy?'disabled':''}><option value="">Select a native position</option>${options(catalogue.positions,p=>`${p.service_type_id}:${p.position_id}`,p=>`${churchLabel(p.name)}, service ${p.service_type_id}`,fields.position)}</select></label>
      <label>Existing local role<select data-pco-role-field="role" ${busy?'disabled':''}><option value="">Select a local role</option>${options(catalogue.roles,r=>r.id,r=>r.name,fields.role)}</select></label>
      <button class="quiet" data-pco-role-action="review" ${busy || !mapping()?'disabled':''}>Review role mapping</button>`:''}
      ${snap?`<article class="section"><h3>${escape(churchLabel(snap.native.position_name))} to ${escape(churchLabel(snap.local.role.name))}</h3>
        <p>Required qualifications: ${escape(snap.local.role.required_qualifications.join(', ') || 'None')}.</p>
        <p>Applies to ${snap.local.shifts.length} imported slots. Native scope ${escape(snap.local.event.native_key)}, team ${escape(snap.local.binding.team_id)}, position ${escape(snap.local.binding.position_id)}.</p>
        <p>${escape(snap.local.event.starts_at)} to ${escape(snap.local.event.ends_at)}. Existing assignment history prevents rebinding.</p>
        <button class="quiet" data-pco-role-action="apply" ${busy || Date.parse(proposal.review_token.expires_at)<=Date.now()?'disabled':''}>Use reviewed local role</button></article>`:''}
      ${error?`<p role="alert">${escape(error)}</p>`:''}${result?`<p role="status">${escape(result)}</p>`:''}</section>`;
  }
  if(typeof document!=='undefined' && typeof document.addEventListener==='function') {
    document.addEventListener('click',event=>{const button=event.target.closest?.('[data-pco-role-action]');if(button){event.preventDefault();perform(button.dataset.pcoRoleAction);}});
    document.addEventListener('change',event=>{if(event.target.dataset?.pcoRoleField)select(event.target.dataset.pcoRoleField,event.target.value);});
  }
  return {panel,select,perform,reset};
}
