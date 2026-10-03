import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parseCSV, guessMapping, previewSample} from '../public/setup-domain.js';

test('shipped demo CSVs produce the documented public-preview staging counts', () => {
  const rows = filename => parseCSV(readFileSync(new URL(`../../../data/demo-import/${filename}`, import.meta.url), 'utf8'));
  const clean = rows('contacts-clean.csv');
  const mapping = guessMapping(clean[0]);
  assert.deepEqual(mapping, {name:0, phone:1, email:2, ministry:3});
  const report = previewSample(clean, mapping, 'US', 'Fictional Text Monkey demo package');
  assert.deepEqual(report.counts, {ready:3, duplicate:0, invalid:0});
  assert.deepEqual(report.rows.map(r => r.phone), ['+12025550111', '+12025550112', '+12025550113']);
  assert.ok(report.rows.every(r => r.consent === 'not_recorded' && r.ministry.endsWith('(unverified note)')));
  assert.deepEqual(previewSample(clean, mapping, 'US', 'Fixture', report.rows.map(r => r.phone)).counts,
    {ready:0, duplicate:3, invalid:0});
  const invalid = previewSample(rows('contacts-needs-fixes.csv'), mapping, 'US', 'Fixture');
  assert.deepEqual(invalid.counts, {ready:0, duplicate:0, invalid:3});
  assert.deepEqual(invalid.rows.map(r => r.row), [2,3,4]);
});
