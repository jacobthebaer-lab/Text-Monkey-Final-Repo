import {focusView} from './accessibility.js';
import {defaults, emptySetup, fields, guessMapping, parseCSV, previewSample, errorCSV, sampleCSV, normalizePhone} from './setup-domain.js';
const esc = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label = field => ({name:'Full name',first_name:'First name',last_name:'Last name',phone:'Phone number',email:'Email (optional)',ministry:'Ministry / team (optional)'}[field]);
const download = (name, text) => { const url=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'})), a=document.createElement('a'); a.href=url; a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); };
export function registrationDetails(values) {
  const details = {...defaults()};
  for(const key of ['church_name','address','city','region','postal_code','country','timezone','coordinator_name','coordinator_role','coordinator_phone'])
    if(values[key] !== undefined) details[key] = values[key];
  return details;
}
export function accountChurchFields(values={}) {
  const details = registrationDetails(values);
  const input = (key,text,max=160) => `<div><label for="account-${key}">${text}</label><input id="account-${key}" name="${key}" value="${esc(details[key])}" maxlength="${max}" required></div>`;
  return `<fieldset class="account-church-fields"><legend>Your church</legend>${input('church_name','Church name')}<div class="form-row">${input('coordinator_name','Your name')}${input('coordinator_role','Your role',100)}</div><div><label for="account-coordinator_phone">Your mobile number</label><input id="account-coordinator_phone" name="coordinator_phone" type="tel" autocomplete="tel" maxlength="40" value="${esc(details.coordinator_phone)}" required><p class="field-hint">Use your own mobile, not the church office number. Turn on event status texts in Settings after signing in.</p></div>${input('address','Street address',240)}<div class="form-row">${input('city','City',100)}${input('region','State / province',100)}</div><div class="form-row">${input('postal_code','ZIP / postal code',30)}<div><label for="account-country">Country</label><select id="account-country" name="country">${[['US','United States'],['CA','Canada'],['international','Another country']].map(([v,t])=>`<option value="${v}" ${details.country===v?'selected':''}>${t}</option>`).join('')}</select></div></div>${input('timezone','Church timezone',80)}<p class="field-hint">Confirm your email to finish creating this workspace. You can edit church details later in Settings.</p></fieldset>`;
}
export function createSetup({api, getMode, getToken, render, toast, onComplete}) {
  let setup = emptySetup(), draft = {...setup.details}, step = 0, contacts=[], sheets=[], sheet=0, mapping={}, report=null, source='', filename='', error='', unavailable=false, busy=false, submission=null, previewInput=null, importCountry='US', previewPage=0, contactPage=0;
  const localKey = 'texty.synthetic.setup.v1';
  const steps = ['Your church','Your ministry','Preferences','Ready to begin'];
  function clearImport() { sheets=[]; report=null; mapping={}; filename=''; submission=null; previewInput=null; previewPage=0; }
  async function load() {
    clearImport(); unavailable=false; error=''; step=0; contactPage=0; contacts=[]; setup=emptySetup();
    if (getMode()==='demo') {
      try { const saved=JSON.parse(localStorage.getItem(localKey)); if(saved) {setup=saved.setup || emptySetup(); contacts=saved.contacts || [];} } catch {}
    } else if(getToken()) {
      try { setup=await api('/api/setup'); if(setup.account_setup_available) setup=await api('/api/setup/from-account',{}); contacts=(await api('/api/setup/contacts')).contacts; }
      catch(e) { unavailable=true; error=e.message; }
    }
    draft={...setup.details}; importCountry=draft.country || 'US';
    if(setup.completed) step=3;
  }
  function reset() { localStorage.removeItem(localKey); setup=emptySetup(); draft={...setup.details}; contacts=[]; step=0; error=''; clearImport(); }
  function persist() { if(getMode()==='demo') localStorage.setItem(localKey,JSON.stringify({setup,contacts})); }
  function collect() {
    const form=document.querySelector('#church-setup-form');
    if(form) for(const [k,v] of new FormData(form)) draft[k]=k==='monthly_ask_limit'?Number(v):v;
  }
  function field(name,text,{required=false,type='text',hint='',max=160}={}) { return `<div><label for="setup-${name}">${text}${required?' <span class="required">*</span>':''}</label><input id="setup-${name}" name="${name}" type="${type}" value="${esc(draft[name])}" maxlength="${max}" ${required?'required':''}>${hint?`<small class="field-hint">${hint}</small>`:''}</div>`; }
  function select(name,text,options) { return `<div><label for="setup-${name}">${text}</label><select id="setup-${name}" name="${name}">${options.map(([v,t])=>`<option value="${esc(v)}" ${draft[name]===v?'selected':''}>${esc(t)}</option>`).join('')}</select></div>`; }
  const scope = () => getMode()==='demo' ? `<p class="notice setup-scope">Fictional local setup and staged contacts are saved in this browser only. No account, live roster, real texting rules or delivery is connected.</p>` : `<p class="notice setup-scope">This setup and imported contacts belong to your admin account. The existing scheduling dashboard serves one church. Setup preferences are saved for review; they do not switch the live church, change its texting rules or enable delivery.</p>`;
  function banner() { return `<div class="setup-callout"><div><strong>${setup.completed?'Your church setup is saved.':'Start with a few church details.'}</strong><p>${setup.completed?'Import a volunteer list, then follow the readiness checklist.':'A guided setup helps you prepare your volunteer list and first texting workflow.'}</p></div><button data-page="setup">${setup.completed?'Open setup checklist':'Set up your church'} →</button><button data-page="import">Import contacts</button></div>`; }
  function screen() {
    if(unavailable) return `<section class="panel setup-card"><h2>Setup is awaiting a storage update.</h2><p class="error" role="alert">${esc(error)}</p><p>Your existing scheduling tools remain available. An administrator must review and apply the setup migration before this feature can save data.</p><button data-setup="reload">Retry setup</button></section>`;
    return `<div class="setup-layout"><aside class="panel setup-steps"><p class="eyebrow">A little setup, a smoother week</p><h2>Finish your account</h2><ol>${steps.map((s,i)=>`<li><button data-setup-step="${i}" ${busy?'disabled':''} ${i===step?'aria-current="step"':''}><span>${i===3&&setup.completed?'✓':i+1}</span>${s}</button></li>`).join('')}</ol><p class="muted">${setup.saved_at?`Last saved ${esc(new Date(setup.saved_at).toLocaleString())}`:'Save a draft whenever you need a break.'}</p></aside><section class="panel setup-card">${step===3?checklist():form()}</section></div>${scope()}`;
  }
  function form() {
    let body='';
    if(step===0) body=`<p class="eyebrow">Account details · 1 of 3</p><h2>Tell us about your church.</h2><p class="muted">Use the church you coordinate for. You can update these details in Settings.</p>${field('church_name','Church name',{required:true})}${field('address','Street address',{required:true,max:240})}<div class="form-row">${field('city','City',{required:true,max:100})}${field('region','State / province / region',{required:true,max:100})}</div><div class="form-row">${field('postal_code','ZIP / postal code',{required:true,max:30})}${select('country','Country / phone region',[['US','United States'],['CA','Canada'],['international','Another country (+code required)']])}</div>${field('timezone','Church timezone',{required:true,max:80,hint:'Use an IANA timezone, e.g. America/Denver or Europe/London.'})}`;
    if(step===1) body=`<p class="eyebrow">Account details · 2 of 3</p><h2>Make it fit your ministry.</h2><p class="muted">The essentials now; the rest can wait.</p><div class="form-row">${field('coordinator_name','Your name',{required:true})}${field('coordinator_role','Your church role',{required:true,max:100})}</div>${field('coordinator_phone','Your mobile number',{type:'tel',required:true,max:40,hint:'Your own phone for event updates. You’ll turn on admin texts in Settings after setup.'})}${select('church_size','Typical weekly attendance (optional)',[['','Choose later'],['under_100','Under 100'],['100_300','100–300'],['300_1000','300–1,000'],['over_1000','Over 1,000']])}${field('service_times','Usual services (optional)',{max:500,hint:'For example: Sunday 9am and 11am; Wednesday 6pm.'})}${field('ministries','Ministries that need volunteers (optional)',{max:500,hint:'Welcome, kids, production, food pantry…'})}<div class="form-row">${field('church_phone','Church office phone (optional)',{type:'tel',max:40})}${field('website','Church website (optional)',{type:'url',max:240})}</div>`;
    if(step===2) body=`<p class="eyebrow">Account details · 3 of 3</p><h2>Give people room to say yes.</h2><p class="muted">Save the texting preferences you want your coordinator to review. The current live rules remain in effect until a separate reviewed configuration update.</p><div class="form-row">${field('quiet_start','Quiet hours begin',{type:'time',required:true,max:5})}${field('quiet_end','Quiet hours end',{type:'time',required:true,max:5})}</div><label for="setup-monthly_ask_limit">Monthly invitation limit per volunteer</label><input id="setup-monthly_ask_limit" name="monthly_ask_limit" type="number" min="1" max="8" value="${draft.monthly_ask_limit}"><p class="field-hint">Default: 4 asks per month. Confirmations and reminders follow the existing send gate.</p><div class="notice section"><strong>Importing a number never records text consent.</strong><p>Volunteers must explicitly opt in before outreach. STOP, qualifications, availability and human approval stay enforced.</p></div>`;
    return `<form id="church-setup-form">${body}<p class="error" role="alert">${esc(error)}</p><div class="setup-actions"><button type="button" data-setup="back" ${step===0||busy?'disabled':''}>Back</button><button type="submit" name="action" value="draft" formnovalidate ${busy?'disabled':''}>Save draft</button><button class="primary" type="submit" name="action" value="continue" ${busy?'disabled':''}>${busy?'Saving…':step===2?'Save church setup':'Save & continue'} →</button></div><p class="field-hint">Saved when you continue or choose Save draft. No texts are sent.</p></form>`;
  }
  function checklist() {
    return `<p class="eyebrow">Your next steps</p><h2>${setup.completed?'A good foundation. Now your people.':'Finish your church details first.'}</h2><p class="muted">${esc(draft.church_name || 'Your church')} · ${esc(draft.timezone)}</p><div class="readiness">${setup.completed && getMode()!=='demo' ? `<article><span class="check-number">${setup.coordinator_ready?'✓':'1'}</span><div><h3>Prepare coordinator tools</h3><p>${esc(setup.details.coordinator_name)} · ${esc(setup.details.coordinator_phone)}</p><p>Use these saved details for event planning and reviews. Text consent is managed separately in Settings.</p><button data-setup="coordinator" ${busy||setup.coordinator_ready?'disabled':''}>${setup.coordinator_ready?'Coordinator ready':'Set up coordinator tools'}</button></div></article>` : ''}<article><span class="check-number">${setup.completed?'✓':'1'}</span><div><h3>Save church details</h3><p>Church, address, coordinator and preferences.</p><button data-setup-step="0">${setup.completed?'Review details':'Continue setup'}</button></div></article><article><span class="check-number">${contacts.length?'✓':'2'}</span><div><h3>Prepare your volunteer list</h3><p>${contacts.length?`${contacts.length} contacts staged. All await consent.`:'Choose a spreadsheet or phone contact export, then review each row.'}</p><button data-page="import">Import contacts →</button></div></article><article><span class="check-number">3</span><div><h3>Connect and verify the church texting line</h3><p>The church owner configures the supported transport and chooses consenting test phones. Setup does not enable a line or delivery.</p><button data-page="settings">Check connections</button></div></article><article><span class="check-number">4</span><div><h3>Let volunteers start their own text signup</h3><p>Once the line is verified, share the church number through your existing church channels. Volunteers follow the configured signup prompts to provide their details and consent.</p><p>Imported contacts stay in staging. Connecting them to the single-church roster requires a separate reviewed workflow.</p></div></article><article><span class="check-number">5</span><div><h3>Review your first schedule</h3><p>Verify qualifications and coverage, then review every required approval before any outreach.</p><button data-page="schedule">View the existing church schedule</button></div></article></div><p class="error" role="alert">${esc(error)}</p>`;
  }
  function importScreen() {
    if(unavailable) return screen();
    const rows=sheets[sheet]?.rows;
    return `<section class="panel setup-card"><div class="section-heading"><div><p class="eyebrow">A careful start with your people</p><h2>Bring your volunteer list.</h2></div><span class="pill amber">Staged · no text consent</span></div><p class="muted">Select a CSV, Excel XLSX or phone contact export (.vcf), up to 5 MB and 2,000 rows. Only the file you choose is read. Raw files are discarded after parsing.</p>${getMode()==='demo'?'<p class="notice">Synthetic preview: use the sample below or a synthetic CSV. Excel and phone exports are available after verified sign-in. Do not choose personal contacts for this demo.</p>':''}<div class="import-source-grid"><div><h3>From Excel or a list</h3><p>Use a header row with names and phone numbers. Excel: save as XLSX or UTF-8 CSV. Keep international phones as text with +country code.</p><label class="file-choice" for="contact-file">Choose ${getMode()==='demo'?'synthetic CSV':'CSV, XLSX or vCard'} file<input id="contact-file" type="file" accept="${getMode()==='demo'?'.csv':'.csv,.xlsx,.vcf'}" ${busy?'disabled':''}></label>${getMode()==='demo'?`<button data-setup="sample" ${busy?'disabled':''}>Try a synthetic sample</button>`:''}<button class="quiet" data-setup="template">Download CSV template</button></div><div><h3>From your phone</h3><p>${typeof navigator.contacts?.select === 'function'?'This browser exposes a contact picker API; this MVP uses an exported file so you can review it first.':'Direct phone-contact access is unavailable here. Use a contact export instead.'}</p><p>iPhone: share a selected contact or export a Contacts list as vCard. Android / Google Contacts: export chosen contacts as vCard or CSV. Save to Files, then select the export here.</p><p class="field-hint">Browser and device support varies. The file picker reads only your explicit selection.</p></div></div>${!setup.saved_at?'<p class="notice section">Save a church setup draft before importing.</p><button data-page="setup">Set up your church</button>':''}${rows?mappingScreen(rows):''}<p class="error" role="alert">${esc(error)}</p></section>${report?reportScreen():''}${contactsScreen()}${scope()}`;
  }
  function mappingScreen(rows) {
    return `<form id="contact-map-form" class="section"><h2>Match your columns.</h2><p class="muted">${esc(filename)} · ${rows.length-1} rows · Review the worksheet and source before previewing.</p>${sheets.length>1?`<label for="import-sheet">Worksheet</label><select id="import-sheet">${sheets.map((s,i)=>`<option value="${i}" ${i===sheet?'selected':''}>${esc(s.name)}</option>`).join('')}</select>`:''}<div class="mapping-grid">${fields.map(f=>`<div><label for="map-${f}">${label(f)}</label><select id="map-${f}" name="${f}"><option value="">${['phone','name','first_name'].includes(f)?'Choose column':'Skip'}</option>${rows[0].map((h,i)=>`<option value="${i}" ${mapping[f]===i?'selected':''}>${i+1}. ${esc(h||'Unnamed column')}</option>`).join('')}</select></div>`).join('')}</div><label for="import-source">Where did this contact list come from?</label><input id="import-source" name="source" maxlength="240" value="${esc(source)}" placeholder="e.g. Welcome team signup sheet, October 2026" required><p class="field-hint">Source is provenance, not proof of consent. Consent columns are ignored.</p><label for="import-country">Default phone region</label><select id="import-country" name="country">${[['US','United States (+1)'],['CA','Canada (+1)'],['international','International (+country code required)']].map(([v,t])=>`<option value="${v}" ${importCountry===v?'selected':''}>${t}</option>`).join('')}</select><button class="primary section" ${busy||!setup.saved_at?'disabled':''}>${busy?'Checking…':'Preview contacts'} →</button></form>`;
  }
  function reportScreen() { return `<section class="panel setup-card section"><div class="section-heading"><h2>Review before saving.</h2><button class="quiet" data-setup="errors" ${report.counts.invalid+report.counts.duplicate?'':'disabled'}>Download row report</button></div><div class="import-counts"><span><strong>${report.counts.ready}</strong> ready to stage</span><span><strong>${report.counts.duplicate}</strong> duplicates skipped</span><span><strong>${report.counts.invalid}</strong> rows to fix</span></div><p class="muted">Valid new rows will be saved. Duplicate and invalid rows are skipped. Existing records and consent are never overwritten. Number formatting is checked; ownership and deliverability are unverified.</p><div class="table-wrap preview-table" tabindex="0" role="region" aria-label="Contact import preview, scroll horizontally"><table><thead><tr><th>Row</th><th>Name</th><th>Phone</th><th>Result / consent</th></tr></thead><tbody>${report.rows.slice(previewPage*100,(previewPage+1)*100).map(r=>`<tr><td>${r.row}</td><td>${esc(r.name)}</td><td>${esc(r.phone)}</td><td><span class="pill ${r.status==='ready'?'green':r.status==='invalid'?'amber':'gray'}">${esc(r.status)}</span><small>${esc(r.reason)}</small><small>No text consent recorded</small></td></tr>`).join('')}</tbody></table></div>${report.rows.length>100?`<div class="setup-actions"><button data-preview-page="${previewPage-1}" ${previewPage===0?'disabled':''}>Previous rows</button><span>Page ${previewPage+1} of ${Math.ceil(report.rows.length/100)}</span><button data-preview-page="${previewPage+1}" ${(previewPage+1)*100>=report.rows.length?'disabled':''}>Next rows</button></div>`:''}<div class="notice section"><strong>No outreach is enabled.</strong> These contacts remain separate from the live roster, inactive and awaiting explicit volunteer consent.</div><button class="primary section" data-setup="commit" ${busy||!report.counts.ready?'disabled':''}>${busy?'Saving…':`Save ${report.counts.ready} staged contact${report.counts.ready===1?'':'s'}`}</button></section>`; }
  function contactsScreen() { return `<section class="panel setup-card section"><div class="section-heading"><h2>Your staged contacts <span class="pill gray">${contacts.length}</span></h2><span class="pill amber">Outreach blocked</span></div>${contacts.length?`<div class="table-wrap" tabindex="0" role="region" aria-label="Staged contacts, scroll horizontally"><table><thead><tr><th>Name / phone</th><th>Source</th><th>Consent</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${contacts.slice(contactPage*100,(contactPage+1)*100).map(c=>`<tr><td><strong>${esc(c.name)}</strong><small>${esc(c.phone)}</small></td><td>${esc(c.source)}</td><td><span class="pill amber">Awaiting consent</span><small>Staged only · cannot text</small></td><td><button class="quiet danger" data-remove-staged="${esc(c.id)}" ${busy?'disabled':''}>Remove</button></td></tr>`).join('')}</tbody></table></div>${contacts.length>100?`<div class="setup-actions"><button data-contact-page="${contactPage-1}" ${contactPage===0?'disabled':''}>Previous contacts</button><span>Page ${contactPage+1} of ${Math.ceil(contacts.length/100)}</span><button data-contact-page="${contactPage+1}" ${(contactPage+1)*100>=contacts.length?'disabled':''}>Next contacts</button></div>`:''}`:'<p class="muted">No contacts staged yet. Start with the sample to see mapping, duplicates and error reporting.</p>'}</section>`; }
  async function save(complete) {
    collect();
    if(getMode()==='demo') {
      if(complete && ['church_name','address','city','region','postal_code','coordinator_name','coordinator_role','coordinator_phone'].some(k=>!draft[k]?.trim())) throw new Error('Complete the required church and coordinator details before finishing.');
      if(draft.coordinator_phone) draft.coordinator_phone=normalizePhone(draft.coordinator_phone,draft.country);
      try { new Intl.DateTimeFormat('en',{timeZone:draft.timezone}); } catch { throw new Error('Use a valid IANA timezone.'); }
      if(draft.quiet_start===draft.quiet_end) throw new Error('Choose different quiet-hour start and end times.');
      setup={details:{...draft},completed:complete,revision:setup.revision+1,saved_at:new Date().toISOString()}; persist();
    } else setup=await api('/api/setup',{details:draft,revision:setup.revision,complete});
  }
  document.addEventListener('click', async e=> {
    const b=e.target.closest('button'); if(!b) return;
    if(b.dataset.page) collect();
    if(!b.dataset.setup && b.dataset.setupStep===undefined && !b.dataset.removeStaged && b.dataset.previewPage===undefined && b.dataset.contactPage===undefined) return;
    e.stopImmediatePropagation(); if(busy) return;
    collect(); error=''; busy=true;
    try {
      if(b.dataset.setupStep!==undefined) step=Number(b.dataset.setupStep);
      if(b.dataset.previewPage!==undefined) previewPage=Number(b.dataset.previewPage);
      if(b.dataset.contactPage!==undefined) contactPage=Number(b.dataset.contactPage);
      if(b.dataset.setup==='back') step=Math.max(0,step-1);
      if(b.dataset.setup==='reload') await load();
      if(b.dataset.setup==='coordinator') {
        if(getMode()!=='live' || !setup.completed) throw new Error('Finish your connected church setup first.');
        const result=await api('/api/setup/coordinator',{revision:setup.revision});
        setup.coordinator_ready=result.coordinator_ready;
        toast('Coordinator tools are ready. No texts sent.');
      }
      if(b.dataset.setup==='sample') { sheets=[{name:'Sample contacts',rows:parseCSV(sampleCSV)}]; sheet=0; filename='Synthetic sample.csv'; source='Synthetic sample — no real people'; mapping=guessMapping(sheets[0].rows[0]); report=null; }
      if(b.dataset.setup==='template') download('volunteer-template.csv','Full name,Mobile,Email,Team\nAlex Sample,+12025550111,alex@example.test,Welcome\n');
      if(b.dataset.setup==='errors') download('contact-import-row-report.csv',errorCSV(report.rows));
      if(b.dataset.setup==='commit') {
        if(!report) throw new Error('Preview contacts again after changing mapping or source.');
        if(getMode()==='demo') { contacts.push(...report.rows.filter(r=>r.status==='ready').map(r=>({...r,id:crypto.randomUUID(),can_text:false}))); persist(); toast('Contacts staged. No texts sent.'); }
        else { const result=await api('/api/setup/import',{...previewInput,preview_hash:report.preview_hash,submission_id:submission}); contacts=(await api('/api/setup/contacts')).contacts; toast(`${result.imported} contacts staged. No texts sent.`); }
        clearImport();
      }
      if(b.dataset.removeStaged) {
        if(getMode()==='live') { const response=await fetch('/api/setup/contacts/'+encodeURIComponent(b.dataset.removeStaged),{method:'DELETE',headers:{Authorization:'Bearer '+getToken()}}); if(!response.ok) {const body=await response.json(); throw new Error(body.detail||'Unable to remove contact.');} }
        contacts=contacts.filter(c=>c.id!==b.dataset.removeStaged); persist(); report=null; contactPage=Math.min(contactPage,Math.max(0,Math.ceil(contacts.length/100)-1));
      }
    } catch(e) {error=e.message; toast(e.message);} finally {busy=false; render(); focusView(error ? '.setup-card .error' : '.setup-card h2');}
  },true);
  document.addEventListener('submit',async e=> {
    if(!['church-setup-form','contact-map-form'].includes(e.target.id)) return;
    e.preventDefault(); e.stopImmediatePropagation(); if(busy) return;
    const f=e.target; collect(); busy=true; error='';
    try {
      if(f.id==='church-setup-form') { const continuing=e.submitter?.value!=='draft', firstCompletion=!setup.completed && continuing && step===2; await save(continuing&&step===2); if(continuing) step++; if(firstCompletion && setup.completed) await onComplete?.(); toast(firstCompletion?'Your account is ready.':'Church details saved.'); }
      else {
        const data=Object.fromEntries(new FormData(f)); importCountry=data.country; mapping={}; for(const key of fields) if(data[key]!=='') mapping[key]=Number(data[key]); source=data.source;
        previewInput={rows:sheets[sheet].rows,mapping,country:data.country,source};
        report=getMode()==='demo'?previewSample(previewInput.rows,mapping,data.country,source,contacts.map(c=>c.phone)):await api('/api/setup/preview',previewInput);
        previewPage=0; submission=crypto.randomUUID();
      }
    } catch(e) {error=e.message;} finally {busy=false; render(); focusView(error ? '.setup-card .error' : '.setup-card h2');}
  },true);
  function invalidatePreview() {
    report=null;
    const commit=document.querySelector('[data-setup="commit"]');
    if(commit) {commit.disabled=true;commit.textContent='Preview again after changing the import';}
  }
  document.addEventListener('input', e=> { if(e.target.closest('#contact-map-form')) invalidatePreview(); });
  document.addEventListener('change', async e=> {
    if(e.target.id==='contact-file') {
      const file=e.target.files[0]; if(!file) return; clearImport(); error=''; busy=true;
      try {
        if(file.size>5*1024*1024) throw new Error('Select a file up to 5 MB.');
        if(getMode()==='demo') { if(!/\.csv$/i.test(file.name)) throw new Error('Use a synthetic CSV in preview; sign in for Excel or phone exports.'); sheets=[{name:'Contacts',rows:parseCSV(await file.text())}]; }
        else {const form=new FormData();form.append('file',file);const response=await fetch('/api/setup/parse',{method:'POST',body:form,headers:{Authorization:'Bearer '+getToken()}});const body=await response.json();if(!response.ok)throw new Error(body.detail||'Unable to parse selected file.');sheets=body.sheets;}
        filename=file.name; sheet=0; mapping=guessMapping(sheets[0].rows[0]);
      } catch(e) {error=e.message;} finally {busy=false; render();}
    } else if(e.target.id==='import-sheet') { sheet=Number(e.target.value); mapping=guessMapping(sheets[sheet].rows[0]); report=null; render(); }
    else if(e.target.closest('#contact-map-form')) { invalidatePreview(); }
  });
  return {load,reset,screen,importScreen,banner,collect,completed:()=>setup.completed,details:()=>setup.details};
}
