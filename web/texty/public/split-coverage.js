const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

export function createSplitCoverage({api,getMode,getToken,getSessionEpoch,getVolunteers=()=>[],getTimezone=()=> 'America/Denver',render,onChanged=async()=>{}}) {
  let data=null,busy=false,error='',notice='',owner=null;
  const requests=new Map();
  const signed=()=>getMode()==='live' && !!getToken();
  const current=epoch=>signed() && getSessionEpoch()===epoch;
  const reset=()=>{data=null;busy=false;error=notice='';owner=null;requests.clear();};
  const request=id=>{if(!requests.has(id))requests.set(id,globalThis.crypto.randomUUID());return requests.get(id);};
  const time=value=>new Date(value).toLocaleString(undefined,{timeZone:getTimezone(),month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  const name=id=>getVolunteers().find(v=>String(v.id)===String(id))?.name || getVolunteers().find(v=>String(v.id)===String(id))?.first_name || `Helper ${id}`;
  async function load() {
    if(!signed()){reset();return;}
    const epoch=getSessionEpoch();
    if(owner!==epoch)reset();
    owner=epoch;
    try{const value=await api('/api/split-coverage');if(current(epoch)){data=value;error='';}}
    catch(failure){if(current(epoch))error=failure.message;}
  }
  async function mutate(path,body,message) {
    if(!signed()||busy)return;
    const epoch=getSessionEpoch();busy=true;error=notice='';render();
    try{await api(path,body);if(!current(epoch))return;notice=message;await onChanged();await load();}
    catch(failure){if(current(epoch))error=failure.message;}
    finally{if(current(epoch)){busy=false;render();}}
  }
  return {load,reset,
    async action(button){
      if(!signed())return;
      const kind=button.dataset.splitAction;
      if(kind==='refresh'){await load();render();return;}
      if(owner!==getSessionEpoch()){reset();return;}
      if(!data)return;
      if(kind==='partition'){
        const source=data.evidence.find(e=>String(e.incoming_id)===button.dataset.input);
        if(source)await mutate('/api/split-coverage/partitions',{parent_id:source.parent_id,outreach_id:source.outreach_id,
          incoming_id:source.incoming_id,request_id:button.dataset.request},'The exact interval proposal is ready for review. No helper has been booked.');
      }else if(kind==='outreach'){
        await mutate(`/api/split-coverage/${Number(button.dataset.parent)}/outreach`,{content_hash:button.dataset.hash},'The existing worker may prepare exact interval offers, subject to its saved contact, review and delivery rules.');
      }else if(kind==='booking'){
        await mutate(`/api/split-coverage/${Number(button.dataset.parent)}/booking-review`,{},'Review all exact child intervals and helpers together before booking.');
      }else if(kind==='approve'||kind==='reject'){
        const review=data.reviews.find(r=>String(r.id)===button.dataset.review);
        if(review)await mutate(`/api/split-coverage/reviews/${review.id}/${kind}`,{content_hash:review.content_hash},
          kind==='reject'?'The proposal was rejected.':review.kind==='confirm_split_partition'
            ?'The split intervals were saved. Each interval still needs its own delivered offer, actual reply and final booking review.'
            :'The reviewed helpers were booked for their exact intervals. Parent coverage is calculated from all child intervals.');
      }
    },
    async setRole(input){if(!data||owner!==getSessionEpoch()||!signed())return;await mutate(`/api/split-coverage/roles/${Number(input.dataset.splitRole)}`,{allowed:input.checked},
      input.checked?'This role may use reviewed split coverage.':'Reviewed split coverage is held for this role.');},
    panel(){
      if(!signed())return '';
      if(owner!==getSessionEpoch())reset();
      return `<section class="panel settings-panel section" aria-labelledby="split-coverage-heading">
        <h2 id="split-coverage-heading">Reviewed split coverage</h2>
        <p>Choose roles that may be shared across exact time intervals. A partial reply first creates a reviewed interval proposal. Helpers are booked only after actual replies to each interval's delivered offer and a final review.</p>
        <p>Planning Center slots remain held until an exact partial-time mapping is supported. Existing consent, qualifications and messaging rules still apply.</p>
        ${error?`<p class="error" role="alert">${esc(error)}</p>`:''}${notice?`<p role="status">${esc(notice)}</p>`:''}
        <button type="button" data-split-action="refresh" ${busy?'disabled':''}>${data?'Refresh split coverage':'Load split coverage'}</button>
        ${data?`<details class="section"><summary>Roles that may be split</summary>${data.roles.map(r=>
          `<label><input type="checkbox" data-split-role="${esc(r.id)}" ${r.allowed?'checked':''} ${busy?'disabled':''}> ${esc(r.name)}</label>`).join('')}</details>
        ${data.evidence.map(e=>`<article class="insight section"><h3>${esc(e.role)}, ${esc(e.event)}</h3><p>${esc(time(e.start))} to ${esc(time(e.end))}</p>
          <blockquote>${esc(e.actual_reply)}</blockquote><button type="button" data-split-action="partition" data-input="${esc(e.incoming_id)}"
          data-request="${esc(request(e.incoming_id))}" ${busy?'disabled':''}>Prepare exact interval proposal</button></article>`).join('')}
        ${data.reviews.filter(r=>r.status==='pending').map(r=>{const partition=r.kind==='confirm_split_partition';
          const intervals=partition?r.scope.intervals:r.scope.children.map(c=>({start:c.snapshot.start,end:c.snapshot.end,volunteer_id:c.volunteer_id}));
          return `<article class="insight section"><h3>${partition?'Review split intervals':'Review all helper bookings'}</h3>
            ${partition?'<p>Approving saves child slots only. No helper will be booked by this action.</p>':'<p>Approving books every listed helper together. A changed or expired source holds the whole action.</p>'}
            <ul>${intervals.map(i=>`<li>${i.volunteer_id?`${esc(name(i.volunteer_id))}: `:''}${esc(time(i.start))} to ${esc(time(i.end))}</li>`).join('')}</ul>
            <button type="button" data-split-action="approve" data-review="${esc(r.id)}" ${busy?'disabled':''}>Approve these exact ${partition?'intervals':'bookings'}</button>
            <button type="button" data-split-action="reject" data-review="${esc(r.id)}" ${busy?'disabled':''}>Reject</button></article>`;}).join('')}
        ${data.coverage.map(c=>`<article class="insight section"><h3>Split slot ${esc(c.parent_id)}</h3><p>${c.held?esc(c.held):c.fully_covered?'Every child interval is covered.':`${c.gaps.length} child interval${c.gaps.length===1?'':'s'} still need coverage.`}</p>
          <ul>${c.children.map(i=>`<li>${esc(time(i.start))} to ${esc(time(i.end))}: ${i.assigned?esc(name(i.volunteer_id)):'awaiting reviewed coverage'}</li>`).join('')}</ul>
          ${c.initial_pending&&!c.held?`<button type="button" data-split-action="outreach" data-parent="${esc(c.parent_id)}" data-hash="${esc(data.reviews.find(r=>r.kind==='confirm_split_partition'&&r.scope.source.parent.parent_id===c.parent_id)?.content_hash || '')}" ${busy?'disabled':''}>Ask eligible helpers for these intervals</button><button type="button" data-split-action="booking" data-parent="${esc(c.parent_id)}" ${busy?'disabled':''}>Prepare final booking review</button>`:''}</article>`).join('')}`:''}
      </section>`;
    }
  };
}
