// Pure helpers used by the setup UI and its synthetic preview.
export const defaults = () => ({country: 'US', timezone: 'America/Denver', quiet_start: '21:00', quiet_end: '07:00', monthly_ask_limit: 4});
export const emptySetup = () => ({details: defaults(), revision: 0, completed: false, saved_at: null});
export const fields = ['name', 'first_name', 'last_name', 'phone', 'email', 'ministry'];
export function guessMapping(headers) {
  const aliases = {name:['name','full name','display name'], first_name:['first name','firstname','given name'],last_name:['last name','lastname','surname','family name'],phone:['phone','phone number','mobile','mobile phone','cell','telephone'],email:['email','email address'],ministry:['ministry','team','department']};
  const map = {};
  headers.forEach((header, index) => { const h = header.trim().toLowerCase().replaceAll('_',' '); for (const [field, names] of Object.entries(aliases)) if (!(field in map) && names.includes(h)) map[field] = index; });
  return map;
}
export function parseCSV(text) {
  text = text.replace(/^\uFEFF/, '');
  const delimiter = text.split(/\r?\n/)[0].includes('\t') ? '\t' : text.split(/\r?\n/)[0].includes(';') ? ';' : ',';
  const rows = []; let row = [], value = '', quoted = false, closed = false;
  for (let i = 0; i <= text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i+1] === '"') { value += '"'; i++; }
      else if (c === '"') { quoted = false; closed = true; }
      else if (c === undefined) throw new Error('Check the CSV quoting.');
      else value += c;
    } else if (c === '"') {
      if (value || closed) throw new Error('Check the CSV quoting.');
      quoted = true;
    } else if (c === delimiter || c === '\n' || c === '\r' || c === undefined) {
      row.push(value); value = ''; closed = false;
      if (c !== delimiter) {
        if (row.some(v => v.trim())) rows.push(row);
        row = [];
        if (c === '\r' && text[i+1] === '\n') i++;
      }
    } else { if (closed) throw new Error('Check the CSV quoting.'); value += c; }
  }
  if (rows.length < 2 || rows.length > 2001 || rows.some(r => r.length > 80 || r.some(v => v.length > 500))) throw new Error('Use a header and 1–2,000 rows, up to 80 columns and 500 characters per cell.');
  return rows;
}
export function normalizePhone(value, country = 'US') {
  value = value.trim();
  if (!/^\+?[0-9\s().-]+$/.test(value)) throw new Error('Use a phone without extensions or letters.');
  let digits = value.replace(/\D/g,'');
  if (value.startsWith('+')) { if (!/^[1-9][0-9]{7,14}$/.test(digits)) throw new Error('Use +country code and 8–15 digits.'); }
  else if (['US','CA'].includes(country) && digits.length === 10) digits = '1'+digits;
  else if (!(['US','CA'].includes(country) && digits.length === 11 && digits.startsWith('1'))) throw new Error('Include +country code; local numbers support US/Canada only.');
  if (digits.startsWith('1') && (digits.length !== 11 || !/[2-9]/.test(digits[1]) || !/[2-9]/.test(digits[4]))) throw new Error('Check the US/Canada area code and exchange.');
  return '+'+digits;
}
export function previewSample(rows, mapping, country, source, existing = []) {
  if (!source.trim()) throw new Error('Describe the contact source.');
  if (!('phone' in mapping) || !('name' in mapping || 'first_name' in mapping)) throw new Error('Map a phone and a name column.');
  if (new Set(Object.values(mapping)).size !== Object.values(mapping).length) throw new Error('Map each field to a different column.');
  const seen = new Set(existing), counts = {ready:0,duplicate:0,invalid:0};
  const results = rows.slice(1).map((row,index) => {
    const get = key => (row[mapping[key]] || '').trim();
    const contact = {name:get('name') || [get('first_name'),get('last_name')].filter(Boolean).join(' '),phone:get('phone'),email:get('email'),ministry:get('ministry'),source:source.trim(),consent:'not_recorded'};
    let status = 'ready', reason = 'Awaiting volunteer consent; no text will be sent.';
    try {
      if (row.length !== rows[0].length) throw new Error('Column count differs from the header.');
      if (Object.values(contact).some(v => v.startsWith('='))) throw new Error('Replace formulas with plain text.');
      if (!contact.name || contact.name.length > 160) throw new Error('Provide a name of 1–160 characters.');
      if (contact.ministry.length > 160) throw new Error('Ministry is too long.');
      if (contact.email && (contact.email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contact.email))) throw new Error('Check the email address or leave it blank.');
      contact.phone = normalizePhone(contact.phone,country);
      if (seen.has(contact.phone)) { status='duplicate'; reason='Phone already staged or repeated; existing record preserved.'; }
      else seen.add(contact.phone);
    } catch(e) { status='invalid'; reason=e.message; }
    counts[status]++;
    return {row:index+2,...contact,status,reason};
  });
  return {rows:results,counts};
}
export function errorCSV(rows) {
  const cell = v => '"'+String(v ?? '').replace(/^[=+@\-\t\r]/,"'$&").replaceAll('"','""')+'"';
  return [['Row','Status','Reason'], ...rows.filter(r=>r.status !== 'ready').map(r=>[r.row,r.status,r.reason])].map(r=>r.map(cell).join(',')).join('\r\n');
}
export const sampleCSV = 'Full name,Mobile,Email,Team\nAlex Sample,(202) 555-0111,alex@example.test,Welcome\nCasey Example,+12025550112,casey@example.test,Production\nAlex Duplicate,2025550111,,Welcome\nMorgan Fix,not a phone,,Kids\n';
