import test from 'node:test';
import assert from 'node:assert/strict';
import { VoiceBrowser, selectors } from '../browser.mjs';

const fixture = ({ visible = true, destination = 'https://voice.google.com/u/3/settings' } = {}) => {
  const browser = new VoiceBrowser({ directory: '/unused', allowedPhones: [] });
  browser.page = {
    isClosed: () => false,
    goto: async () => {},
    url: () => destination,
    locator: selector => ({ waitFor: async () => {
      assert.equal(selector, '[role="button"][aria-label^="Google Account:"]');
      if (!visible) throw new Error('not visible');
    } }),
  };
  return browser;
};

test('settings navigation accepts the visible account button without main side navigation', async () => {
  assert.equal(selectors.signedIn, '[role="button"][aria-label^="Google Account:"]');
  const browser = fixture();
  let identityReads = 0;
  browser.assertProfileAvailable = async () => {};
  browser.readIdentity = async () => { identityReads += 1; return { email: 'dedicated@example.invalid', phone: '+12025550102' }; };
  assert.deepEqual(await browser.identity(), { email: 'dedicated@example.invalid', phone: '+12025550102' });
  assert.equal(identityReads, 1);
});

for (const [label, options] of [
  ['absent account', { visible: false }],
  ['Google sign-in redirect', { destination: 'https://accounts.google.com/signin' }],
  ['onboarding', { destination: 'https://voice.google.com/u/3/onboarding' }],
]) test(`navigation holds for ${label}`, async () => {
  await assert.rejects(fixture(options).navigate('settings'), error => error.reason === 'reconnect_required' || error.message === 'reconnect_required');
});
