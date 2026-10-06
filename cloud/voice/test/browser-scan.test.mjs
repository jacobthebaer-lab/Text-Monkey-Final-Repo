import test from 'node:test';
import assert from 'node:assert/strict';
import { VoiceBrowser, selectors } from '../browser.mjs';

const phone = '+12025550102';
const other = '+12025550103';
const fixture = (options = {}) => {
  const browser = new VoiceBrowser({ directory: '/unused', allowedPhones: [phone], demoMode: true });
  let stage = 'thread';
  let url = '';
  let searchUrlReads = 0;
  const actions = { searches: 0, drafts: 0, bodyFills: 0, sends: 0, rows: 0 };
  const visible = (count = 1, shown = true) => ({ count: async () => count, isVisible: async () => shown });
  const chipLabel = { ...visible(1, options.chipVisible !== false), textContent: async () => options.chip ?? '(202) 555-0102' };
  const chips = { ...visible(options.chips ?? 1), locator: () => chipLabel };
  const recipients = { ...visible(), locator: () => chips };
  const absent = {
    ...visible(options.absentCount ?? 1, options.absentVisible !== false),
    waitFor: async () => { if (options.absentTimeout) throw new Error('not observable'); },
    textContent: async () => options.absentText ?? `No search results found for ${phone}`,
  };
  const search = { ...visible(options.regions ?? 1, options.regionVisible !== false), locator: selector => {
    assert.equal(selector, selectors.noSearchResults); return absent;
  } };
  const composer = {
    ...visible(options.composers ?? 1, options.composerVisible !== false),
    waitFor: async () => {
      if (!options.existing) { const error = new Error('synthetic timeout'); error.name = options.waitError ?? 'TimeoutError'; throw error; }
    },
    inputValue: async () => options.body ?? '',
    fill: async () => { actions.bodyFills += 1; },
  };
  const choice = {
    ...visible(), waitFor: async () => {},
    locator: () => ({ ...visible(), textContent: async () => `Send to ${phone}` }),
    click: async () => { stage = 'draft'; url = `https://voice.google.com/u/3/messages?${new URLSearchParams({itemId:options.draftItem ?? 'draft'})}`; },
  };
  browser.page = {
    getByRole: (role,options) => {
      assert.equal(role,'region');assert.deepEqual(options,{name:'Search results',exact:true});
      return search;
    },
    url: () => {
      if (stage === 'search' && options.changedSearch && ++searchUrlReads > 1) return `https://voice.google.com/u/3/search?from=%5B%5D&q=${encodeURIComponent(JSON.stringify([other]))}`;
      return url;
    },
    keyboard: { press: async () => {} },
    locator: selector => {
      if (selector === selectors.compose) return composer;
      if (selector === selectors.signedIn) return visible(1, !options.signedOut);
      if (selector === selectors.progress) return visible(options.loading ?? 0);
      if (selector === selectors.threads) return visible(stage === 'search' ? options.results ?? 0 : 0);
      if (selector === selectors.bubbles) return visible(stage === 'search' ? options.searchBubbles ?? 0 : options.draftBubbles ?? 0);
      if (selector === selectors.newMessage) return { click: async () => { actions.drafts += 1; } };
      if (selector === selectors.recipient) return { fill: async value => assert.equal(value, phone) };
      if (selector === selectors.recipientChoice) return choice;
      if (selector === selectors.recipientRegion) return recipients;
      if (selector === selectors.send) return { click: async () => { actions.sends += 1; } };
      throw new Error(`unhandled synthetic selector ${selector}`);
    },
  };
  browser.navigate = async path => {
    browser.prepared = null;
    url = `https://voice.google.com/u/3/${path}`;
    if (path.startsWith('search?')) {
      stage = 'search'; actions.searches += 1;
      if (options.searchUrl) url = options.searchUrl;
    } else {
      stage = 'thread';
      if (options.directUrl && path.startsWith('messages?itemId=')) url = options.directUrl;
    }
  };
  browser.rows = async () => { actions.rows += 1; return []; };
  return { browser, actions };
};

test('explicit first-pending scope proves exact no-results search and empty numeric recipient draft without composing or sending', async () => {
  const { browser, actions } = fixture();
  assert.deepEqual(await browser.scan([phone], { emptyPhones: [phone] }), []);
  assert.deepEqual(actions, { searches: 1, drafts: 1, bodyFills: 0, sends: 0, rows: 0 });
  assert.equal(browser.prepared, null);
});

