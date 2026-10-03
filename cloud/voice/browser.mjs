import { chromium } from 'playwright';
import { join } from 'node:path';
import { Hold, hash, validateOutgoingStyle } from './core.mjs';

// Public UI selector facts corroborated by the MIT-licensed googlevoice-mcp
// selectors.ts (April 2026). Google supplies no supported SMS automation API.
// A changed/ambiguous UI always produces a hold, never an alternative send.
export const selectors = Object.freeze({
  signedIn: '[gv-test-id="sidenav-messages"]',
  threads: 'gv-thread-list-item',
  bubbles: 'gv-text-message-item',
  text: '.subject-content-container.bubble',
  compose: 'textarea[placeholder="Type a message"], input[placeholder="Type a message"]',
  newMessage: '[aria-label="Send new message"]',
  recipient: 'input[placeholder="Type a name or phone number"]',
  recipientChoice: '.send-to-label',
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
  constructor({ directory, executablePath, allowedPhones }) {
    this.directory = directory; this.executablePath = executablePath; this.allowedPhones = allowedPhones;
  }
  async start() {
    this.context = await chromium.launchPersistentContext(join(this.directory, 'profile'), {
      ...(this.executablePath ? { executablePath: this.executablePath } : {}),
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
  async identity() {
    await this.navigate('settings');
    const observed = await this.page.evaluate(() => {
      const visible = e => !!(e && e.getClientRects().length);
      const accountLabels = [...document.querySelectorAll('[aria-label]')]
        .filter(visible).map(e => e.getAttribute('aria-label'))
        .filter(label => /^Google Account:/i.test(label || ''));
      // Only the Voice-number section, never the linked/forwarding number list.
      const headings = [...document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')]
        .filter(e => visible(e) && /^(Google Voice number|Your Google Voice number)$/i.test(e.textContent.trim()));
      const sections = headings.map(heading => {
        let parent = heading.parentElement;
        for (let i = 0; parent && i < 3; i++, parent = parent.parentElement) {
          const text = parent.innerText || '';
          if (/Linked numbers|Call forwarding/i.test(text)) break;
          if (/(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}/.test(text)) return text;
        }
        return '';
      });
      return { accountLabels, sections };
    });
    const emails = [...new Set(observed.accountLabels.flatMap(label => label.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || []))];
    const phones = [...new Set(observed.sections.flatMap(text =>
      (text.match(/(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}/g) || []).map(normalizePhone)))];
    if (emails.length !== 1 || phones.length !== 1 || !phones[0]) throw new Hold('identity_not_observable');
    return { email: emails[0], phone: phones[0] };
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
  async scan() {
    const messages = [];
    // Navigate only explicitly allowed test participants. Do not enumerate
    // personal threads, contacts, or message bodies outside this allowlist.
    for (const phone of this.allowedPhones) {
      const thread = `t.${phone}`;
      await this.navigate(`messages?itemId=${encodeURIComponent(thread)}`);
      if (new URL(this.page.url()).searchParams.get('itemId') !== thread) throw new Hold('thread_not_observable');
      // Do not interpret a still-loading page as a conversation. Every inbound
      // item also requires an absolute timestamp, so late-rendered history is
      // discarded against the durable activation baseline by the core.
      await this.page.locator(selectors.compose).waitFor({ state: 'visible' });
      const count = await this.page.locator(selectors.bubbles).count();
      if (!count) continue;
      const rows = await this.rows();
      if (rows.some(row => !row.directionKnown)) throw new Hold('message_direction_unavailable');
      messages.push(...normalizeBubbles(rows, thread, phone));
    }
    return messages;
  }
  async prepareSend(to, body) {
    validateOutgoingStyle(body);
    await this.navigate('messages');
    await this.page.locator(selectors.newMessage).click();
    await this.page.locator(selectors.recipient).fill(to);
    const choice = this.page.locator(selectors.recipientChoice);
    await choice.waitFor({ state: 'visible' });
    if (await choice.count() !== 1 || normalizePhone(await choice.innerText()) !== to) throw new Hold('recipient_not_verified');
    await choice.click();
    await this.page.keyboard.press('Escape');
    const composer = this.page.locator(selectors.compose);
    if (await composer.count() !== 1) throw new Hold('composer_ambiguous');
    await composer.fill(body);
    const send = this.page.locator(selectors.send);
    if (await send.count() !== 1 || !await send.isEnabled()) throw new Hold('send_unavailable');
    this.prepared = { to, body, before: (await this.rows()).filter(row => !row.incoming && row.text === body).length };
  }
  async submitSend(to, body, notAfter) {
    if (!this.prepared || this.prepared.to !== to || this.prepared.body !== body) throw new Hold('send_not_prepared');
    const before = this.prepared.before;
    this.prepared = null;
    validateOutgoingStyle(body);
    const url = new URL(this.page.url());
    if (url.origin !== 'https://voice.google.com' || url.searchParams.get('itemId') !== `t.${to}`) {
      return { status: 'rejected', reason_code: 'recipient_not_verified' };
    }
    const composer = this.page.locator(selectors.compose);
    if (await composer.count() !== 1 || await composer.inputValue() !== body) {
      return { status: 'rejected', reason_code: 'composer_changed' };
    }
    const send = this.page.locator(selectors.send);
    if (await send.count() !== 1 || !await send.isEnabled()) return { status: 'rejected', reason_code: 'send_unavailable' };
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
