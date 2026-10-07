import test from 'node:test';
import assert from 'node:assert/strict';
import {churchLabel,lastName,availabilityText,historyText,preserveProfileMarkers} from '../public/church-presentation.js';

test('natural labels keep original identities and clocks intact',()=>{
  assert.equal(churchLabel('Synthetic Youth Night - 2026-10-07'),'Youth Night');
  const person={fictional:true,last_name:'Lawson [Fictional]',availability:'[Fictional history] Sundays at 9 AM'};
  assert.equal(lastName(person),'Lawson');
  assert.equal(availabilityText(person),'Sundays at 9 AM');
  assert.deepEqual(preserveProfileMarkers(person,{last_name:'Lawson',availability:'Sundays at 9 AM'}),
    {last_name:'Lawson [Fictional]',availability:person.availability});
  assert.equal(person.last_name,'Lawson [Fictional]');
});

test('exact real text and editable real names are never rewritten',()=>{
  const message={fictional:false,body:'[Fictional history] Keep this exact reviewed body.'};
  assert.equal(historyText(message),message.body);
  assert.equal(historyText({...message,fictional:true}),'Keep this exact reviewed body.');
  assert.equal(lastName({fictional:false,last_name:'Lawson [Fictional]'}),'Lawson [Fictional]');
  const edit={last_name:'Lawson',availability:'Any Sunday'};
  assert.equal(preserveProfileMarkers({fictional:false},edit),edit);
});
