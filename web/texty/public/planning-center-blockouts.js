import {churchLabel} from './church-presentation.js';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const states=new Set(['pending','verified','held','unknown','disabled']);
const readyKeys=['signing_ready','acceptance_ready','mapping_ready','consent_ready'];
const validZone=value=>{try{return typeof value==='string' && !!new Intl.DateTimeFormat('en',{timeZone:value});}catch{return false;}};
const modes=value=>value?.notification_mode==='provider_managed' && value?.conflict_strategy==='preserve_existing_bookings';
const revision=value=>value===null || typeof value==='string';
function validStatus(value,id){
  return Number(value?.volunteer_id)===Number(id) && typeof value.policy_enabled==='boolean' && typeof value.runtime_enabled==='boolean'
    && states.has(value.state) && typeof value.unknown_attempt==='boolean' && (value.owned_count===null || Number.isSafeInteger(value.owned_count) && value.owned_count>=0)
    && revision(value.desired_revision) && revision(value.verified_revision)
    && [null,'provider_managed'].includes(value.notification_mode) && [null,'preserve_existing_bookings'].includes(value.conflict_strategy) && value.effect_scope==='all_services_teams'
    && typeof value.acceptance?.available==='boolean' && readyKeys.every(key=>typeof value.readiness?.[key]==='boolean')
    && ['authority_ready','journal_ready'].every(key=>value.readiness[key]===undefined || typeof value.readiness[key]==='boolean');
}
function readinessText(status){
  if(!status.readiness.consent_ready)return 'This volunteer’s current text consent needs attention.';
  if(!status.readiness.mapping_ready)return 'A current Planning Center person connection is needed.';
  if(!status.readiness.acceptance_ready || !status.acceptance.available)return 'The connection owner must finish the notification and booking checks.';
  if(!status.readiness.signing_ready)return 'The connection owner must finish automatic sync setup.';
  return 'Check the saved availability and Planning Center connection with the connection owner.';
}
export function blockoutPresentation(status){
  if(status.unknown_attempt || status.state==='unknown')return {label:'Needs attention',detail:'A previous Planning Center update could not be confirmed. Automatic updates are held until the connection owner checks it.'};
  if(!status.policy_enabled)return {label:'Off',detail:'Automatic updates are off for this volunteer.'};
  if(status.state==='held' || !readyKeys.every(key=>status.readiness[key]) || status.readiness.authority_ready===false || status.readiness.journal_ready===false)return {label:'Needs attention',detail:readinessText(status)};
  if(!status.runtime_enabled)return {label:'Waiting',detail:'Automatic sync is enabled. Updates are waiting for the connection owner to start the Planning Center sync connection.'};
  if(status.state==='verified' && status.desired_revision && status.desired_revision===status.verified_revision)return {label:'Synced',detail:'Planning Center has verified this volunteer’s latest saved full-day availability.'};
  return {label:'Waiting',detail:'Automatic sync is enabled. The latest saved availability is waiting for Planning Center verification.'};
}

export function createPlanningCenterBlockouts({api,getMode,getToken,getSessionEpoch=()=>getToken(),getVolunteer,render}) {
  let status=null,error='',notice='',busy=false,generation=0,identity=null;
  const connected=()=>getMode()==='live' && !!getToken();
  const person=()=>{const v=getVolunteer();return v && /^[1-9][0-9]*$/.test(String(v.id)) && Number.isSafeInteger(Number(v.id)) ? v : null;};
  const owner=()=>JSON.stringify([getSessionEpoch(),person()?.id]);
  function reset(){generation++;status=null;error=notice='';busy=false;identity=null;}
  function ensure(){const next=owner();if(identity!==next){reset();identity=next;}}
  const current=(version,key)=>connected() && version===generation && key===owner() && !!person();
  const canEnable=()=>status && !status.policy_enabled && !status.unknown_attempt && status.state!=='unknown'
    && readyKeys.every(key=>status.readiness[key]) && status.readiness.journal_ready!==false && status.acceptance.available && modes(status.acceptance)
    && /^[a-f0-9]{64}$/.test(status.acceptance.evidence_hash || '') && validZone(status.acceptance.timezone)
    && ['verified_at','expires_at'].every(key=>Number.isFinite(Date.parse(status.acceptance[key])));
  async function request(enabled){
    ensure();if(!connected() || !person() || busy)return;
    const saving=typeof enabled==='boolean';
    if(saving && (!status || enabled && !canEnable()))return;
    const version=++generation,key=owner(),id=person().id;busy=true;error=notice='';render();
    try{
      const response=await api(`/api/planning-center/blockouts/${id}${saving?'/policy':''}`,saving?{enabled}:undefined,saving?{method:'PUT'}:undefined);
      if(!current(version,key))return;
      if(!validStatus(response,id) || saving && response.policy_enabled!==enabled)throw Error('Incomplete sync status');
      status={...response,unknown_attempt:!!response.unknown_attempt};
      if(saving)notice=enabled?'Automatic sync is enabled for this volunteer. Later saved full-day availability changes follow this setting.':'Automatic updates are off. Existing sync history is preserved.';
    }catch(failure){if(current(version,key)){status=null;error=saving?'Could not confirm the setting change. Check current sync status before trying another change.':
      [401,403].includes(failure?.status)?'Sign in with an authorized admin account to check availability sync.':'Could not check availability sync. Check its current status before making a change.';}}
    finally{if(current(version,key)){busy=false;render();}}
  }
  function panel(){
    ensure();if(!connected())return '';
    if(!person())return '<section class="panel settings-panel section"><h2>Automatic availability sync</h2><p>Choose a volunteer above to check their Planning Center availability sync.</p></section>';
    const v=person(),name=churchLabel([v.first_name,v.last_name].filter(Boolean).join(' ') || v.name || 'Volunteer');
    const presentation=status?blockoutPresentation(status):null,zone=status?.acceptance?.timezone;
    const button=(action,label,disabled=false)=>`<button class="${action==='refresh'?'quiet':'primary'}" data-pco-blockout-action="${action}" data-pco-blockout-person="${esc(v.id)}" ${busy||disabled?'disabled':''}>${label}</button>`;
    return `<section class="panel settings-panel section" aria-labelledby="pco-blockout-heading"><h2 id="pco-blockout-heading">Automatic availability sync</h2><p>For <strong>${esc(name)}</strong>. Keep Planning Center blockouts up to date when this volunteer’s saved full-day availability changes.</p><p>Blockouts apply across all Planning Center Services teams. Planning Center uses its normal leader notification preferences. This sync does not cancel existing bookings. Role limits and partial-hour availability stay in Text Monkey.</p>${validZone(zone)?`<p>Availability dates use ${esc(zone)}.</p>`:''}${error?`<p class="error" role="alert">${esc(error)}</p>`:''}${notice?`<p role="status">${esc(notice)}</p>`:''}<p role="status">${busy?'Checking availability sync…':presentation?`<strong>${presentation.label}</strong>. ${presentation.detail}`:'Check current sync status to see whether this person’s automatic sync is enabled.'}</p>${status && !status.policy_enabled && !canEnable()?`<p class="field-hint">${esc(readinessText(status))}</p>`:''}<div class="setup-actions">${button('refresh','Check sync status')}${status?`${!status.policy_enabled?button('enable','Enable automatic sync',!canEnable()):''}${button('disable','Turn automatic sync off')}`:''}</div></section>`;
  }
  return {panel,load:()=>request(),reset,async action(action,id){ensure();if(String(id)!==String(person()?.id))return;if(action==='refresh')await request();else if(action==='enable')await request(true);else if(action==='disable')await request(false);}};
}
