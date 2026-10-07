import {churchLabel} from './church-presentation.js';
// Review saved sender facts through the existing exact-action cards.
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
export function preferencesPanel(rows=[],connected=false){
  if(!connected||!rows.length)return '';
  return `<section class="panel section"><h2>Review saved signup preferences</h2><p>Match the volunteer's stated group and calendar restrictions, then create an exact review in Volunteers. Approval completes local preferences; clearance and scheduling remain separate.</p>${rows.map(row=>`<form class="section" data-preference-review="${esc(row.volunteer_id)}" data-source-hash="${esc(row.source_hash)}"><h3>${esc(churchLabel(row.name))}</h3><blockquote>${esc(row.actual_reply)}</blockquote><ul>${row.constraints.map(item=>`<li>${esc(item.description||item.reason||item.proposal?.calendar_restriction||'Unresolved availability')}</li>`).join('')}</ul>${row.windows.map(w=>`<div class="section"><strong>${esc(w.roles.map(churchLabel).join(', '))}</strong><p>${esc(days[w.weekday])}, ${esc(w.all_day?'all day':`${w.start_time||'time unknown'} to ${w.end_time||'time unknown'}`)}</p>${row.group_windows.includes(w.index)?`<label>Stated event group<select name="group:${w.index}"><option value="">Choose the matching saved group</option>${row.event_types.map(e=>`<option value="${esc(e.id)}" ${w.event_context?.event_type_ids?.includes(e.id)?'selected':''}>${esc(churchLabel(e.name))}</option>`).join('')}</select></label>`:''}<label>Weekday occurrence<select name="ordinal:${w.index}"><option value="">Every available week</option>${[1,2,3,4,5].map(n=>`<option value="${n}" ${w.month_ordinals?.length===1&&w.month_ordinals[0]===n?'selected':''}>${['First','Second','Third','Fourth','Fifth'][n-1]} ${esc(days[w.weekday])}</option>`).join('')}</select></label></div>`).join('')}${row.unavailable_months.map(month=>`<label><input type="checkbox" name="absence:${esc(month)}"> Keep all of ${esc(month)} unavailable, if stated above</label>`).join('')}<p class="field-hint">Role limits: ${esc(row.role_caps.map(c=>`${churchLabel(c.role_name)}, at most ${c.max_per_month} per month`).join('; ')||'No role cap added')}. Linked roles retain their same-day requirement. Choosing a stable schedule does not restrict otherwise flexible Sundays.</p><p class="error" role="alert"></p><button class="primary">Create exact preference review</button></form>`).join('')}</section>`;
}
export function preferenceChoices(form){
  const data=new FormData(form),event_mappings=[],window_ordinals=[],absence_months=[];
  for(const [key,value] of data){
    if(key.startsWith('group:')&&value)event_mappings.push({window_index:Number(key.slice(6)),event_type_id:Number(value)});
    if(key.startsWith('ordinal:')&&value)window_ordinals.push({window_index:Number(key.slice(8)),ordinals:[Number(value)]});
    if(key.startsWith('absence:')&&value==='on')absence_months.push(key.slice(8));
  }
  return {source_hash:form.dataset.sourceHash,event_mappings,window_ordinals,absence_months};
}
