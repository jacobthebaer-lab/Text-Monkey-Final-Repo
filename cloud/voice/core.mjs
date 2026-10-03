import { createHash, timingSafeEqual } from 'node:crypto';
import { mkdir, open, readFile, rename, chmod } from 'node:fs/promises';
import { join } from 'node:path';

export class Hold extends Error {
  constructor(code, status = 409) { super(code); this.code = code; this.status = status; }
}
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
  constructor({ store, browser, expectedEmail, expectedPhone, allowedPhones, now = () => new Date().toISOString() }) {
    this.store = store; this.browser = browser; this.expectedEmail = expectedEmail.toLowerCase();
    this.expectedPhone = expectedPhone; this.now = now; this.queue = Promise.resolve();
    this.allowedPhones = new Set(allowedPhones || []);
    this.state = 'reconnect_required'; this.reason = 'session_not_verified'; this.identity = null;
    this.preparation = null;
  }
  serialized(operation) {
    const result = this.queue.then(operation);
    this.queue = result.catch(() => {});
    return result;
  }
  health() {
    return { ready: this.state === 'ready', state: this.state, reason_code: this.reason, account_email: maskEmail(this.identity?.email),
      number: maskPhone(this.identity?.phone), identity_verified: !!this.identity, expected_identity_match: !!this.identity,
      identity_fingerprint: this.identity ? hash(`${this.identity.email.toLowerCase()}\n${this.identity.phone}`) : null,
      baseline_at: this.store.data.baseline_at, inbound_cursor: String(this.store.data.next_cursor - 1),
      delivery_verified: false };
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
  async poll() {
    return this.serialized(async () => {
      // Keep the prepared recipient/composer intact while the backend rereads
      // approval, opt-out, pause and event state. A preparation lasts <=30s.
      if (this.pendingPreparation()) return this.health();
      try {
        await this.verify();
        const messages = await this.browser.scan();
        const observedAt = this.now();
        const baseline = this.store.data.baseline_at;
        // A complete first scan establishes a baseline. Never enqueue history.
        for (const message of messages) {
          if (!this.allowedPhones.has(message.phone)) continue;
          const id = hash(message.id);
          if (this.store.data.seen[id]) continue;
          this.store.data.seen[id] = true;
          if (!baseline || message.received_at < baseline) continue;
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
  async prepare(input) {
    const request = validateSend(input);
    return this.serialized(async () => {
      const key = hash(request.idempotency_key), digest = hash(`${request.to}\0${request.body}\0${request.not_after}`);
      const previous = this.store.data.sends[key];
      if (previous) {
        if (previous.digest !== digest) throw new Hold('idempotency_conflict', 409);
        return publicResult(previous);
      }
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
      if (!this.allowedPhones.has(request.to) || this.state !== 'ready') return { status: 'rejected', reason_code: 'transport_not_ready' };
      const record = { digest, status: 'pending', created_at: this.now() };
      this.store.data.sends[key] = record;
      try { await this.store.save(); }
      catch { this.hold(new Hold('state_unavailable')); Object.assign(record, { status: 'uncertain', reason_code: 'state_unavailable' }); return publicResult(record); }
      try {
        const result = await this.browser.submitSend(request.to, request.body, request.not_after);
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
  inbound(cursor = '0') {
    if (!/^\d{1,15}$/.test(cursor) || Number(cursor) > this.store.data.next_cursor - 1) throw new Hold('invalid_cursor', 400);
    const page = this.store.data.inbound.filter(m => m.cursor > Number(cursor)).slice(0, 100);
    return { messages: page.map(({ cursor: _, ...message }) => message),
      cursor: String(page.at(-1)?.cursor ?? Number(cursor)), state: this.state };
  }
}
function publicResult(record) {
  return { status: record.status, ...(record.provider_id ? { provider_id: record.provider_id } : {}),
    ...(record.reason_code ? { reason_code: record.reason_code } : {}) };
}
