import { createHash, timingSafeEqual } from 'node:crypto';
import { mkdir, open, readFile, rename, chmod } from 'node:fs/promises';
import { join } from 'node:path';

export class Hold extends Error {
  constructor(code, status = 409) { super(code); this.code = code; this.status = status; }
}
export const scopeHash = sessions => hash(JSON.stringify(Object.entries(sessions).sort(([a],[b]) => a.localeCompare(b)).map(([phone,spec]) =>
  [phone,spec.id,new Date(spec.starts_at).toISOString(),new Date(spec.expires_at).toISOString(), ...(spec.continuous === true ? [true] : [])])));
export const hash = value => createHash('sha256').update(value).digest('hex');
export const maskPhone = value => value ? `***${value.slice(-4)}` : null;
export const maskEmail = value => value ? `${value[0]}***@${value.split('@')[1]}` : null;
export function validateOutgoingStyle(body) {
  // Match the Python delivery guard; never rewrite reviewed text or its hash.
  if (/[\u2014\ufe31\ufe58\u2e3a\u2e3b]/u.test(body)) throw new Hold('outbound_em_dash_forbidden', 400);
}
export function authorized(header, token) {
  if (typeof header !== 'string') return false;
  const a = Buffer.from(header), b = Buffer.from(`Bearer ${token}`);
  return a.length === b.length && timingSafeEqual(a, b);
}
export function validateSend(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input) ||
      Object.keys(input).some(k => !['idempotency_key', 'to', 'body', 'not_after'].includes(k)) ||
      typeof input.idempotency_key !== 'string' || !/^[A-Za-z0-9:_-]{8,128}$/.test(input.idempotency_key) ||
      typeof input.to !== 'string' || !/^\+1[2-9]\d{9}$/.test(input.to) ||
      typeof input.body !== 'string' || input.body.length > 1600 || !input.body.trim() || input.body.includes('\0') ||
      typeof input.not_after !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(input.not_after) ||
      !Number.isFinite(Date.parse(input.not_after))) {
    throw new Hold('invalid_send_request', 400);
  }
  validateOutgoingStyle(input.body);
  return { idempotency_key: input.idempotency_key, to: input.to, body: input.body, not_after: input.not_after };
}
export function validateCookies(input) {
  if (!input || Object.keys(input).some(k => k !== 'cookies') || !Array.isArray(input.cookies) ||
      input.cookies.length < 1 || input.cookies.length > 100) throw new Hold('invalid_session_request', 400);
  return input.cookies.map(cookie => {
    if (!cookie || typeof cookie.name !== 'string' || !/^[\w-]{1,100}$/.test(cookie.name) ||
        typeof cookie.value !== 'string' || cookie.value.length > 16384 ||
        !['.google.com', 'google.com', 'accounts.google.com', '.accounts.google.com', 'voice.google.com', '.voice.google.com'].includes(cookie.domain) ||
        cookie.path !== '/' || (cookie.expires !== undefined && (!Number.isFinite(cookie.expires) || cookie.expires < -1)) ||
        (cookie.sameSite !== undefined && !['Strict', 'Lax', 'None'].includes(cookie.sameSite))) {
      throw new Hold('invalid_cookie', 400);
    }
    return { name: cookie.name, value: cookie.value, domain: cookie.domain, path: '/',
      secure: true, httpOnly: cookie.httpOnly !== false,
      ...(cookie.expires !== undefined ? { expires: cookie.expires } : {}),
      ...(cookie.sameSite ? { sameSite: cookie.sameSite } : {}) };
  });
}

