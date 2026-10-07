'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Execute the real event handlers with synthetic DOM surfaces. No app/transport
// endpoints, browser account, WebGL dependency or external library is involved.
function explorer() {
  const model = JSON.parse(fs.readFileSync(__dirname + '/model.json', 'utf8'));
  model.sourceRevision = 'a'.repeat(40);
  const nodes = new Map(), handlers = new Map();
  const element = key => {
    if (!nodes.has(key)) nodes.set(key, {
      textContent: '', innerHTML: '', dataset: {}, style: {}, hidden: false,
      scrollTop: 0, isConnected: true, open: false,
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, addEventListener() {}, contains() { return false; },
      querySelector() { return element(key + '/heading'); },
      focus() {}, matches() { return false; },
      getContext() { return { setTransform() {} }; },
    });
    return nodes.get(key);
  };
  element('model-data').textContent = JSON.stringify(model);
  const document = {
    getElementById: element, querySelector: element, querySelectorAll() { return []; },
    body: element('body'), activeElement: element('body'), addEventListener() {},
  };
  const context = vm.createContext({
    document, location: { protocol: 'file:' }, innerWidth: 1280, innerHeight: 800,
    devicePixelRatio: 1, matchMedia: () => ({ matches: true }),
    addEventListener: (name, handler) => handlers.set(name, handler),
    requestAnimationFrame() {}, setInterval() {}, setTimeout, clearTimeout,
    AbortController, URL, Blob, CSS: { escape: value => value },
  });
  const source = fs.readFileSync(__dirname + '/explorer.js', 'utf8');
  const hook = `globalThis.auditHook = { sourceHTML, selectFeature, pollLiveStatus,
    snapshot: () => JSON.parse(JSON.stringify({mode, flight, desired, selected, query, view})),
    enableFeed: () => { liveFeed.enabled = true; } };`;
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, hook + '\n})();'), context);
  return { context, model, element, handlers, hook: context.auditHook };
}

test('evidence links use the reconciled source snapshot while preserving the original change baseline', () => {
  const { hook, model } = explorer();
  assert.notEqual(model.sourceRevision, model.revision);
  const html = hook.sourceHTML('app/core/split_coverage.py:15');
  assert.match(html, new RegExp('/blob/' + model.sourceRevision + '/app/core/split_coverage.py#L15'));
  assert.ok(!html.includes('/blob/' + model.revision + '/'));
});

test('keyboard plus and minus fly forward and back just like the visible buttons', () => {
  const { element, handlers, hook } = explorer();
  element('fly-mode').onclick();
  const before = hook.snapshot();
  let prevented = 0;
  const press = key => handlers.get('keydown')({ key, target: element('space'), preventDefault() { prevented++; } });
  press('+'); const after = hook.snapshot();
  assert.equal(after.mode, 'fly');
  const distance = Math.hypot(...['x', 'y', 'z'].map(axis => after.flight.position[axis] - before.flight.position[axis]));
  assert.ok(Math.abs(distance - 100) < 1e-8);
  press('-'); const back = hook.snapshot();
  for (const axis of ['x', 'y', 'z']) assert.ok(Math.abs(back.flight.position[axis] - before.flight.position[axis]) < 1e-8);
  assert.equal(back.desired.distance, before.desired.distance);
  assert.equal(prevented, 2);
});

test('live polling preserves camera and selected detail instead of resetting exploration', async () => {
  const { context, hook, model } = explorer();
  hook.selectFeature(model.categories[0].features[0].id);
  const before = hook.snapshot();
  const at = new Date().toISOString();
  const features = Object.fromEntries(model.categories.flatMap(c => c.features.map(f => [f.id, {
    status: f.status, progress: 'changed', summary: 'Synthetic source change, review needed.',
    checkedAt: at, sourceRevision: model.sourceRevision, links: [],
  }])));
  context.fetch = async () => ({ ok: true, text: async () => JSON.stringify({
    schemaVersion: 1, checkedAt: at, generatedAt: at,
    repository: { revision: model.sourceRevision, branch: 'codex/complete-text-monkey' },
    features, sync: { mode: 'repository-events', intervalMinutes: 60 },
  }) });
  hook.enableFeed(); await hook.pollLiveStatus();
  assert.deepEqual(hook.snapshot(), before);
});
