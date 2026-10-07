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
      setAttribute() {}, addEventListener(name, handler) { handlers.set(key + ':' + name, handler); }, contains() { return false; },
      querySelector() { return element(key + '/heading'); },
      focus() { document.activeElement = this; }, matches() { return false; },
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
    setView, selectCategory, startFlow, chooseDecision, visibleFeatures,
    snapshot: () => JSON.parse(JSON.stringify({mode, flight, desired, selected, query, view,
      path, flow: activeFlow.id, keys: [...keys], motion})),
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

test('every actual inventory card opens with current source links and retains its exact ID', () => {
  const { hook, model, element } = explorer();
  const features = model.categories.flatMap(c => c.features);
  assert.equal(features.length, 183);
  for (const feature of features) {
    hook.selectFeature(feature.id, false);
    assert.equal(hook.snapshot().selected, feature.id);
    assert.equal(element('detail').hidden, false);
    assert.ok(element('detail').innerHTML.includes('data-travel="' + feature.id + '"'));
    assert.ok(element('announcement').textContent.includes(feature.title));
    for (const source of feature.sources.filter(s => typeof s === 'string')) {
      assert.ok(element('detail').innerHTML.includes(hook.sourceHTML(source)));
    }
  }
});

test('search, status and category filters intersect and reset through their real controls', () => {
  const { hook, element, handlers } = explorer();
  hook.selectCategory('future');
  hook.setView('matrix');
  handlers.get('search:input')({ target: { value: 'Google Voice' } });
  handlers.get('status-filter:change')({ target: { value: 'historical' } });
  const visible = Array.from(hook.visibleFeatures(), f => f.id);
  assert.deepEqual(visible, ['google-voice-prototype', 'historical-transports']);
  assert.equal(element('matrix-count').textContent, '2 features');
  element('clear-filters').onclick();
  assert.equal(hook.visibleFeatures().length, 183);
  handlers.get('search:input')({ target: { value: 'zzzz no real feature' } });
  assert.equal(hook.visibleFeatures().length, 0);
  hook.selectFeature('fictional-church', false);
  assert.equal(hook.snapshot().query, '');
  assert.equal(hook.visibleFeatures().length, 183);
});

test('all ten decision paths and every offered branch remain explanations without fetches', () => {
  const { context, hook, model, element } = explorer();
  context.fetch = () => assert.fail('Decision navigation must never operate the product');
  assert.equal(model.flows.length, 10);
  hook.setView('decision');
  for (const flow of model.flows) {
    hook.startFlow(flow.id);
    assert.equal(hook.snapshot().selected, flow.nodes[0].id);
    for (const node of flow.nodes) {
      for (const choice of node.choices || []) {
        hook.chooseDecision(node.id, choice.next);
        assert.equal(hook.snapshot().selected, choice.next);
        assert.ok(element('detail').innerHTML.includes('Choices here never schedule an event or send a text.'));
      }
    }
    element('restart-path').onclick();
    assert.deepEqual(Array.from(hook.snapshot().path), [flow.nodes[0].id]);
  }
});

test('keyboard shortcuts respect editable focus, close details, and stop flight keys on blur', () => {
  const { hook, handlers, element, context } = explorer();
  element('fly-mode').onclick();
  let prevented = 0;
  const press = (key, editable = false) => handlers.get('keydown')({ key,
    target: { matches: () => editable }, preventDefault() { prevented++; } });
  const before = hook.snapshot();
  press('h', true);
  assert.deepEqual(hook.snapshot(), before);
  for (const key of ['w', 'a', 's', 'd', 'q', 'e', 'Shift', 'ArrowUp']) press(key);
  assert.equal(hook.snapshot().keys.length, 8);
  handlers.get('keyup')({key:'w'});
  assert.ok(!hook.snapshot().keys.includes('w'));
  handlers.get('blur')();
  assert.equal(hook.snapshot().keys.length, 0);
  hook.selectFeature('fictional-church', false);
  press('Escape');
  assert.equal(element('detail').hidden, true);
  press('/');
  assert.equal(context.document.activeElement, element('search'));
  press('h');
  assert.equal(hook.snapshot().view, 'galaxy');
  assert.ok(prevented >= 9);
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