// One replica owns this volume. Atomic replacement + fsync ensures a durable
// send reservation survives process loss before any browser submission.
export class Store {
  constructor(directory) { this.directory = directory; this.path = join(directory, 'state.json'); }
  async load() {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    await chmod(this.directory, 0o700);
    try { this.data = JSON.parse(await readFile(this.path, 'utf8')); }
    catch (e) {
      if (e.code !== 'ENOENT') throw new Hold('state_unreadable', 503);
      this.data = { version: 1, account: null, baseline_at: null, seen: {}, inbound: [], sends: {}, next_cursor: 1 };
    }
    if (this.data.version !== 1 || !this.data.sends || !this.data.seen || !Array.isArray(this.data.inbound)) {
      throw new Hold('state_invalid', 503);
    }
    for (const record of Object.values(this.data.sends)) {
      if (record.status === 'pending') Object.assign(record, { status: 'uncertain', reason_code: 'restart_during_send' });
    }
    await this.save();
  }
  async save() {
    const temporary = `${this.path}.tmp`;
    const file = await open(temporary, 'w', 0o600);
    try { await file.writeFile(JSON.stringify(this.data)); await file.sync(); } finally { await file.close(); }
    await chmod(temporary, 0o600);
    await rename(temporary, this.path);
    const directory = await open(this.directory, 'r');
    try { await directory.sync(); } finally { await directory.close(); }
  }
}