for (const [label, options] of [
  ['wrong query', { searchUrl: `https://voice.google.com/u/3/search?from=%5B%5D&q=${encodeURIComponent(JSON.stringify([other]))}` }],
  ['nonempty sender filter', { searchUrl: `https://voice.google.com/u/3/search?from=%5B%22other%22%5D&q=${encodeURIComponent(JSON.stringify([phone]))}` }],
  ['extra query parameter', { searchUrl: `https://voice.google.com/u/3/search?from=%5B%5D&q=${encodeURIComponent(JSON.stringify([phone]))}&extra=1` }],
  ['stale messages route', { searchUrl: `https://voice.google.com/u/3/messages?from=%5B%5D&q=${encodeURIComponent(JSON.stringify([phone]))}` }],
  ['search route changing during observation', { changedSearch: true }],
  ['authentication redirect', { searchUrl: 'https://accounts.google.com/signin' }],
  ['wrong no-results target', { absentText: `No search results found for ${other}` }],
  ['hidden no-results paragraph', { absentVisible: false }],
  ['duplicate no-results paragraph', { absentCount: 2 }],
  ['no-results timeout', { absentTimeout: true }],
  ['hidden search region', { regionVisible: false }],
  ['duplicate search regions', { regions: 2 }],
  ['loading results', { loading: 1 }],
  ['existing search result', { results: 1 }],
  ['stale search bubble', { searchBubbles: 1 }],
  ['missing signed-in account', { signedOut: true }],
]) test(`empty-history fallback holds before draft preparation for ${label}`, async () => {
  const { browser, actions } = fixture(options);
  await assert.rejects(browser.scan([phone], { emptyPhones: [phone] }),
    { code: options.absentTimeout ? 'thread_not_observable_search_load' : 'thread_not_observable_query_proof' });
  assert.equal(actions.drafts, 0);
  assert.equal(actions.bodyFills, 0);
  assert.equal(actions.sends, 0);
  assert.equal(actions.rows, 0);
});

for (const [label, options] of [
  ['multiple recipient chips', { chips: 2 }],
  ['wrong numeric chip', { chip: other }],
  ['nonnumeric contact chip', { chip: 'Synthetic contact' }],
  ['hidden recipient chip', { chipVisible: false }],
  ['established thread', { draftItem: `t.${phone}` }],
  ['stale draft body', { body: 'A prior draft, never send this.' }],
  ['draft conversation bubbles', { draftBubbles: 1 }],
  ['hidden composer', { composerVisible: false }],
  ['multiple composers', { composers: 2 }],
]) test(`empty-history fallback holds without filling or sending for ${label}`, async () => {
  const { browser, actions } = fixture(options);
  await assert.rejects(browser.scan([phone], { emptyPhones: [phone] }),
    { code: ['multiple recipient chips','wrong numeric chip','nonnumeric contact chip','hidden recipient chip'].includes(label)
      ? 'recipient_not_verified' : options.composers === 2 ? 'composer_ambiguous' : 'thread_not_observable_draft_recipient_proof' });
  assert.equal(actions.bodyFills, 0);
  assert.equal(actions.sends, 0);
  assert.equal(actions.rows, 0);
});

test('missing first-pending scope and non-timeout browser failures never search or prepare a draft', async () => {
  for (const [options, scope] of [[{}, {}], [{}, { emptyPhones: [other] }], [{ waitError: 'TargetClosedError' }, { emptyPhones: [phone] }]]) {
    const { browser, actions } = fixture(options);
    await assert.rejects(browser.scan([phone], scope), { code: 'thread_not_observable_composer' });
    assert.equal(actions.searches, 0);
    assert.equal(actions.drafts, 0);
  }
});

test('a changed direct target route holds with its own fixed phase code before searching', async () => {
  const {browser,actions}=fixture({directUrl:`https://voice.google.com/u/3/messages?itemId=t.${other}`});
  await assert.rejects(browser.scan([phone],{emptyPhones:[phone]}),{code:'thread_not_observable_direct_route'});
  assert.equal(actions.searches,0);assert.equal(actions.drafts,0);assert.equal(actions.bodyFills,0);assert.equal(actions.sends,0);
});

test('an unapproved recipient cannot use empty-history scope', async () => {
  const { browser, actions } = fixture();
  await assert.rejects(browser.scan([other], { emptyPhones: [other] }), { code: 'recipient_not_allowed' });
  assert.equal(actions.searches, 0);
  assert.equal(actions.drafts, 0);
});

test('observable existing thread keeps its original scan path without a search or draft', async () => {
  const { browser, actions } = fixture({ existing: true });
  assert.deepEqual(await browser.scan([phone], { emptyPhones: [phone] }), []);
  assert.equal(actions.searches, 0);
  assert.equal(actions.drafts, 0);
});
