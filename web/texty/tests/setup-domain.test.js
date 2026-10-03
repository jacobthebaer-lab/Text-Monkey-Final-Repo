import test from 'node:test';
import assert from 'node:assert/strict';
import {parseCSV,guessMapping,previewSample,sampleCSV,normalizePhone,errorCSV} from '../public/setup-domain.js';

test('synthetic import preview maps, normalizes, rejects invalid phones and deduplicates',()=>{
  const rows=parseCSV(sampleCSV),mapping=guessMapping(rows[0]);
  assert.deepEqual(mapping,{name:0,phone:1,email:2,ministry:3});
  const result=previewSample(rows,mapping,'US','Synthetic fixture');
  assert.deepEqual(result.counts,{ready:2,duplicate:1,invalid:1});
  assert.equal(result.rows[0].phone,'+12025550111');
  assert.ok(result.rows.every(r=>r.consent==='not_recorded'));
  assert.equal(previewSample(rows,mapping,'US','Fixture',['+12025550111']).counts.ready,1);
});
test('CSV preserves quotes, unicode and embedded newlines',()=>{
  assert.deepEqual(parseCSV('\ufeffName,Phone\r\n"Alex, \"\"Sample\"\"",2025550111\r\n"Casey\nExample",+442079460123'),[['Name','Phone'],['Alex, "Sample"','2025550111'],['Casey\nExample','+442079460123']]);
  assert.throws(()=>parseCSV('Name,Phone\n"Alex,2025550111'));
});
test('phone and mapping validation never infer unsupported countries or consent',()=>{
  assert.equal(normalizePhone('(202) 555-0111'),' +12025550111'.trim());
  assert.equal(normalizePhone('+44 20 7946 0123','international'),'+442079460123');
  assert.throws(()=>normalizePhone('02079460123','international'));
  assert.throws(()=>normalizePhone('2025550111 ext 2'));
  assert.throws(()=>previewSample(parseCSV(sampleCSV),{name:0,phone:0},'US','Source'));
  assert.throws(()=>previewSample(parseCSV(sampleCSV),{name:0},'US','Source'));
});
test('row report excludes contact data and escapes spreadsheet formulas',()=>{
  const csv=errorCSV([{row:2,status:'invalid',reason:'=HYPERLINK("example")'},{row:3,status:'ready',reason:'Sample'}]);
  assert.match(csv,/'=HYPERLINK/);assert.doesNotMatch(csv,/Sample/);
});
