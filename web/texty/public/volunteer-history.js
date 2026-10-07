// Each profile uses authenticated, paginated reads independent of the inbox cap.
export function createVolunteerHistory({api,getSessionEpoch,getSelectedId,render}) {
  let generation=0, current=null;
  const reset=()=>{generation++;current=null;};
  const view=id=>current?.id===String(id) && current.epoch===getSessionEpoch() ? current : null;
  async function load(id,{older=false,fictional=true,preserve=false}={}) {
    id=String(id);
    const previous=view(id);
    if(older && (!previous?.next || previous.loading))return;
    const owner=++generation,epoch=getSessionEpoch();
    const expectedFictional=older ? previous.fictional : fictional===true;
    const keep=older || (preserve && previous?.fictional===expectedFictional);
    const messages=keep ? previous?.messages || [] : [];
    const olderLoaded=older || (keep && previous?.olderLoaded===true);
    current={id,epoch,fictional:expectedFictional,olderLoaded,messages,next:keep?previous?.next:null,loading:true,error:''};render();
    const belongs=()=>owner===generation && epoch===getSessionEpoch() && getSelectedId()===id;
    try {
      const result=await api(`/api/volunteers/${encodeURIComponent(id)}/history?limit=100${older?`&before_id=${previous.next}`:''}`);
      if(!belongs())return;
      if(String(result.volunteer_id)!==id || result.fictional!==expectedFictional || !Array.isArray(result.messages)
          || result.messages.some(row=>row.fictional!==expectedFictional || (expectedFictional && row.status!=='simulated')))
        throw Error('The server did not confirm this conversation history.');
      const combined=older ? [...result.messages,...messages] : [...messages,...result.messages];
      current={id,epoch,fictional:expectedFictional,olderLoaded,messages:[...new Map(combined.map(row=>[String(row.id),row])).values()]
        .sort((a,b)=>(Date.parse(a.created_at)-Date.parse(b.created_at)) || Number(a.id)-Number(b.id)),
        next:preserve && previous?.olderLoaded ? previous.next : Number.isInteger(result.next_before_id)&&result.next_before_id>0?result.next_before_id:null,loading:false,error:''};
    }catch(error){if(belongs())current={id,epoch,fictional:expectedFictional,olderLoaded,messages,next:keep?previous?.next:null,loading:false,error:error.message};}
    if(belongs())render();
  }
  return {view,load,reset};
}
