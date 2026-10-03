import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createCopyDraft, renderCopy, COPY_LABELS} from '../public/onboarding-copy.js';

const defaults = JSON.parse(fs.readFileSync(new URL('../public/onboarding-copy-defaults.json', import.meta.url)));
const snapshot = messages => ({messages, defaults, revision:0});

test('all four editable fields use the canonical defaults and escaped placeholder values', () => {
  assert.deepEqual(Object.keys(COPY_LABELS), Object.keys(defaults));
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