export class Connector {
  constructor({ store, browser, expectedEmail, expectedPhone, allowedPhones, demoMode = false, signupEnabled = false, testSessions = {}, now = () => new Date().toISOString() }) {
    this.signupEnabled = signupEnabled;
    this.demoMode = demoMode; this.testSessions = { ...testSessions, ...(demoMode ? store.data.demo_sessions || {} : {}) };
    this.store = store; this.browser = browser; this.expectedEmail = expectedEmail.toLowerCase();
    this.expectedPhone = expectedPhone; this.now = now; this.queue = Promise.resolve();
    this.allowedPhones = new Set([...(allowedPhones || []), ...Object.keys(this.testSessions)]);
    if (demoMode) this.browser.allowedPhones = [...this.allowedPhones];
    this.state = 'reconnect_required'; this.reason = 'session_not_verified'; this.identity = null;
    this.preparation = null;
  }
  serialized(operation) {
    const result = this.queue.then(operation);
    this.queue = result.catch(() => {});
    return result;
  }
  health() {
    return { demo_mode: this.demoMode, scope_fingerprint: this.demoMode ? scopeHash(this.testSessions) : null, ready: this.state === 'ready', state: this.state, reason_code: this.reason, account_email: maskEmail(this.identity?.email),
      number: maskPhone(this.identity?.phone), identity_verified: !!this.identity, expected_identity_match: !!this.identity,
      identity_fingerprint: this.identity ? hash(`${this.identity.email.toLowerCase()}\n${this.identity.phone}`) : null,
      baseline_at: this.store.data.baseline_at, inbound_cursor: String(this.store.data.next_cursor - 1),
      delivery_verified: false,
      ...(this.browser.recipientPreparationDiagnostic ?
        { preparation_diagnostic: this.browser.recipientPreparationDiagnostic } : {}) };
  }
  hold(error) {
    this.state = 'reconnect_required'; this.reason = error instanceof Hold ? error.code : 'browser_unavailable';
    this.identity = null; this.preparation = null;
  }
  pendingPreparation() {
    if (this.preparation && Date.parse(this.preparation.not_after) <= Date.parse(this.now())) this.preparation = null;
    return this.preparation;
  }
  async verify() {
    const identity = await this.browser.identity();
    if (identity.email.toLowerCase() !== this.expectedEmail || identity.phone !== this.expectedPhone) {
      throw new Hold('account_mismatch');
    }
    const account = hash(`${identity.email.toLowerCase()}:${identity.phone}`);
    if (this.store.data.account && this.store.data.account !== account) throw new Hold('state_account_mismatch');
    this.store.data.account = account; this.identity = identity;
  }
  async poll({ phone = null, phones = null } = {}) {
    return this.serialized(async () => {
      if (phone && !this.allowedPhones.has(phone)) throw new Hold('recipient_not_allowed');
      if (phones && (!this.signupEnabled || phones.some(p => !this.allowedPhones.has(p) || this.testSessions[p]?.continuous !== true))) throw new Hold('recipient_not_allowed');
      // Keep the prepared recipient/composer intact while the backend rereads
      // approval, opt-out, pause and event state. A preparation lasts <=30s.
      if (this.pendingPreparation()) return this.health();
      try {
        await this.verify();
        const targets = phones || (phone ? [phone] : [...this.allowedPhones]);
        const emptyPhones = this.demoMode ? targets.filter(p => !this.store.data.demo_started?.[p]) : [];
        const messages = await this.browser.scan(targets, {emptyPhones});
        if (this.demoMode && messages.length > 100) throw new Hold('demo_intake_limit_exceeded');
        const observedAt = this.now();
        const baseline = this.store.data.baseline_at;
        // A complete first scan establishes a baseline. Never enqueue history.
        for (const message of messages) {
          if (!this.allowedPhones.has(message.phone)) continue;
          const id = hash(message.id);
          if (this.store.data.seen[id]) continue;
          const cutoff = this.demoMode && (this.store.data.demo_activation?.[message.phone] || this.store.data.demo_sessions?.[message.phone]?.starts_at) || baseline;
          if(message.received_at_interval && cutoff && Date.parse(message.received_at_interval.start)<Date.parse(cutoff)
            && Date.parse(message.received_at_interval.end)>Date.parse(cutoff)) throw new Hold('message_timestamp_ambiguous');
          this.store.data.seen[id] = true;
          if (!cutoff || Date.parse(message.received_at) < Date.parse(cutoff)) continue;
          this.store.data.inbound.push({ ...message, id, cursor: this.store.data.next_cursor++ });
        }
        this.store.data.baseline_at ||= observedAt;
        await this.store.save();
        this.state = 'ready'; this.reason = null;
      } catch (error) { this.hold(error); }
      return this.health();
    });
  }
  async session(input) {
    const cookies = validateCookies(input);
    return this.serialized(async () => {
      if (this.pendingPreparation()) throw new Hold('preparation_in_progress');
      this.hold(new Hold('session_not_verified'));
      await this.browser.importSession(cookies);
      try { await this.verify(); await this.store.save(); }
      catch (error) { await this.browser.clearSession(); this.hold(error); throw new Hold(this.reason); }
      // Polling must establish/refresh inbound state before sends are enabled.
      this.state = 'initializing'; this.reason = 'baseline_pending';
      return this.health();
    });
  }
  async verifyProfile() {
    return this.serialized(async () => {
      if (!this.demoMode) throw new Hold('demo_required');
      if (this.pendingPreparation()) throw new Hold('preparation_in_progress');
      this.hold(new Hold('session_not_verified'));
      try { await this.verify(); await this.store.save(); }
      catch (error) { this.hold(error); throw new Hold(this.reason); }
      // Identity alone grants no inbox freshness or outgoing authorization.
      this.state = 'initializing'; this.reason = 'baseline_pending';
      return this.health();
    });
  }
  async registerRecipient(input) {
    return this.serialized(async () => {
      if (!this.demoMode) throw new Hold('demo_required');
      if (!input || typeof input !== 'object' || Array.isArray(input) ||
          Object.keys(input).some(k => !['phone', 'id', 'starts_at', 'expires_at', 'continuous', 'expected_scope'].includes(k)) ||
          ('continuous' in input && input.continuous !== true) ||
          (input.continuous === true && (!this.signupEnabled || input.expires_at !== '9999-12-31T23:59:59.999000+00:00')) ||
          !/^\+1[2-9]\d{9}$/.test(input.phone || '') || input.phone === this.expectedPhone ||
          !/^[a-f0-9]{32}$/.test(input.id || '') || !/^[a-f0-9]{64}$/.test(input.expected_scope || '') ||
          typeof input.starts_at !== 'string' || typeof input.expires_at !== 'string' ||
          !/(?:Z|[+-]\d{2}:\d{2})$/.test(input.starts_at) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(input.expires_at) ||
          !Number.isFinite(Date.parse(input.starts_at)) || !Number.isFinite(Date.parse(input.expires_at)) ||
          Date.parse(input.expires_at) <= Date.parse(this.now()) ||
          Date.parse(input.starts_at) > Date.parse(this.now()) + 60000 ||
          Date.parse(input.expires_at) <= Date.parse(input.starts_at) ||
          (input.continuous !== true && Date.parse(input.expires_at) - Date.parse(input.starts_at) > 7200000)) throw new Hold('invalid_demo_registration', 400);
      if (this.pendingPreparation()) throw new Hold('preparation_in_progress');
      const candidate = { ...this.testSessions, [input.phone]: {id:input.id, starts_at:input.starts_at, expires_at:input.expires_at,
        ...(input.continuous === true ? {continuous:true} : {})} };
      const candidateHash = scopeHash(candidate);
      // Idempotent recovery after sidecar success/backend crash. No new session
      // may overwrite an unrelated scope change or consume a send preparation.
      if (scopeHash(this.testSessions) !== input.expected_scope && candidateHash !== scopeHash(this.testSessions)) throw new Hold('demo_scope_mismatch');
      if (Object.keys(candidate).length > 200 || new Set(Object.values(candidate).map(s => s.id)).size !== Object.keys(candidate).length) throw new Hold('demo_scope_limit');
      this.store.data.demo_activation ||= {};
      this.store.data.demo_activation[input.phone] ||= this.testSessions[input.phone]?.starts_at || input.starts_at;
      this.store.data.demo_sessions = candidate;
      try { await this.store.save(); }
      catch { this.hold(new Hold('state_unavailable')); throw new Hold('state_unavailable', 503); }
      this.testSessions = candidate;
      this.allowedPhones = new Set(Object.keys(candidate));
      this.browser.allowedPhones = [...this.allowedPhones];
      this.hold(new Hold('demo_scope_changed_check_inbox'));
      return { scope_fingerprint: candidateHash, registered: true, ready: false, delivery_verified: false };
    });
  }
  sessionPermits(request) {
    const spec = this.testSessions[request.to], now = Date.parse(this.now());
    return !!(spec && (!spec.continuous || this.signupEnabled) && request.idempotency_key.startsWith(`GV${spec.id}:`) &&
      Date.parse(spec.starts_at) <= now && now < Date.parse(spec.expires_at) &&
      Date.parse(request.not_after) <= Date.parse(spec.expires_at));
  }
  async recipientProbe(input) {
    return this.serialized(async () => {
      if (!this.demoMode || !input || Object.keys(input).sort().join(',') !== 'phone,session_id' ||
          !this.allowedPhones.has(input.phone) || this.testSessions[input.phone]?.id !== input.session_id ||
          !this.sessionPermits({to:input.phone,idempotency_key:`GV${input.session_id}:probe`,not_after:this.now()})) {
        throw new Hold('invalid_recipient_probe',400);
      }
      if (this.pendingPreparation()) throw new Hold('preparation_in_progress');
      try {
        await this.verify();
        const composer = await this.browser.prepareRecipient(input.phone);
        if (await composer.count() !== 1 || !await composer.isVisible() || await composer.inputValue() !== '' ||
            !await this.browser.recipientVerified(input.phone)) throw new Hold('recipient_probe_not_verified');
        return {status:'verified',native_submission_attempted:false,recipient_verified:true};
      } catch (error) {
        this.hold(error);
        return {status:'held',reason_code:this.reason,native_submission_attempted:false,
          ...(this.browser.recipientPreparationDiagnostic ?
            {preparation_diagnostic:this.browser.recipientPreparationDiagnostic}: {})};
      }
    });
  }
  async presendAbsence(input) {
    return this.serialized(async () => {
      const fields = ['body_hash','idempotency_key','reason_code','session_id','to'];
      if (!this.demoMode || !input || Object.keys(input).sort().join(',') !== fields.join(',') ||
          typeof input.idempotency_key !== 'string' || !/^[A-Za-z0-9:_-]{8,128}$/.test(input.idempotency_key) ||
          !/^[a-f0-9]{64}$/.test(input.body_hash || '') || input.reason_code !== 'recipient_choice_wait_unavailable' ||
          !this.allowedPhones.has(input.to) || this.testSessions[input.to]?.id !== input.session_id ||
          !this.sessionPermits({...input,not_after:this.now()})) throw new Hold('invalid_presend_observation',400);
      if (this.pendingPreparation()) throw new Hold('preparation_in_progress');
      const key = hash(input.idempotency_key);
      if (this.store.data.sends[key] || Object.values(this.store.data.sends).some(record=>['pending','uncertain'].includes(record.status))) {
        throw new Hold('submission_record_exists');
      }
      await this.verify();
      const proof = {submission_key_hash:key,body_hash:input.body_hash,session_id:input.session_id,
        sender_fingerprint:hash(`${this.identity.email.toLowerCase()}\n${this.identity.phone}`),
        scope_fingerprint:scopeHash(this.testSessions),reason_code:input.reason_code,
        ledger_absent:true,original_key_disabled:true,native_submission_attempted:false};
      this.store.data.presend_recoveries ||= {};
      const previous = this.store.data.presend_recoveries[key];
      if (previous && JSON.stringify(previous) !== JSON.stringify(proof)) throw new Hold('presend_observation_changed');
      this.store.data.presend_recoveries[key] = proof;
      try { await this.store.save(); }
      catch { this.hold(new Hold('state_unavailable')); throw new Hold('state_unavailable',503); }
      return {status:'unsubmitted',proof:{...proof,observed_at:this.now()}};
    });
  }
  async prepare(input) {
    const request = validateSend(input);
    return this.serialized(async () => {
      const key = hash(request.idempotency_key), digest = hash(`${request.to}\0${request.body}\0${request.not_after}`);
      if (this.store.data.presend_recoveries?.[key]) return {status:'rejected',reason_code:'original_key_disabled_after_review_recovery'};
      const previous = this.store.data.sends[key];
      if (previous) {
        if (previous.digest !== digest) throw new Hold('idempotency_conflict', 409);
        return publicResult(previous);
      }
      if (this.demoMode && !this.sessionPermits(request)) return { status: 'rejected', reason_code: 'demo_session_not_authorized' };
      if (!this.allowedPhones.has(request.to)) return { status: 'rejected', reason_code: 'recipient_not_allowed' };
      const expiredBeforePrepare = await this.rejectExpired(request, key, digest);
      if (expiredBeforePrepare) return expiredBeforePrepare;
      if (Date.parse(request.not_after) - Date.parse(this.now()) > 30000) return { status: 'rejected', reason_code: 'authorization_window_too_long' };
      const pending = this.pendingPreparation();
      if (pending?.key === key && pending.digest !== digest) throw new Hold('idempotency_conflict', 409);
      if (pending) return pending.key === key && pending.digest === digest ? { status: 'prepared' } :
        { status: 'rejected', reason_code: 'preparation_in_progress' };
      if (this.state !== 'ready') return { status: 'rejected', reason_code: this.reason || 'transport_not_ready' };
      try { await this.verify(); await this.browser.prepareSend(request.to, request.body); }
      catch (error) { this.hold(error); return { status: 'rejected', reason_code: this.reason }; }
      const expiredAfterPrepare = await this.rejectExpired(request, key, digest);
      if (expiredAfterPrepare) return expiredAfterPrepare;
      this.preparation = { key, digest, not_after: request.not_after };
      return { status: 'prepared' };
    });
  }
  async rejectExpired(request, key, digest) {
    if (Date.parse(request.not_after) > Date.parse(this.now())) return null;
    const rejected = { digest, status: 'rejected', reason_code: 'authorization_expired', created_at: this.now() };
    this.store.data.sends[key] = rejected;
    await this.store.save();
    return publicResult(rejected);
  }
  async send(input) {
    const request = validateSend(input);
    return this.serialized(async () => {
      const key = hash(request.idempotency_key), digest = hash(`${request.to}\0${request.body}\0${request.not_after}`);
      if (this.store.data.presend_recoveries?.[key]) return {status:'rejected',reason_code:'original_key_disabled_after_review_recovery'};
      const previous = this.store.data.sends[key];
      if (previous) {
        if (previous.digest !== digest) throw new Hold('idempotency_conflict', 409);
        return publicResult(previous);
      }
      const expired = await this.rejectExpired(request, key, digest);
      if (expired) return expired;
      const pending = this.pendingPreparation();
      if (pending?.key === key && pending.digest !== digest) throw new Hold('idempotency_conflict', 409);
      if (!pending || pending.key !== key || pending.digest !== digest) return { status: 'rejected', reason_code: 'send_not_prepared' };
      this.preparation = null;
      if (this.demoMode && !this.sessionPermits(request)) return { status: 'rejected', reason_code: 'demo_session_not_authorized' };
      if (!this.allowedPhones.has(request.to) || this.state !== 'ready') return { status: 'rejected', reason_code: 'transport_not_ready' };
      const record = { digest, status: 'pending', created_at: this.now() };
      this.store.data.demo_started ||= {};
      this.store.data.demo_started[request.to] = true;
      this.store.data.sends[key] = record;
      try { await this.store.save(); }
      catch { this.hold(new Hold('state_unavailable')); Object.assign(record, { status: 'uncertain', reason_code: 'state_unavailable' }); return publicResult(record); }
      try {
        const result = await this.browser.submitSend(request.to, request.body, request.not_after, this.identity);
        if (!['submitted', 'uncertain', 'rejected'].includes(result.status)) throw new Hold('invalid_browser_result');
        Object.assign(record, result);
      } catch { Object.assign(record, { status: 'uncertain', reason_code: 'submission_unconfirmed' }); }
      // No retry after the click boundary, including a disconnect or timeout.
      try { await this.store.save(); }
      catch { this.hold(new Hold('state_unavailable')); return { status: 'uncertain', reason_code: 'state_unavailable' }; }
      if (record.status === 'uncertain') this.hold(new Hold('submission_unconfirmed'));
      return publicResult(record);
    });
  }
  async reconcile(input) {
    if(!input||typeof input!=='object'||Array.isArray(input)||Object.keys(input).some(k=>!['idempotency_key','to','body','session_id','claim_created_at'].includes(k))
      ||typeof input.idempotency_key!=='string'||!/^[A-Za-z0-9:_-]{8,128}$/.test(input.idempotency_key)
      ||typeof input.to!=='string'||!/^\+1[2-9]\d{9}$/.test(input.to)||typeof input.body!=='string'||!input.body.trim()
      ||input.body.length>1600||input.body.includes('\0')||typeof input.claim_created_at!=='string'
      ||!Number.isFinite(Date.parse(input.claim_created_at))) throw new Hold('invalid_reconciliation_request',400);
    validateOutgoingStyle(input.body);
    return this.serialized(async()=>{
      const selected=this.testSessions[input.to],key=hash(input.idempotency_key),record=this.store.data.sends[key];
      if(!this.demoMode||!this.allowedPhones.has(input.to)||!selected||selected.id!==input.session_id
        ||!input.idempotency_key.startsWith(`GV${selected.id}:`)) throw new Hold('reconciliation_scope_not_verified');
      if(this.pendingPreparation()) throw new Hold('preparation_in_progress');
      if(!record||!['uncertain','pending','submitted'].includes(record.status)||!/^[a-f0-9]{64}$/.test(record.digest)
        ||!Number.isFinite(Date.parse(record.created_at))) throw new Hold('reconciliation_claim_not_found');
      if(record.reconciliation){
        const proof=record.reconciliation;
        if(proof.body_hash!==hash(input.body)||proof.session_id!==input.session_id||proof.claim_created_at!==input.claim_created_at) throw new Hold('reconciliation_proof_conflict');
        await this.verify();return {status:'submitted',proof};
      }
      if(record.status==='submitted') throw new Hold('reconciliation_claim_not_found');
      await this.verify();
      const observed=await this.browser.observeSubmission({...input,reserved_at:record.created_at});
      if(observed.body_hash!==hash(input.body)) throw new Hold('reconciliation_message_not_verified');
      const proof={...observed,submission_key_hash:key,session_id:input.session_id,claim_created_at:input.claim_created_at,
        ledger_created_at:record.created_at,original_digest:record.digest,digest_verification:'opaque_original_preserved',
        original_status:record.status,original_reason_code:record.reason_code||null,observed_at:this.now(),
        sender_fingerprint:hash(`${this.identity.email.toLowerCase()}\n${this.identity.phone}`)};
      const original={...record};
      Object.assign(record,{status:'submitted',reason_code:'observed_exact_google_voice_submission',reconciliation:proof});
      try{await this.store.save();}catch{
        this.store.data.sends[key]=original;this.hold(new Hold('state_unavailable'));throw new Hold('state_unavailable',503);
      }
      this.state='reconnect_required';this.reason='reconciliation_complete_intake_required';
      return {status:'submitted',proof};
    });
  }
  inbound(cursor = '0') {
    if (!/^\d{1,15}$/.test(cursor) || Number(cursor) > this.store.data.next_cursor - 1) throw new Hold('invalid_cursor', 400);
    const page = this.store.data.inbound.filter(m => m.cursor > Number(cursor)).slice(0, 100);
    return { messages: page.map(({ cursor: _, ...message }) => message),
      cursor: String(page.at(-1)?.cursor ?? Number(cursor)), state: this.state };
  }
  storedSignupInput(input) {
    // Read ONE already-durable input. Never scan a browser, reset a cursor,
    // recreate a receipt or expose unrelated participant history.
    if (!input || typeof input !== 'object' || Array.isArray(input) ||
        Object.keys(input).sort().join(',') !== 'id,phone,session_id' ||
        typeof input.id !== 'string' || !input.id.length || input.id.length > 256 ||
        typeof input.phone !== 'string' || typeof input.session_id !== 'string') throw new Hold('invalid_signup_input_request',400);
    const spec=this.testSessions[input.phone];
    if (!this.demoMode || !this.signupEnabled || !this.allowedPhones.has(input.phone) ||
        !spec?.continuous || spec.id !== input.session_id ||
        Date.parse(spec.starts_at)>Date.parse(this.now()) || Date.parse(spec.expires_at)<=Date.parse(this.now())) throw new Hold('signup_input_not_authorized',409);
    const matches=this.store.data.inbound.filter(item=>item.id===input.id);
    if(matches.length!==1 || matches[0].phone!==input.phone ||
        !Number.isFinite(Date.parse(matches[0].received_at)) ||
        Date.parse(matches[0].received_at)<Date.parse(spec.starts_at) ||
        Date.parse(matches[0].received_at)>=Date.parse(spec.expires_at)) throw new Hold('signup_input_not_found',409);
    const {cursor:_,...message}=matches[0];
    return {message:structuredClone(message),session_id:spec.id};
  }
}
function publicResult(record) {
  return { status: record.status, ...(record.provider_id ? { provider_id: record.provider_id } : {}),
    ...(record.reason_code ? { reason_code: record.reason_code } : {}) };
}
