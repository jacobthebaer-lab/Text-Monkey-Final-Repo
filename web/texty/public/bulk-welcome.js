import {churchLabel} from './church-presentation.js';
// Explicit, bounded recipient steps. A lost response resumes the same batch.
import {presentationText} from './admin-readiness.js';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function createBulkWelcome({api,getVolunteers,getReady,getSessionEpoch,render,onChanged,uuid=()=>crypto.randomUUID()}) {
  let selected=new Set(),batch=null,running=false,error='',generation=0,sessionEpoch=getSessionEpoch();
  function ensureSession(){if(sessionEpoch!==getSessionEpoch()){reset();sessionEpoch=getSessionEpoch();}}
  const selectable=v=>getReady()&&!v.fictional&&v.consent&&v.status==='active'&&(v.welcome_eligible??v.can_start_text_setup);
  function reset(){generation++;selected=new Set();batch=null;running=false;error='';}
  function select(id,value){ensureSession();if(running)return;const person=getVolunteers().find(v=>String(v.id)===String(id));if(!value)selected.delete(String(id));else if(person&&selectable(person))selected.add(String(id));render();}
  function selectFiltered(rows,value){ensureSession();if(running)return;for(const person of rows){if(!value)selected.delete(String(person.id));else if(selectable(person))selected.add(String(person.id));}render();}
  async function process(ids,{resume=false}={}) {
    ensureSession();if(running||!getReady())return;
    if(!resume){ids=[...new Set(ids.map(String))].sort((a,b)=>Number(a)-Number(b));if(!ids.length)return;
      if(!batch||batch.done||JSON.stringify(batch.ids)!==JSON.stringify(ids))batch={request_id:uuid(),ids,results:[],done:false};}
    if(!batch||batch.done)return;
    const owner=++generation,epoch=getSessionEpoch(),current=batch;
    const belongs=()=>owner===generation&&epoch===getSessionEpoch()&&batch===current;
    running=true;error='';render();
    try {
      while(belongs()&&!current.done){
        const result=await api('/api/welcome-batches',{request_id:current.request_id,volunteer_ids:current.ids.map(Number)});
        if(!belongs())return;
        if(result.request_id!==current.request_id||typeof result.done!=='boolean'||!Array.isArray(result.results)
            || result.results.some(row=>!current.ids.includes(String(row.volunteer_id)))
            || result.completed!==result.results.length||result.total!==current.ids.length
            || result.completed<=current.results.length&&!result.done)
          throw Error('The backend did not confirm welcome progress. Resume this request safely.');
        current.results=result.results;current.done=result.done;render();
      }
      if(belongs()){
        for(const row of current.results)if(['prepared','already_prepared'].includes(row.status))selected.delete(String(row.volunteer_id));
        await onChanged();
      }
    }catch(problem){if(belongs())error=presentationText(problem.message);}
    finally{if(belongs()){running=false;render();}}
  }
  const send=()=>process([...selected]);
  const resume=()=>process([],{resume:true});
  const retry=()=>process((batch?.results||[]).filter(row=>['held','failed'].includes(row.status)).map(row=>row.volunteer_id));
  function panel(){
    ensureSession();
    const resultLabel=row=>row.status==='already_prepared'?'Already prepared. Check their history or review.':row.status==='prepared'
      ?row.delivery==='awaiting_confirmation'?'Awaiting exact review. Nothing sent.':'Queued for Messages. Delivery appears in their history.':presentationText(row.reason)||'Held. Open their profile for details.';
    return `<section class="panel welcome-bulk" aria-labelledby="welcome-bulk-title">
      <div class="welcome-bulk-header">
        <div class="welcome-bulk-copy"><h2 id="welcome-bulk-title">Welcome volunteers</h2><p>Select volunteers below to send a welcome text. Select all includes eligible people in the current filtered roster.</p></div>
        <div class="welcome-bulk-actions"><p class="welcome-bulk-count" role="status">${selected.size} selected${running?`<span>Preparing ${batch.results.length+1} of ${batch.ids.length}…</span>`:''}</p><button class="primary" data-welcome-action="send" ${running||!getReady()||!selected.size?'disabled':''}>Send welcome texts</button></div>
      </div>
      ${error?`<p class="welcome-bulk-error error" role="alert">${esc(error)}</p>`:''}
      ${batch&&!batch.done&&!running?'<div class="welcome-bulk-followup"><button data-welcome-action="resume">Resume welcome request</button></div>':''}
      ${batch?.done&&batch.results.some(row=>['held','failed'].includes(row.status))?`<div class="welcome-bulk-followup"><button data-welcome-action="retry" ${running?'disabled':''}>Retry held welcomes</button></div>`:''}
      ${batch?.results.length?`<ul class="welcome-bulk-results">${batch.results.map(row=>`<li><strong>${esc(churchLabel(row.name))}</strong><span>${esc(resultLabel(row))}</span></li>`).join('')}</ul>`:''}
    </section>`;
  }
  return {reset,select,selectFiltered,selectable,send,resume,retry,panel,busy:()=>{ensureSession();return running;},selected:()=>{ensureSession();return new Set(selected);}};
}
