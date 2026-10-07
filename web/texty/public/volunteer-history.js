// Fictional histories are authenticated reads, independent of the live inbox cap.
export function createVolunteerHistory({api,getSessionEpoch,getSelectedId,render}) {
  let generation=0, current=null;
  const reset=()=>{generation++;current=null;};
  const view=id=>current?.id===String(id) && current.epoch===getSessionEpoch() ? current : null;
  async function load(id,{older=false}={}) {
    id=String(id);
    const previous=view(id);
    if(older && (!previous?.next || previous.loading))return;
    const owner=++generation,epoch=getSessionEpoch();
    const messages=older ? previous.messages : [];
    current={id,epoch,messages,next:older?previous.next:null,loading:true,error:''};render();
    const belongs=()=>owner===generation && epoch===getSessionEpoch() && getSelectedId()===id;
    try {
      const result=await api(`/api/volunteers/${encodeURIComponent(id)}/history?limit=100${older?`&before_id=${previous.next}`:''}`);
      if(!belongs())return;
      if(String(result.volunteer_id)!==id || result.fictional!==true || !Array.isArray(result.messages)
          || result.messages.some(row=>row.fictional!==true || row.status!=='simulated'))
        throw Error('The server did not confirm this fictional history.');
      const combined=[...result.messages,...messages];
      current={id,epoch,messages:[...new Map(combined.map(row=>[String(row.id),row])).values()]
        .sort((a,b)=>String(a.created_at).localeCompare(String(b.created_at)) || Number(a.id)-Number(b.id)),
        next:Number.isInteger(result.next_before_id)&&result.next_before_id>0?result.next_before_id:null,loading:false,error:''};
    }catch(error){if(belongs())current={id,epoch,messages,next:older?previous.next:null,loading:false,error:error.message};}
    if(belongs())render();
  }
  return {view,load,reset};
}
