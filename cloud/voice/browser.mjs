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
  bubbles: 'gv-text-message-item, gv-message-item',
  text: '.subject-content-container.bubble',
  compose: 'textarea[placeholder="Type a message"], input[placeholder="Type a message"]',
  newMessage: '[role="button"][aria-label="Send new message"]',
  recipient: 'input[placeholder="Type a name or phone number"]',
  recipientChoice: 'button#send-to-button',
  recipientChoiceLabel: '.send-to-label[aria-hidden="true"]',
  recipientRegion: 'div[role="region"][aria-label="Select recipients"]',
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
      phone, body: row.text, received_at: receivedAt,
      ...(row.timestampInterval?{received_at_interval:row.timestampInterval}:{}) };
  });
}

export function parseAccessibleMessage(row) {
  if (row.containers !== 1 || row.accessibleCount !== 1 || row.bodyCount !== 1 || !row.directionKnown
    || typeof row.accessible !== 'string') {
    throw new Hold('message_format_changed');
  }
  // Observed en-US Voice accessibility protocol. The body is captured verbatim,
  // including emoji and punctuation, separate from sender/date metadata.
  const match = row.accessible.trim().match(/^Message from (you|\d(?: \d){9,10}), ([\s\S]+), (Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday), (January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}) (20\d{2}), (\d{1,2}):(\d{2}) (AM|PM)\.$/u);
  if (!match || (match[1] === 'you') === row.incoming) throw new Hold('message_format_changed');
  const [, sender, text, weekday, monthName, dayText, yearText, hourText, minuteText, period] = match;
  const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const days = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  const month = months.indexOf(monthName), day = Number(dayText), year = Number(yearText);
  const hour = Number(hourText), minute = Number(minuteText);
  if (hour < 1 || hour > 12 || minute > 59) throw new Hold('message_timestamp_unavailable');
  // The persistent browser is explicitly configured to UTC and en-US. Never
  // interpret bare clock labels or a date in the process's local timezone.
  const date = new Date(Date.UTC(year, month, day, hour % 12 + (period === 'PM' ? 12 : 0), minute));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month || date.getUTCDate() !== day
    || days[date.getUTCDay()] !== weekday) throw new Hold('message_timestamp_unavailable');
  const senderPhone = sender === 'you' ? null : normalizePhone(sender);
  if (row.incoming && !senderPhone) throw new Hold('message_format_changed');
  return { ...row, text, senderPhone, timestamps: [date.toISOString()],
    timestampInterval: {start:date.toISOString(),end:new Date(date.getTime()+60000).toISOString(),precision:'minute'} };
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
  async rows(phone) {
    const observed = await this.page.locator(selectors.bubbles).evaluateAll(elements => elements.map(element => {
      if (element.localName === 'gv-message-item') {
        const containers = element.querySelectorAll('.full-container');
        const full = containers[0];
        const accessible = full?.querySelectorAll(':scope > .container > .cdk-visually-hidden');
        const incoming = full?.matches('.incoming') || false;
        const outgoing = full?.matches('.outgoing') || false;
        return { format: 'accessible', containers: containers.length, accessibleCount: accessible?.length || 0,
          bodyCount: full?.querySelectorAll('.subject-content-container.bubble').length || 0,
          accessible: accessible?.[0]?.textContent || '', incoming, directionKnown: incoming !== outgoing,
          providerId: '', failed: /not delivered|failed to send|couldn.t send/i.test(element.innerText || '') };
      }
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
    return observed.map(row => {
      const parsed = row.format === 'accessible' ? parseAccessibleMessage(row) : row;
      if (phone && parsed.incoming && parsed.senderPhone && parsed.senderPhone !== phone) throw new Hold('message_sender_mismatch');
      return parsed;
    });
  }
  async scan(phones = this.allowedPhones, { emptyPhones = [] } = {}) {
    const messages = [];
    // Navigate only explicitly allowed test participants. Do not enumerate
    // personal threads, contacts, or message bodies outside this allowlist.
    for (const phone of phones) {
      if (!this.allowedPhones.includes(phone)) throw new Hold('recipient_not_allowed');
      const thread = `t.${phone}`;
      await this.navigate(`messages?itemId=${encodeURIComponent(thread)}`);
      if (new URL(this.page.url()).searchParams.get('itemId') !== thread) throw new Hold('thread_not_observable_direct_route');
      // Do not interpret a still-loading page as a conversation. Every inbound
      // item also requires an absolute timestamp, so late-rendered history is
      // discarded against the durable activation baseline by the core.
      try { await this.page.locator(selectors.compose).waitFor({ state: 'visible' }); }
      catch (error) {
        // A missing composer alone never establishes an empty history. The
        // core supplies this scope only before its durable first-send marker.
        if (error.name !== 'TimeoutError' || !emptyPhones.includes(phone) || !/^\+1\d{10}$/.test(phone)) throw new Hold('thread_not_observable_composer');
        await this.verifyEmptyFirstRecipient(phone);
        continue;
      }
      const count = await this.page.locator(selectors.bubbles).count();
      if (!count) continue;
      if (this.demoMode && count > 100) throw new Hold('demo_thread_limit_exceeded');
      const rows = await this.rows(phone);
      if (rows.some(row => !row.directionKnown)) throw new Hold('message_direction_unavailable');
      messages.push(...normalizeBubbles(rows, thread, phone));
    }
    return messages;
  }
  async verifyEmptyFirstRecipient(phone) {
    if (!this.allowedPhones.includes(phone)) throw new Hold('recipient_not_allowed');
    const query = new URLSearchParams({ from: '[]', q: JSON.stringify([phone]) });
    await this.navigate(`search?${query}`);
    // The observed section is named by aria-labelledby + a hidden h3. Its
    // implicit region role has no literal role/aria-label CSS attributes.
    const region = this.page.getByRole('region', { name: 'Search results', exact: true });
    const absent = region.locator(selectors.noSearchResults);
    try { await absent.waitFor({ state: 'visible' }); }
    catch { throw new Hold('thread_not_observable_search_load'); }
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
      || !exactSearch()) throw new Hold('thread_not_observable_query_proof');
    const composer = await this.prepareRecipient(phone);
    // The no-results proof must also resolve to the observed first-message
    // draft, never an established thread or a stale body. No send occurs here.
    await this.waitForEmptyDraft(phone, composer);
    const problem = await this.emptyDraftProblem(phone, composer);
    if (problem) throw new Hold(problem);
  }
  async emptyDraftProblem(phone, composer) {
    if (new URL(this.page.url()).searchParams.get('itemId') !== 'draft') return 'thread_not_observable_draft_route';
    if (await composer.count() !== 1 || !await composer.isVisible()) return 'thread_not_observable_draft_composer';
    if (await composer.inputValue() !== '') return 'thread_not_observable_draft_body';
    if (await this.page.locator(selectors.bubbles).count() !== 0) return 'thread_not_observable_draft_history';
    const progress = this.page.locator(selectors.progress);
    // Settled drafts may retain hidden Angular progress nodes. Check rendered
    // visibility for every match; a visible indicator still holds readiness.
    for (let index = 0, count = await progress.count(); index < count; index++) {
      if (await progress.nth(index).isVisible()) return 'thread_not_observable_draft_loading';
    }
    if (!await this.recipientVerified(phone)) return 'thread_not_observable_draft_recipient';
    return null;
  }
  async waitForEmptyDraft(phone, composer, timeout = 10000) {
    const deadline = Date.now() + timeout;
    let problem;
    while ((problem = await this.emptyDraftProblem(phone, composer))) {
      if (Date.now() >= deadline) throw new Hold(problem);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  async waitForRecipientProof(proof, code, timeout = 10000) {
    const deadline = Date.now() + timeout;
    while (!await proof()) {
      if (Date.now() >= deadline) throw new Hold(code);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  async prepareRecipient(to) {
    this.recipientPreparationDiagnostic = null;
    let phase = 'recipient_navigation_unavailable';
    try {
      await this.navigate('messages');
      phase = 'recipient_open_unavailable';
      await this.page.locator(selectors.newMessage).click();
      phase = 'recipient_input_unavailable';
      const recipient = this.page.locator(selectors.recipient);
      await recipient.fill('');
      // A committed chip from an earlier preparation can survive on the draft.
      // The exact single-chip proof would then reject a correct recipient, so
      // clear existing chips first. Backspace on the empty chip input removes
      // the last chip; never infer success, demand an empty region.
      const stale = this.page.locator(selectors.recipientRegion);
      if (await stale.count() === 1) {
        const chips = stale.locator('mat-chip-row');
        for (let guard = 0; guard < 12 && await chips.count() > 0; guard++) {
          await recipient.press('Backspace');
        }
        if (await chips.count() > 0) throw new Hold('recipient_input_unavailable');
      }
      // Some autocomplete controls listen to keyboard events, not input alone.
      // Use Playwright's supported typing API, then demand the same exact proofs.
      await recipient.pressSequentially(to, { delay: 40 });
      phase = 'recipient_choice_unavailable';
      const choice = this.page.locator(selectors.recipientChoice);
      phase = 'recipient_choice_wait_unavailable';
      let suggested = true;
      try {
        await choice.waitFor({ state: 'visible' });
      } catch (error) {
        if (error instanceof Hold) throw error;
        // Observed on the live account, October 2026: the picker is an Angular
        // Material chip input and no suggestion button exists anywhere on the
        // page. Keep the identical structural diagnostic, then commit the typed
        // number through the chip input and demand the same recipient proofs.
        await this.recordChoiceWaitDiagnostic(error, phase);
        suggested = false;
      }
      if (suggested) {
        phase = 'recipient_choice_unavailable';
        const label = choice.locator(selectors.recipientChoiceLabel);
        const choiceVerified = async () => await choice.count() === 1 && await label.count() === 1
          && await label.isVisible() && normalizePhone((await label.textContent()) || '') === to;
        // The button can precede its numeric label, and Angular renders the chip
        // after selection. Wait for the exact proofs, never infer them from a click.
        await this.waitForRecipientProof(choiceVerified, 'recipient_choice_not_verified');
        if (!await choiceVerified()) throw new Hold('recipient_choice_not_verified');
        phase = 'recipient_selection_unavailable';
        await choice.click();
        phase = 'recipient_escape_unavailable';
        await this.page.keyboard.press('Escape');
      } else {
        // The chip input commits the exact typed value; it never selects a
        // different contact and never reaches the message body or Send.
        phase = 'recipient_commit_unavailable';
        await recipient.press('Enter');
      }
      phase = 'recipient_verification_unavailable';
      await this.waitForRecipientProof(() => this.recipientVerified(to), 'recipient_selected_not_verified');
      if (!await this.recipientVerified(to)) throw new Hold('recipient_selected_not_verified');
      phase = 'recipient_composer_unavailable';
      const composer = this.page.locator(selectors.compose);
      if (await composer.count() !== 1) throw new Hold('composer_ambiguous');
      return composer;
    } catch (error) {
      if (error instanceof Hold) throw error;
      // Fixed operation names only; Playwright errors can contain private data.
      throw new Hold(phase);
    }
  }
  async recordChoiceWaitDiagnostic(error, phase) {
    // Safe structural facts only. Never expose exception messages, page
    // text, URLs or recipient values through the private diagnostic.
    let count = null, visible = null;
    try {
      const choices = this.page.locator(selectors.recipientChoice);
      count = await choices.count();
      if (count <= 20) {
        visible = 0;
        for (let index = 0; index < count; index++) {
          if (await choices.nth(index).isVisible()) visible++;
        }
      }
    } catch { /* The page can already be closed. */ }
    this.recipientPreparationDiagnostic = {
      phase, exception_class: error?.name === 'TimeoutError' ? 'timeout' :
        error instanceof TypeError ? 'type_error' : 'other',
      choice_count: Number.isInteger(count) && count <= 20 ? count : null,
      visible_choice_count: visible,
      page_closed: typeof this.page?.isClosed === 'function' ? this.page.isClosed() : null,
    };
  }
  async observeRecipient(to) {
    const input = this.page.locator(selectors.recipient);
    const count = await input.count();
    const result = {recipient_input_count:count,recipient_input_visible:false,recipient_input_exact:false};
    if (count !== 1) return result;
    result.recipient_input_visible = await input.isVisible();
    result.recipient_input_exact = (await input.inputValue()) === to;
    if (!result.recipient_input_visible || !result.recipient_input_exact) return result;
    return {...result,...await input.evaluate((field,phone) => {
      const visible = element => {
        const style = getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden' &&
          style.visibility !== 'collapse' && element.getClientRects().length > 0;
      };
      const structure = element => ({tag:element.tagName.toLowerCase(),
        id:(element.id || '').slice(0,100),class:(element.getAttribute('class') || '').slice(0,160),
        role:element.getAttribute('role'),test_id:element.getAttribute('gv-test-id'),
        aria_hidden:element.getAttribute('aria-hidden'),visible:visible(element)});
      const numericMatch = text => {
        const digits = (text || '').replace(/\D/g,'');
        return (digits.length === 10 ? '+1'+digits : digits.length === 11 ? '+'+digits : null) === phone;
      };
      const ancestors = [];let node=field.parentElement;
      while(node && ancestors.length<5 && !['BODY','HTML'].includes(node.tagName)) {
        ancestors.push({...structure(node),children:[...node.children].slice(0,20).map(structure)});
        node=node.parentElement;
      }
      // Bound observation to the recipient form's nearby structure. Never
      // inspect message-item bodies, other contacts' text or whole-page HTML.
      const scope=field.closest('form,gv-recipient-picker,gv-new-conversation') ||
        field.parentElement?.parentElement?.parentElement || field.parentElement;
      const lists=[...scope.querySelectorAll('gv-contact-list')];
      const controls=[...scope.querySelectorAll('button,[role="button"],[role="option"],input,mat-option')]
        .filter(element=>!element.closest('gv-message-item,gv-text-message-item'));
      const candidates=controls.slice(0,40).map(element=>({...structure(element),
        numeric_target:[...element.querySelectorAll('*')].slice(0,30).some(child=>
          child.children.length===0 && numericMatch(child.textContent)) || numericMatch(element.childNodes.length===1?element.textContent:''),
        children:[...element.children].slice(0,12).map(structure)}));
      const statuses=[...scope.querySelectorAll('[role="alert"],[role="status"],mat-error,.error')]
        .filter(element=>!element.closest('gv-message-item,gv-text-message-item')).slice(0,12)
        .map(element=>({...structure(element),text_present:!!element.textContent?.trim(),
          hint:/invalid|not valid/i.test(element.textContent || '') ? 'invalid_input' :
            /cannot send|can't send/i.test(element.textContent || '') ? 'cannot_send' :
            /no contacts|no results/i.test(element.textContent || '') ? 'no_matches' : null}));
      return {ancestors,contact_list_count:lists.length,contact_lists:lists.slice(0,5).map(structure),
        control_count:controls.length,controls:candidates,controls_truncated:controls.length>40,statuses};
    },to)};
  }
  async prepareSend(to, body) {
    validateOutgoingStyle(body);
    const composer = await this.prepareRecipient(to);
    await composer.fill(body);
    const send = this.page.locator(selectors.send);
    if (await send.count() !== 1 || !await send.isEnabled()) throw new Hold('send_unavailable');
    this.prepared = { to, body, before: (await this.rows(to)).filter(row => !row.incoming && row.directionKnown && row.text === body).length };
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
      await this.waitForRecipientProof(async () => {
        const matching = (await this.rows(to)).filter(row => !row.incoming && row.directionKnown && row.text === body);
        return matching.length > before && await composer.count() === 1 && await composer.inputValue() === ''
          && await this.recipientVerified(to);
      }, 'submission_unconfirmed', 15000);
      const matching = (await this.rows(to)).filter(row => !row.incoming && row.directionKnown && row.text === body);
      if (matching.length <= before || await composer.count() !== 1 || await composer.inputValue() !== ''
        || !await this.recipientVerified(to)) return {status:'uncertain',reason_code:'submission_unconfirmed'};
      if (matching.at(-1)?.failed) return { status: 'rejected', reason_code: 'google_rejected_message' };
      return { status: 'submitted', reason_code: 'visible_in_google_voice' };
    } catch { return { status: 'uncertain', reason_code: 'submission_unconfirmed' }; }
  }
  async observeSubmission({to,body,claim_created_at,reserved_at}) {
    await this.navigate(`messages?itemId=${encodeURIComponent(`t.${to}`)}`);
    await this.page.locator(selectors.compose).waitFor({state:'visible'});
    if (new URL(this.page.url()).searchParams.get('itemId') !== `t.${to}` || !await this.recipientVerified(to)) throw new Hold('reconciliation_recipient_not_verified');
    const rows = await this.rows(to);
    if (rows.length > 100) throw new Hold('demo_thread_limit_exceeded');
    const matches = rows.filter(row=>!row.incoming && row.directionKnown && row.text===body);
    if (matches.length!==1 || matches[0].failed || !matches[0].timestampInterval) throw new Hold('reconciliation_message_not_verified');
    const interval=matches[0].timestampInterval;
    for(const value of [claim_created_at,reserved_at]) {
      const time=Date.parse(value);
      if(!Number.isFinite(time)||time<Date.parse(interval.start)||time>=Date.parse(interval.end)) throw new Hold('reconciliation_time_not_verified');
    }
    if (!await this.recipientVerified(to)) throw new Hold('reconciliation_recipient_not_verified');
    return {body_hash:hash(body),native_timestamp_interval:interval,
      provider_item_fingerprint:hash(`${to}\0${interval.start}\0${body}`)};
  }
}
