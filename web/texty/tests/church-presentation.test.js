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

test('fixture metadata is hidden without rewriting actual texts or names',()=>{
  assert.equal(churchLabel('Demo: Sunday Service'),'Sunday Service');
  assert.equal(churchLabel('Test Greeter'),'Greeter');
  assert.equal(churchLabel('Testament ministry'),'Testament ministry');
  assert.equal(churchLabel('[Synthetic] Women’s Group 7 PM'),'Women’s Group 7 PM');
  assert.equal(churchLabel('Greeter [Synthetic 006]'),'Greeter');
  assert.equal(churchLabel('Sunday Service [Synthetic]'),'Sunday Service');
  assert.equal(historyText({fictional:true,body:'[Fictional history] Serve as Synthetic Greeter at Demo: Sunday Service.'}),'Serve as Greeter at Sunday Service.');
  const real={fictional:false,body:'Exact approved Synthetic Greeter test text.'};
  assert.equal(historyText(real),real.body);
});


test('development labels are removed from presentation only',()=>{
  for(const label of ['Fictional','Test dummy','Dummy','Fake','Mock']) {
    assert.equal(churchLabel(label+' Sunday Service'),'Sunday Service');
    assert.equal(churchLabel('Sunday Service ['+label+']'),'Sunday Service');
    assert.equal(churchLabel('Sunday Service ('+label+')'),'Sunday Service');
  }
  assert.equal(churchLabel('Faith and Fellowship'),'Faith and Fellowship');
  assert.equal(historyText({fictional:true,body:'[Fictional history] Serve at Dummy Sunday Service.'}),'Serve at Sunday Service.');
  const approved={fictional:false,body:'Please test the sound system.'};
  assert.equal(historyText(approved),approved.body);
});
