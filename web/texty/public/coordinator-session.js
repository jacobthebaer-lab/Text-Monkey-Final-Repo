// Supabase validates lifetime, revocation and access on the server. This client
// rotates a granted session, without inventing a longer-lived access token.
const KEY = 'texty.coordinator.session.v1';
const PUBLIC = new Set(['/api/config','/api/login','/api/register','/api/recover','/api/session/refresh']);
const failure = (message,status) => Object.assign(new Error(message),{status});

export function createCoordinatorSession({fetch:send,storage,now=()=>Date.now(),onChange=()=>{},onInvalid=()=>{}}) {
  let current = null, epoch = 0, refreshing = null;
  const positive = value => value !== null && value !== undefined && Number.isFinite(Number(value)) && Number(value)>0;
  function normalize(value) {
    if (typeof value === 'string' && value) return {access_token:value}; // old access-only tab
    if (!value || typeof value.access_token !== 'string' || !value.access_token) return null;
    return {access_token:value.access_token,
      refresh_token:typeof value.refresh_token === 'string' && value.refresh_token ? value.refresh_token : null,
      expires_at:positive(value.expires_at) ? Number(value.expires_at) : positive(value.expires_in) ? now()/1000+Number(value.expires_in) : null};
  }
  function persist() {
    try {
      if (!current) storage().removeItem(KEY);
      else storage().setItem(KEY,current.refresh_token ? JSON.stringify(current) : current.access_token);
    } catch { /* Restricted tab storage still permits an in-memory session. */ }
  }
  function set(value,{save=true}={}) {
    epoch++; refreshing=null; current=normalize(value); onChange(current?.access_token || null);
    if (save) persist();
  }
  function restore() {
    let value;
    try {value=storage().getItem(KEY); try {value=JSON.parse(value);} catch { /* legacy opaque access token */ }} catch {return false;}
    set(value,{save:false}); return !!current;
  }
  const unchanged = owner => owner===epoch;
  function invalidate(owner) {
    if (unchanged(owner)) {set(null); onInvalid();}
  }
  async function json(response) {
    try {return await response.json();} catch {throw failure('The server returned an unreadable response. Try again.',503);}
  }
  async function rotate(owner) {
    if (!unchanged(owner)) throw failure('The signed-in session changed. Try again.',409);
    if (refreshing) return refreshing;
    if (!current?.refresh_token) {invalidate(owner); throw failure('Session expired. Sign in again.',401);}
    const secret=current.refresh_token;
    const pending=(async()=>{
      const response=await send('/api/session/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:secret})});
      // Never restore or revoke a newer login after an older response arrives.
      if (!unchanged(owner)) throw failure('The signed-in session changed. Try again.',409);
      if (response.status===401 || response.status===403) {invalidate(owner); throw failure('Session expired. Sign in again.',401);}
      const result=await json(response);
      if (!response.ok) throw failure(result.error || result.detail || 'Sign-in is temporarily unavailable. Try again.',response.status);
      const next=normalize(result);
      if (!next?.refresh_token) throw failure('Sign-in returned an incomplete session. Try again.',503);
      if (!unchanged(owner)) throw failure('The signed-in session changed. Try again.',409);
      current=next; persist(); onChange(current.access_token);
    })();
    refreshing=pending;
    try {await pending;} finally {if (refreshing===pending) refreshing=null;}
  }
  function signOut() {
    const captured=current?.access_token;
    // The click boundary is synchronous. In-flight refreshes/actions belong to
    // the previous epoch and cannot resume while remote logout is pending.
    set(null);
    if (!captured) return Promise.resolve();
    return Promise.resolve().then(()=>send('/api/logout',{method:'POST',
      headers:{'Content-Type':'application/json',Authorization:`Bearer ${captured}`},body:'{}'}))
      .then(response=>{
        if (!response.ok) throw failure('Signed out locally. The server sign-out could not be verified.',response.status);
      });
  }
  async function request(path,body) {
    const owner=epoch, authenticated=!!current && !PUBLIC.has(path);
    if (authenticated && current.refresh_token && current.expires_at && current.expires_at*1000<=now()+60000) await rotate(owner);
    const call=()=>send(path,{method:body ? 'POST':'GET',headers:{'Content-Type':'application/json',
      ...(authenticated && current ? {Authorization:`Bearer ${current.access_token}`} : {})},...(body ? {body:JSON.stringify(body)} : {})});
    if (!unchanged(owner)) throw failure('The signed-in session changed. Try again.',409);
    let used=current?.access_token, response=await call();
    if (!unchanged(owner)) throw failure('The signed-in session changed. Try again.',409);
    if (authenticated && response.status===401) {
      // Retry only an explicit authentication rejection, once, under the same
      // login. Never replay a mutation on a network error, 5xx or a new account.
      if (used===current?.access_token) await rotate(owner);
      if (!unchanged(owner)) throw failure('The signed-in session changed. Try again.',409);
      response=await call();
      if (!unchanged(owner)) throw failure('The signed-in session changed. Try again.',409);
      if (response.status===401) invalidate(owner);
    }
    if (authenticated && response.status===403 && response.headers?.get?.('X-Texty-Auth-Invalid')==='1') invalidate(owner);
    const result=await json(response);
    if (!unchanged(owner) && response.ok) throw failure('The signed-in session changed. Try again.',409);
    if (!response.ok) throw failure(result.error || result.detail || 'Request failed.',response.status);
    return result;
  }
  return {set,restore,request,signOut,hasSession:()=>!!current};
}
