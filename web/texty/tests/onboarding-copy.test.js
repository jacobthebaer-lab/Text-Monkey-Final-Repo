import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createCopyDraft, renderCopy, COPY_LABELS, VISIBLE_FIELDS, upgradeSavedDefaults} from '../public/onboarding-copy.js';

const defaults = JSON.parse(fs.readFileSync(new URL('../public/onboarding-copy-defaults.json', import.meta.url)));
const snapshot = messages => ({messages, defaults, revision:0});

test('known cached defaults upgrade while unrelated user edits are preserved', () => {
  const upgraded=upgradeSavedDefaults({interests:'Thanks, {first_name}! What would you like to help with? {roles}. Reply with names or numbers, or Anything. Some roles need coordinator clearance.',availability:'My custom question'},defaults);
  assert.equal(upgraded.interests,defaults.interests);
  assert.equal(upgraded.availability,'My custom question');
  assert.equal(upgraded.welcome,defaults.welcome);
  assert.equal(upgradeSavedDefaults({clarification:'When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. You can also say FLEXIBLE.'}, defaults).clarification, '');
});

test('all four editable fields use the canonical defaults and escaped placeholder values', () => {
  assert.deepEqual(Object.keys(COPY_LABELS), Object.keys(defaults));
  assert.deepEqual(VISIBLE_FIELDS, ['welcome','interests','availability','completion']);
  assert.ok(!VISIBLE_FIELDS.includes('clarification'));
  assert.equal(renderCopy('{roles}'), '1: Greeter, 2: Usher, 3: Production, 4: Coffee, 5: Child Care');
  assert.equal(renderCopy('Hi {first_name}: {roles}', {first_name:'Alex',roles:'1: Greeter'}), 'Hi Alex: 1: Greeter');
});

test('edits, save, reset and reload retain separate saved and draft state', async () => {
  const model = createCopyDraft(snapshot(defaults));
  model.edit('availability', 'Which days work for you?');
  assert.equal(model.dirty(), true);
  let submitted;
  await model.save(async data => {
    submitted = data;
    return {...snapshot(data.messages), revision:1};
  });
  assert.equal(submitted.messages.availability, 'Which days work for you?');
  assert.equal(submitted.revision, 0);
  assert.equal(model.revision(), 1);
  assert.equal(model.dirty(), false);
  model.reset();
  assert.equal(model.messages().availability, defaults.availability);
  assert.equal(model.dirty(), true);
  model.replace({...snapshot(submitted.messages), revision:1});
  assert.equal(model.messages().availability, 'Which days work for you?');
  assert.equal(model.dirty(), false);
});

test('failed save keeps copy and revision for reload or correction', async () => {
  const model = createCopyDraft(snapshot(defaults));
  model.edit('clarification', 'What frequency works for you?');
  await assert.rejects(model.save(async () => {throw Error('Conflict');}), /Conflict/);
  assert.equal(model.messages().clarification, 'What frequency works for you?');
  assert.equal(model.revision(), 0);
  assert.equal(model.dirty(), true);
});

test('state cannot be mutated through a returned message object', () => {
  const model = createCopyDraft(snapshot(defaults));
  model.messages().completion = 'wrong';
  assert.equal(model.messages().completion, defaults.completion);
  assert.throws(() => model.edit('owner_id','different owner'), /Unknown/);
});
