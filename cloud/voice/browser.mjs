import { chromium } from 'playwright';
import { join } from 'node:path';
import { access, lstat, mkdir, readFile, open, rename, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { Hold, hash, validateOutgoingStyle } from './core.mjs';

// A dedicated cloud profile must keep standard session cookies on normal exit.
// Chromium's transient --restore-last-session does not persist this preference.
// Website login is separate from browser-profile sign-in. Disable only the
// latter, whose account reconciliation can log out websites without usable
// browser OAuth tokens. This is BrowserSignin=0's standard desktop preference.
export async function prepareSessionRestoration(directory) {
  const profile = join(directory, 'profile');
  const target = join(profile, 'Default', 'Preferences');
  let temporary;
  try {
    for (const path of [profile, join(profile, 'Default'), target]) {
      try { if ((await lstat(path)).isSymbolicLink()) throw new Hold('profile_state_unavailable'); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    try { await lstat(join(profile, 'SingletonLock')); throw new Hold('profile_in_use'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    let prefs = {};
    try { prefs = JSON.parse(await readFile(target, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (!prefs || typeof prefs !== 'object' || Array.isArray(prefs) ||
        ['session', 'signin'].some(key => prefs[key] !== undefined &&
          (!prefs[key] || typeof prefs[key] !== 'object' || Array.isArray(prefs[key])))) {
      throw new Hold('profile_state_unavailable');
    }
    if (prefs.session?.restore_on_startup === 1 && prefs.signin?.allowed_on_next_startup === false) return;
    await mkdir(join(profile, 'Default'), { recursive: true, mode: 0o700 });
    temporary = join(profile, 'Default', `.voice-session-${randomUUID()}`);
    const file = await open(temporary, 'wx', 0o600);
    try {
      await file.writeFile(JSON.stringify({ ...prefs,
        session: { ...prefs.session, restore_on_startup: 1 },
        signin: { ...prefs.signin, allowed_on_next_startup: false } }));
      await file.sync();
    } finally { await file.close(); }
    await rename(temporary, target);
  } catch (error) {
    if (temporary) await unlink(temporary).catch(() => {});
    if (error instanceof Hold) throw error;
    throw new Hold('profile_state_unavailable');
  }
}

// Public UI selector facts corroborated by the MIT-licensed googlevoice-mcp
// selectors.ts (April 2026). Google supplies no supported SMS automation API.
// A changed/ambiguous UI always produces a hold, never an alternative send.
export const selectors = Object.freeze({
  signedIn: '[role="button"][aria-label^="Google Account:"]',
  threads: 'gv-thread-list-item',
  bubbles: 'gv-text-message-item',
  text: '.subject-content-container.bubble',
  compose: 'textarea[placeholder="Type a message"], input[placeholder="Type a message"]',
  newMessage: '[role="button"][aria-label="Send new message"]',
  recipient: 'input[placeholder="Type a name or phone number"]',
  recipientChoice: 'button#send-to-button',
  recipientChoiceLabel: '.send-to-label[aria-hidden="true"]',
  recipientRegion: 'div[role="region"][aria-label="Select recipients"]',
  searchRegion: '[role="region"][aria-label="Search results"]',
  noSearchResults: 'p[gv-test-id="no-threads-text"]',
  progress: '[role="progressbar"]',
  send: 'button[aria-label="Send message"]',
});
const root = 'https://voice.google.com/u/0';
const normalizePhone = text => {
  const digits = text.replace(/\D/g, '');
  return digits.length === 10 ? `+1${digits}` : digits.length === 11 && digits[0] === '1' ? `+${digits}` : null;
};

export function absoluteTimestamp(values) {
  for (const value of values.filter(Boolean)) {
    if (/^\d{13}$/.test(value)) return new Date(Number(value)).toISOString();
    if (/^\d{10}$/.test(value)) return new Date(Number(value) * 1000).toISOString();
    // Refuse relative labels such as "Today", weekdays or bare clock times.
    if (!/\b20\d{2}\b/.test(value)) continue;
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return new Date(parsed).toISOString();
  }
  throw new Hold('message_timestamp_unavailable');
}

export function normalizeBubbles(rows, thread, phone) {
  const occurrences = new Map();
  return rows.filter(row => row.incoming).map(row => {
    if (!row.text?.trim() || !row.directionKnown) throw new Hold('message_format_changed');
    const receivedAt = absoluteTimestamp(row.timestamps);
    const fingerprint = `${thread}:${receivedAt}:${row.text}`;
    const occurrence = occurrences.get(fingerprint) || 0;
    occurrences.set(fingerprint, occurrence + 1);
    return { id: `${thread}:${row.providerId || `${hash(fingerprint)}:${occurrence}`}`,
      phone, body: row.text, received_at: receivedAt };
  });
}

export class VoiceBrowser {
  constructor({ directory, executablePath, allowedPhones, demoMode = false }) {
    this.demoMode = demoMode;
    this.directory = directory; this.executablePath = executablePath; this.allowedPhones = allowedPhones;
  }
  async start() {
    await this.assertProfileAvailable();
    await prepareSessionRestoration(this.directory);
    await this.assertProfileAvailable();
    this.context = await chromium.launchPersistentContext(join(this.directory, 'profile'), {
      ...(this.executablePath ? { executablePath: this.executablePath } : {}),
      // Required even in demo mode. An unsupported sandbox stops startup;
      // never retry with --no-sandbox or expose an override for credentials.
      chromiumSandbox: true,
      // The server owns ordered, idempotent shutdown. Playwright's concurrent
      // signal handlers can reenter gracefulClose and force-kill Chromium.
      handleSIGTERM: false, handleSIGINT: false,
      // Match the human login browser's native Chromium password-store selection.
      // Preserve every other Playwright default, including automation indicators.
      ignoreDefaultArgs: ['--password-store=basic', '--use-mock-keychain'],
      headless: true, locale: 'en-US', timezoneId: 'UTC',
      viewport: { width: 1280, height: 900 },
      // No CDP port, no traces, video, screenshots or credentials in logs.
    });
    this.page = this.context.pages()[0] || await this.context.newPage();
    this.page.setDefaultTimeout(10000);
  }
  async close() { await this.context?.close(); }
  async navigate(path) {
    this.prepared = null;
    if (!this.page || this.page.isClosed()) throw new Hold('browser_unavailable');
    await this.page.goto(`${root}/${path}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    try { await this.page.locator(selectors.signedIn).waitFor({ state: 'visible' }); }
    catch { throw new Hold('reconnect_required'); }
    if (new URL(this.page.url()).hostname !== 'voice.google.com' || this.page.url().includes('/onboarding')) {
      throw new Hold('reconnect_required');
    }
  }
  async importSession(cookies) {
    await this.context.clearCookies();
    await this.context.addCookies(cookies);
  }
  async clearSession() { await this.context.clearCookies(); await this.page.goto('about:blank'); }
  async assertProfileAvailable() {
    try { await access(join(this.directory, 'manual-login.active')); }
    catch (error) { if (error.code === 'ENOENT') return; throw new Hold('profile_state_unavailable'); }
    throw new Hold('manual_login_active');
  }
  async identity() {
    await this.assertProfileAvailable();
    await this.navigate('settings');
    return this.readIdentity(this.page);
  }
  async readIdentity(page) {
    const observed = await page.evaluate(() => {
      const visible = e => !!(e && e.getClientRects().length);
      const accountLabels = [...document.querySelectorAll('[aria-label]')]
        .filter(visible).map(e => e.getAttribute('aria-label'))
        .filter(label => /^Google Account:/i.test(label || ''));
      // Observed Settings DOM, October5: the account number label is a div,
      // not a heading. Scope to its component and visible formatted span;
      // exclude duplicated visually-hidden digits and all linked numbers.
      const sections = [...document.querySelectorAll('gv-account-number')].filter(visible)
        .flatMap(section => [...section.querySelectorAll('.phone-number [aria-hidden="true"]')]
          .filter(visible).map(span => span.textContent));
      return { accountLabels, sections };
    });
    const emails = [...new Set(observed.accountLabels.flatMap(label => label.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || []))];
    const phones = [...new Set(observed.sections.flatMap(text =>
      (text.match(/(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}/g) || []).map(normalizePhone)))];
    if (emails.length !== 1 || phones.length !== 1 || !phones[0]) throw new Hold('identity_not_observable');
    return { email: emails[0], phone: phones[0] };
  }
  async verifyPreparedIdentity(expected) {
    await this.assertProfileAvailable();
    if (!expected?.email || !expected.phone) throw new Hold('identity_not_observable');
    // A second private page verifies both sender identifiers without navigating
    // or altering the exact reviewed recipient/composer on the submission page.
    const verification = await this.context.newPage();
    try {
      verification.setDefaultTimeout(10000);
      await verification.goto(`${root}/settings`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await verification.locator(selectors.signedIn).waitFor({ state: 'visible' });
      const url = new URL(verification.url());
      if (url.hostname !== 'voice.google.com' || url.pathname.includes('/onboarding')) throw new Hold('reconnect_required');
      const identity = await this.readIdentity(verification);
      if (identity.email.toLowerCase() !== expected.email.toLowerCase() || identity.phone !== expected.phone) throw new Hold('account_mismatch');
    } finally { await verification.close(); }
  }
  async rows() {
    return this.page.locator(selectors.bubbles).evaluateAll(elements => elements.map(element => {
      const incoming = element.matches('.incoming') || !!element.querySelector('.incoming');
      const outgoing = element.matches('.outgoing') || !!element.querySelector('.outgoing');
      const timestamp = element.querySelector('.sender-timestamp .timestamp');
      const time = element.querySelector('time');
      const text = element.querySelector('.subject-content-container.bubble')?.textContent || '';
      return { incoming, directionKnown: incoming !== outgoing, text,
        providerId: element.getAttribute('data-message-id') || '',
        timestamps: [element.getAttribute('data-timestamp'), time?.getAttribute('datetime'),
          timestamp?.getAttribute('title'), timestamp?.getAttribute('aria-label'), timestamp?.textContent?.trim()],
        failed: /not delivered|failed to send|couldn.t send/i.test(element.innerText || '') };
    }));
  }
  async scan(phones = this.allowedPhones, { emptyPhones = [] } = {}) {
    const messages = [];
    // Navigate only explicitly allowed test participants. Do not enumerate
    // personal threads, contacts, or message bodies outside this allowlist.
    for (const phone of phones) {
      if (!this.allowedPhones.includes(phone)) throw new Hold('recipient_not_allowed');
      const thread = `t.${phone}`;
      await this.navigate(`messages?itemId=${encodeURIComponent(thread)}`);
      if (new URL(this.page.url()).searchParams.get('itemId') !== thread) throw new Hold('thread_not_observable');
      // Do not interpret a still-loading page as a conversation. Every inbound
      // item also requires an absolute timestamp, so late-rendered history is
      // discarded against the durable activation baseline by the core.
      try { await this.page.locator(selectors.compose).waitFor({ state: 'visible' }); }
      catch (error) {
        // A missing composer alone never establishes an empty history. The
        // core supplies this scope only before its durable first-send marker.
        if (error.name !== 'TimeoutError' || !emptyPhones.includes(phone) || !/^\+1\d{10}$/.test(phone)) throw new Hold('thread_not_observable');
        await this.verifyEmptyFirstRecipient(phone);
        continue;
      }
      const count = await this.page.locator(selectors.bubbles).count();
      if (!count) continue;
      if (this.demoMode && count > 100) throw new Hold('demo_thread_limit_exceeded');
      const rows = await this.rows();
      if (rows.some(row => !row.directionKnown)) throw new Hold('message_direction_unavailable');
      messages.push(...normalizeBubbles(rows, thread, phone));
    }
    return messages;
  }
  async verifyEmptyFirstRecipient(phone) {
    if (!this.allowedPhones.includes(phone)) throw new Hold('recipient_not_allowed');
    const query = new URLSearchParams({ from: '[]', q: JSON.stringify([phone]) });
    await this.navigate(`search?${query}`);
    const region = this.page.locator(selectors.searchRegion);
    const absent = region.locator(selectors.noSearchResults);
    try { await absent.waitFor({ state: 'visible' }); }
    catch { throw new Hold('thread_not_observable'); }
    const exactSearch = () => {
      const url = new URL(this.page.url());
      return url.origin === 'https://voice.google.com' && /^\/u\/\d+\/search$/.test(url.pathname)
        && url.searchParams.get('from') === '[]' && url.searchParams.get('q') === JSON.stringify([phone])
        && [...url.searchParams].length === 2;
    };
    if (!exactSearch() || await region.count() !== 1 || !await region.isVisible()
      || await absent.count() !== 1 || !await absent.isVisible()
      || (await absent.textContent())?.trim() !== `No search results found for ${phone}`
      || !await this.page.locator(selectors.signedIn).isVisible()
      || await this.page.locator(selectors.progress).count() !== 0
      || await this.page.locator(selectors.threads).count() !== 0
      || await this.page.locator(selectors.bubbles).count() !== 0
      || !exactSearch()) throw new Hold('thread_not_observable');
    const composer = await this.prepareRecipient(phone);
    // The no-results proof must also resolve to the observed first-message
    // draft, never an established thread or a stale body. No send occurs here.
    if (new URL(this.page.url()).searchParams.get('itemId') !== 'draft'
      || !await composer.isVisible() || await composer.inputValue() !== ''
      || await this.page.locator(selectors.bubbles).count() !== 0
      || await this.page.locator(selectors.progress).count() !== 0
      || !await this.recipientVerified(phone)) throw new Hold('thread_not_observable');
  }
  async prepareRecipient(to) {
    await this.navigate('messages');
    await this.page.locator(selectors.newMessage).click();
    await this.page.locator(selectors.recipient).fill(to);
    const choice = this.page.locator(selectors.recipientChoice);
    await choice.waitFor({ state: 'visible' });
    const label = choice.locator(selectors.recipientChoiceLabel);
    if (await choice.count() !== 1 || await label.count() !== 1 || !await label.isVisible() || normalizePhone(await label.textContent()) !== to) throw new Hold('recipient_not_verified');
    await choice.click();
    await this.page.keyboard.press('Escape');
    if (!await this.recipientVerified(to)) throw new Hold('recipient_not_verified');
    const composer = this.page.locator(selectors.compose);
    if (await composer.count() !== 1) throw new Hold('composer_ambiguous');
    return composer;
  }
  async prepareSend(to, body) {
    validateOutgoingStyle(body);
    const composer = await this.prepareRecipient(to);
    await composer.fill(body);
    const send = this.page.locator(selectors.send);
    if (await send.count() !== 1 || !await send.isEnabled()) throw new Hold('send_unavailable');
    this.prepared = { to, body, before: (await this.rows()).filter(row => !row.incoming && row.text === body).length };
  }
  async recipientVerified(to) {
    const url = new URL(this.page.url());
    if (url.origin !== 'https://voice.google.com') return false;
    const item = url.searchParams.get('itemId');
    if (item === `t.${to}`) return true; // Established-thread behavior stays unchanged.
    if (item !== 'draft') return false;
    // First-message drafts retain itemId=draft. The observed selected-recipient
    // region must expose exactly one numeric chip, both now and before click.
    const region = this.page.locator(selectors.recipientRegion);
    if (await region.count() !== 1 || !await region.isVisible()) return false;
    const chips = region.locator('mat-chip-row');
    if (await chips.count() !== 1) return false;
    const label = chips.locator('.chip-name[aria-hidden="true"]');
    if (await label.count() !== 1 || !await label.isVisible()) return false;
    const text = (await label.textContent())?.replace(/[\u202a-\u202e\u2066-\u2069]/g, '').trim();
    return !!(text && /^[+()\d\s.-]+$/.test(text) && normalizePhone(text) === to);
  }
  async submitSend(to, body, notAfter, expectedIdentity) {
    if (!this.prepared || this.prepared.to !== to || this.prepared.body !== body) throw new Hold('send_not_prepared');
    const before = this.prepared.before;
    this.prepared = null;
    validateOutgoingStyle(body);
    if (this.demoMode) {
      try { await this.verifyPreparedIdentity(expectedIdentity); }
      catch { return { status: 'rejected', reason_code: 'final_identity_not_verified' }; }
    }
    let recipientVerified = false;
    try { recipientVerified = await this.recipientVerified(to); } catch { /* Ambiguous/changed DOM holds. */ }
    if (!recipientVerified) {
      return { status: 'rejected', reason_code: 'recipient_not_verified' };
    }
    const composer = this.page.locator(selectors.compose);
    if (await composer.count() !== 1 || await composer.inputValue() !== body) {
      return { status: 'rejected', reason_code: 'composer_changed' };
    }
    const send = this.page.locator(selectors.send);
    if (await send.count() !== 1 || !await send.isEnabled()) return { status: 'rejected', reason_code: 'send_unavailable' };
    try { if (!await this.recipientVerified(to)) return { status: 'rejected', reason_code: 'recipient_not_verified' }; }
    catch { return { status: 'rejected', reason_code: 'recipient_not_verified' }; }
    // The backend binds this deadline to the approval, test-session expiry,
    // queue age and quiet-hours boundary. Check again after browser preparation
    // and durable reservation, directly before the sole submission action.
    const remaining = Date.parse(notAfter) - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0) return { status: 'rejected', reason_code: 'authorization_expired' };
    await send.click({ timeout: Math.min(remaining, 10000) });
    // This confirms the message appeared in the Voice UI, not carrier delivery.
    // No click retries and no automatic resend when confirmation is ambiguous.
    try {
      await this.page.waitForFunction(({ body, before }) => {
        const bubbles = [...document.querySelectorAll('gv-text-message-item')]
          .filter(el => !el.matches('.incoming') && !el.querySelector('.incoming'))
          .filter(el => el.querySelector('.subject-content-container.bubble')?.textContent === body);
        const composer = document.querySelector('textarea[placeholder="Type a message"],input[placeholder="Type a message"]');
        return bubbles.length > before && composer?.value === '';
      }, { body, before }, { timeout: 15000 });
      const matching = (await this.rows()).filter(row => !row.incoming && row.text === body);
      if (matching.at(-1)?.failed) return { status: 'rejected', reason_code: 'google_rejected_message' };
      return { status: 'submitted', reason_code: 'visible_in_google_voice' };
    } catch { return { status: 'uncertain', reason_code: 'submission_unconfirmed' }; }
  }
}
