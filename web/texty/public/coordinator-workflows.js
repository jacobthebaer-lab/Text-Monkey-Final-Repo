const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const flagNames = {single_point_of_failure:'Backup coverage',burnout:'Workload',drop_off:'Serving rhythm',
  untapped:'Volunteers waiting to serve',expiring:'Expiring qualifications',unused_skill:'Unused skills',
  chronic_gap:'Recurring gaps',growing_need:'Growing needs',rebalance:'Ministry balance'};

export function createCoordinatorWorkflows({api,getMode,getToken,getSessionEpoch=()=>getToken(),render,onChanged=async()=>{},getTimezone=()=> 'America/Denver'}) {
  let coordinators=[],selected='',draft='',flags=[],busy=false,loaded=false,error='',notice='',answer='',cacheOwner=null;
  const signedIn=()=>getMode()==='live' && !!getToken();
  const sameOwner=owner=>signedIn() && owner===getSessionEpoch();
  const reset=()=>{coordinators=[];selected=draft=error=notice=answer='';flags=[];loaded=false;busy=false;cacheOwner=null;};
  async function load() {
    if (!signedIn()) {reset();return;}
    const owner=getSessionEpoch();
    if (cacheOwner!==owner) reset();
    cacheOwner=owner;
    try {
      const [context,capacity]=await Promise.all([api('/api/coordinator'),api('/api/coordinator/capacity')]);
      if (!sameOwner(owner)) return;
      if (!Array.isArray(context.coordinators) || !Array.isArray(capacity.flags)) throw new Error('Coordinator tools returned an unreadable response. Try loading them again.');
      coordinators=context.coordinators; flags=capacity.flags; loaded=true;error='';
      if (!coordinators.some(c=>String(c.id)===selected)) selected=coordinators.length===1?String(coordinators[0].id):'';
    } catch (failure) {if (sameOwner(owner)) {loaded=false;error=failure.message;}}
  }
  async function mutate(operation,kind) {
    if (!signedIn() || busy) return;
    const owner=getSessionEpoch();busy=true;error=notice='';render();
    try {
      const result=await operation();
      if (!sameOwner(owner)) return;
      if (kind==='capacity') {
        flags=result.flags || [];
        notice=result.state==='held'?'Some explanations are waiting for AI. Review the recorded facts below.':'Staffing review is ready. Choose the next step for your church.';
      } else {
        answer=result.final_text || '';
        notice=result.state==='pending_exact_review'?'Your proposed changes are ready. Review them in Messages before approving.'
          :result.state==='held'?'This request is held for review. No changes were applied.':'Your answer is ready.';
        await onChanged();
      }
    } catch (failure) {if (sameOwner(owner)) error=failure.message;}
    finally {if (sameOwner(owner)) {busy=false;render();}}
  }
  return {
    load,reset,
    setDraft(value){draft=value;},setCoordinator(value){selected=value;},
    async submit(form) {
      if (!signedIn() || busy) return;
      const data=Object.fromEntries(new FormData(form));
      draft=String(data.command || '');selected=String(data.coordinator_id || '');
      const identity=Number(selected);
      if (!Number.isInteger(identity) || !coordinators.some(c=>c.id===identity) || !draft.trim() || draft.trim().length>1000) {
        error='Choose a saved coordinator and enter a short request.';render();return;
      }
      await mutate(()=>api('/api/coordinator/command',{coordinator_id:identity,command:draft.trim()}),'command');
    },
    async capacity(){await mutate(()=>api('/api/coordinator/capacity',{}),'capacity');},
    async refresh(){await load();if(signedIn())render();},
    panel() {
      if (!signedIn()) return '';
      if (cacheOwner!==getSessionEpoch()) reset();
      return `<section class="panel settings-panel section" aria-labelledby="coordinator-workflow-heading">
        <h2 id="coordinator-workflow-heading">Ask Text Monkey</h2>
        <p>Ask about your schedule, plan an event, or adjust how many helpers you need. You review every proposed change before it happens.</p>
        ${error?`<p class="error" role="alert">${esc(error)}</p>`:''}
        ${!loaded?'<button type="button" data-coordinator-action="refresh">Load coordinator tools</button>':''}
        <form id="coordinator-command-form">
          <label for="coordinator-person">Coordinator</label><select id="coordinator-person" name="coordinator_id" required ${busy?'disabled':''}>
            <option value="">Choose a coordinator</option>${coordinators.map(c=>`<option value="${esc(c.id)}" ${String(c.id)===selected?'selected':''}>${esc(c.name)}</option>`).join('')}</select>
          ${loaded&&!coordinators.length?'<p class="notice">A saved active coordinator is needed before making a request.</p>':''}
          <label for="coordinator-command">Your request</label><textarea id="coordinator-command" name="command" rows="3" maxlength="1000" required ${busy?'disabled':''} placeholder="Add two greeter slots to Sunday's service, or help plan a new event.">${esc(draft)}</textarea>
          <button class="primary section" ${busy||!loaded||!coordinators.length?'disabled':''}>${busy?'Working…':'Prepare for review'}</button>
        </form>
        ${notice?`<p role="status">${esc(notice)}</p>`:''}${answer?`<p>${esc(answer)}</p>`:''}
        <div class="setup-actions section"><button type="button" data-page="messages">Review proposed changes</button><button type="button" data-coordinator-action="capacity" ${busy||!loaded?'disabled':''}>Check staffing</button></div>
        ${flags.length?`<h3 class="section">Staffing observations</h3>${flags.filter(f=>f.status!=='dismissed').map(f=>{
          const ready=f.evidence?.narration?.state==='ready';
          const checked=f.evidence?.scan_at;
          return `<article class="insight"><h4>${esc(flagNames[f.type] || 'Staffing observation')}</h4><p>${esc(f.summary)}</p>
            ${ready&&f.suggested_action?`<p><strong>Next step:</strong> ${esc(f.suggested_action)}</p>`:''}
            <details><summary>Recorded facts</summary><p>${esc(f.evidence?.observation || 'No recorded observation is available.')}</p>
            ${checked?`<small>Checked ${esc(new Date(checked).toLocaleString(undefined,{timeZone:getTimezone()}))}</small>`:''}</details></article>`;
        }).join('')}`:''}
      </section>`;
    }
  };
}
