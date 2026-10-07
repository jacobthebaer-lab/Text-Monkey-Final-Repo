"""Signed additive Mac enrollment, only fictional phones and disposable stores."""
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path

import httpx
import pytest
from app.config import Settings
from app.db import models as m
from app.integrations.mac_ongoing import authorize,enroll,adopt,verify,_sign
from app.integrations.mac_messages import MacWorker,NaturalTestSessionMessagesReader
from app.integrations.test_sessions import parse_sessions
from app.sms.mac_provider import MacMessagesProvider
from tests.test_mac_ongoing import prepared,PHONE,LINE,TOKEN,NOW,BODY
from tests.test_mac_natural_signup import reader

NEW='+15555550999'


def enrolled(tmp_path,now=NOW):
    old,state,raw,current=prepared(tmp_path,now=now-timedelta(minutes=1))
    state,_=adopt(current,state,raw,[],now)
    state.update(after=15,ongoing_target_received=True,progress_enabled=True)
    raw=json.dumps(state).encode()
    Path(current['state_path']).write_bytes(raw)
    updated=enroll(current,raw,phone=NEW,name='Fictional New Participant',actor='Explicit fictional operator',
        operator_confirmed=True,active=[],now=now)
    return current,state,raw,updated


def settings(config):
    return Settings(sms_provider='mac_messages',mac_bridge_enabled=True,mac_bridge_token=TOKEN,
        admin_password='synthetic-password-123',mac_demo_phones=','.join(config['phones']),
        mac_message_services=','.join(config['services']),mac_test_sessions=json.dumps(config['test_sessions']),
        mac_ongoing_authorization=json.dumps(config['ongoing_authorization']))


def test_enrollment_preserves_original_lineage_ledger_and_fresh_session(tmp_path):
    current,state,raw,updated=enrolled(tmp_path)
    journal=verify(updated['ongoing_authorization'],TOKEN)
    selected=MacMessagesProvider(settings(updated)).test_sessions[NEW]
    assert journal['version']==2 and journal['previous_authorization']==current['ongoing_authorization']
    assert journal['target']==current['ongoing_authorization']['target']
    assert updated['test_sessions'][PHONE]==current['test_sessions'][PHONE]
    assert selected.original_expires_at is None and selected.enrolled_at==selected.starts_at==selected.ongoing_since==NOW
    assert selected.expires_at is None and selected.active(NOW+timedelta(days=100))
    assert not selected.active(NOW-timedelta(microseconds=1))
    assert selected.id!=MacMessagesProvider(settings(updated)).test_sessions[PHONE].id
    assert selected.review_until(NOW)==NOW+timedelta(hours=2)
    assert 'guid' not in journal['enrollment'] and 'original_expires_at' not in selected.spec()
    with pytest.raises(ValueError):parse_sessions(updated['test_sessions'],updated['phones'])
    adopted,_=adopt(updated,state,raw,[],NOW)
    assert adopted['after']==state['after'] and adopted['dispatches']==state['dispatches']
    assert adopted['ongoing_target_received'] is True and adopted['progress_enabled'] is True
    assert state['phones']==[PHONE] and adopted['phones']==sorted([PHONE,NEW])
    restarted,_=adopt(updated,adopted,b'new-bytes-after-native-progress',[],NOW+timedelta(days=10))
    assert restarted==adopted


def test_roster_scale_enrollment_preserves_every_existing_session_and_ledger(tmp_path):
    _,state,raw,current=prepared(tmp_path)
    state,_=adopt(current,state,raw,[],NOW)
    state.update(after=15,ongoing_target_received=True)
    original=current['test_sessions'][PHONE]
    ledger=deepcopy(state['dispatches'])
    for index in range(1,100):
        phone=f'+1555556{index:04d}'
        stamp=NOW+timedelta(seconds=index)
        raw=json.dumps(state).encode()
        previous=deepcopy(current['test_sessions'])
        current=enroll(current,raw,phone=phone,name=f'Synthetic roster participant {index}',
            actor='Explicit synthetic roster approval',operator_confirmed=True,active=[],now=stamp)
        state,_=adopt(current,state,raw,[],stamp)
        assert {p:current['test_sessions'][p] for p in previous}==previous
        assert state['after']==15 and state['dispatches']==ledger
    provider=MacMessagesProvider(settings(current))
    assert len(provider.phones)==100
    assert all(provider.allows(phone) for phone in current['phones'])
    assert current['test_sessions'][PHONE]==original
    before=json.dumps(current['ongoing_authorization'],sort_keys=True)
    assert verify(current['ongoing_authorization'],TOKEN)==current['ongoing_authorization']
    assert json.dumps(current['ongoing_authorization'],sort_keys=True)==before
    # Re-sign the outer journal so an invalid inner signature must be detected
    # by traversal, rather than only by the top-level signature check.
    changed=deepcopy(current['ongoing_authorization'])
    changed['previous_authorization']['signature']='0'*64
    changed['signature']=_sign({k:v for k,v in changed.items() if k!='signature'},TOKEN)
    with pytest.raises(ValueError,match='changed or is missing'):
        verify(changed,TOKEN)


@pytest.mark.parametrize('change',['approval','name','existing_phone','pending','uncertain','active','attempting',
    'source_unreceived','cursor','session','route','original_journal'])
def test_operator_enrollment_rejects_unapproved_or_unsettled_scope(tmp_path,change):
    current,state,raw,updated=enrolled(tmp_path)
    kwargs=dict(phone=NEW,name='Fictional New Participant',actor='Explicit operator',operator_confirmed=True,
        active=[],now=NOW)
    if change=='approval':kwargs['operator_confirmed']=False
    elif change=='name':kwargs['name']=' '
    elif change=='existing_phone':kwargs['phone']=PHONE
    elif change=='pending':state['claim_response_pending']=True
    elif change=='uncertain':state['claim_response_uncertain']=True
    elif change=='active':kwargs['active']=[{'id':8}]
    elif change=='attempting':state['dispatches']['7']['outcome']='attempting'
    elif change=='source_unreceived':state['ongoing_target_received']=False
    elif change=='cursor':state['after']=0
    elif change=='session':state['test_sessions'][PHONE]['id']='c'*32
    elif change=='route':current['receiving_number']='+15555550800'
    else:state['ongoing_journal']='changed'
    raw=json.dumps(state).encode()
    with pytest.raises(ValueError):enroll(current,raw,**kwargs)


@pytest.mark.parametrize('change',['hash','cursor','claim','future','newphone','oldphone','service','mode',
    'session','token','decoder','received_flag','prior_journal','ledger_bytes'])
def test_adoption_rejects_stale_checkpoint_or_config_diff(tmp_path,change):
    current,state,raw,updated=enrolled(tmp_path)
    now=NOW;active=[]
    if change=='hash':raw+=b' '
    elif change=='cursor':state['after']+=1
    elif change=='claim':active=[{'id':9}]
    elif change=='future':now-=timedelta(seconds=1)
    elif change=='newphone':updated['phones'].append('+15555550800')
    elif change=='oldphone':updated['phones'].remove(PHONE)
    elif change=='service':updated['services']=['iMessage']
    elif change=='mode':updated['input_mode']='marked'
    elif change=='session':updated['test_sessions'][PHONE]['id']='d'*32
    elif change=='token':updated['token']='changed'+'z'*40
    elif change=='decoder':updated['decoder']='/unauthorized/decoder'
    elif change=='received_flag':state['ongoing_target_received']=False
    elif change=='prior_journal':state['ongoing_journal']='changed'
    else:state['dispatches']['7']['token']='Changed without changing source bytes'
    with pytest.raises(ValueError):adopt(updated,state,raw,active,now)


@pytest.mark.parametrize('change',['unsigned_name','prior_signature','existing_session','remove_old',
    'new_session_id','new_start','new_phone','old_target','line','services','mode'])
def test_signed_revision_cannot_rewrite_prior_participants_or_source(tmp_path,change):
    current,state,raw,updated=enrolled(tmp_path)
    journal=updated['ongoing_authorization']
    if change=='unsigned_name':journal['enrollment']['name']='Changed';resign=False
    elif change=='prior_signature':journal['previous_authorization']['signature']='changed';resign=True
    elif change=='existing_session':journal['sessions'][PHONE]['id']='f'*32;resign=True
    elif change=='remove_old':del journal['sessions'][PHONE];resign=True
    elif change=='new_session_id':journal['sessions'][NEW]['id']='a'*32;resign=True
    elif change=='new_start':journal['sessions'][NEW]['starts_at']=(NOW+timedelta(seconds=1)).isoformat();resign=True
    elif change=='new_phone':journal['route']['phones'].append('+15555550800');resign=True
    elif change=='old_target':journal['target']['guid']='invented';resign=True
    elif change=='line':journal['route']['receiving_number']='+15555550800';resign=True
    elif change=='services':journal['route']['services']=['iMessage'];resign=True
    else:journal['route']['input_mode']='marked';resign=True
    if resign:
        value={k:v for k,v in journal.items() if k!='signature'}
        journal['signature']=_sign(value,TOKEN)
    with pytest.raises(ValueError):verify(journal,TOKEN)


def test_provider_rejects_mismatched_scopes_without_broadening_permission(tmp_path):
    _,_,_,updated=enrolled(tmp_path)
    configured=settings(updated)
    for altered in [replace(configured,mac_demo_phones=PHONE),replace(configured,mac_message_services='iMessage'),
            replace(configured,mac_ongoing_authorization=''),replace(configured,mac_bridge_token='y'*40)]:
        with pytest.raises(ValueError):MacMessagesProvider(altered)
    provider=MacMessagesProvider(configured)
    assert provider.send(NEW,'Exact synthetic body').startswith('MAC'+provider.test_sessions[NEW].id+':')
    with pytest.raises(ValueError):provider.send('+15555550800','Unscoped body')


def test_worker_adopts_expansion_and_restarts_without_cursor_reset_or_native_send(tmp_path):
    current,state,raw,updated=enrolled(tmp_path,now=NOW-timedelta(seconds=1));calls=[]
    class Reader:
        def watermark(self):pytest.fail('Enrollment must preserve existing cursor')
        def new_messages(self,after):
            return [] if after>=16 else [{'row_id':16,'guid':'fresh-enrollment-guid','phone':NEW,
                'body':'New actual reply','service':'SMS','session_id':updated['test_sessions'][NEW]['id']}]
    def server(request):
        calls.append(json.loads(request.content));return httpx.Response(200,json={'received':True})
    client=httpx.Client(transport=httpx.MockTransport(server))
    worker=MacWorker(updated,client=client,reader=Reader(),sender=lambda *a:pytest.fail('No native send'))
    assert worker.state['dispatches']==state['dispatches']
    worker.once();resumed=MacWorker(updated,client=client,reader=Reader());resumed.once()
    assert len(calls)==1 and calls[0]['phone']==NEW
    assert resumed.state['after']==16 and resumed.state['ongoing_target_received'] is True
    assert resumed.state['dispatches']==state['dispatches']


def test_unsigned_phone_expansion_fails_before_reader_or_checkpoint_mutation(tmp_path):
    current,state,raw,_=enrolled(tmp_path,now=NOW-timedelta(seconds=1))
    current['phones'].append(NEW)
    current['test_sessions'][NEW]={'id':'b'*32,'starts_at':(NOW-timedelta(seconds=1)).isoformat(),
        'expires_at':None,'until_stopped':True,'ongoing_since':(NOW-timedelta(seconds=1)).isoformat(),
        'enrolled_at':(NOW-timedelta(seconds=1)).isoformat()}
    with pytest.raises(ValueError):MacWorker(current,reader=object())
    assert Path(current['state_path']).read_bytes()==raw


def test_native_reader_never_reads_new_participant_old_history_and_keeps_original_target(reader,tmp_path,monkeypatch):
    from tests.test_mac_natural_signup import NOW as native_now
    r,add,clock=reader
    old,state,raw,_=prepared(tmp_path,now=native_now-timedelta(minutes=1))
    # Synthetic route 2 already belongs to the new participant in this fixture.
    add(body='Private old history',chat=2,handle=2,when=native_now-timedelta(seconds=1))
    add(body='STOP',chat=2,handle=2,when=native_now-timedelta(seconds=1))
    target_time=native_now-timedelta(minutes=10)
    target_id=add(body=BODY,when=target_time)
    state['after']=0;raw=json.dumps(state).encode()
    current=authorize(old,raw,phone=PHONE,guid='guid-2',row_id=target_id,
        body_hash=hashlib.sha256(BODY.encode()).hexdigest(),received_at=target_time.isoformat(),
        actor='Explicit original authority',operator_confirmed=True,now=native_now-timedelta(minutes=1))
    state,_=adopt(current,state,raw,[],native_now)
    state.update(after=target_id,ongoing_target_received=True);raw=json.dumps(state).encode()
    updated=enroll(current,raw,phone=NEW,name='Fictional New Participant',actor='Explicit operator',
        operator_confirmed=True,active=[],now=native_now)
    # These old texts have new row IDs above the preserved cursor. Their dates
    # still exclude them, and the new phone gets no original-target exception.
    add(body='Late-imported old history',chat=2,handle=2,when=native_now-timedelta(seconds=1))
    add(body='STOP',chat=2,handle=2,when=native_now-timedelta(seconds=1))
    add(body=None,blob=b'private-old-body',chat=2,handle=2,when=native_now-timedelta(seconds=1))
    monkeypatch.setattr('app.integrations.mac_messages.decode_body',lambda *a:pytest.fail('Old private body must not be decoded'))
    new_id=add(body='Fresh permitted input',chat=2,handle=2,when=native_now)
    add(body='Future input',chat=2,handle=2,when=native_now+timedelta(seconds=1))
    add(body='Wrong line',chat=3,line='+15555550300',when=native_now)
    r.test_sessions=parse_sessions(updated['test_sessions'],updated['phones'],allow_ongoing=True)
    r.ongoing_journal=updated['ongoing_authorization']
    assert [row['body'] for row in r.new_messages(0)]==[BODY,'Fresh permitted input']
    clock[0]+=timedelta(days=3)
    add(body='STOP',chat=2,handle=2,when=clock[0])
    assert [row['body'] for row in r.new_messages(new_id)]==['Future input','STOP']


def test_fresh_session_actual_signup_authority_keeps_policy_consent_and_stop_guards(tmp_path,session):
    from app.core.signup_review_authority import received
    _,_,_,updated=enrolled(tmp_path)
    selected=MacMessagesProvider(settings(updated)).test_sessions[NEW]
    session.info['mac_test_session']=selected
    person=m.Volunteer(name='Fictional New Participant',phone=NEW,sms_opt_in=True,status='active',preferences={},created_at=NOW)
    session.add(person);session.flush()
    session.add(m.Policy(key='conversational_signup:'+NEW,value={'value':True,'session_id':selected.id}))
    old=m.Message(volunteer_id=person.id,phone=NEW,body='Old reply',kind='mac_test_in',direction='in',status='received',
        purpose='test:'+selected.id,created_at=NOW-timedelta(seconds=1))
    incoming=m.Message(volunteer_id=person.id,phone=NEW,body='Actual enrolled reply',kind='mac_test_in',direction='in',
        status='received',purpose='test:'+selected.id,created_at=NOW)
    session.add_all([old,incoming]);session.flush()
    assert received(session,person,old.id,NOW) is None
    assert received(session,person,incoming.id,NOW) is incoming
    person.sms_opt_in=False
    assert received(session,person,incoming.id,NOW) is None
    person.sms_opt_in=True
    session.add(m.Policy(key='sms_opt_out:'+NEW,value={'value':True}));session.flush()
    assert received(session,person,incoming.id,NOW) is None


def test_enrolled_outbound_preflight_requires_fresh_epoch_and_retains_stop_and_style(tmp_path):
    from fastapi.testclient import TestClient
    from app.clock import FakeClock
    from app.main import create_app
    from app.integrations.mac_models import MacDeliveryClaim
    from sqlalchemy import select
    _,_,_,updated=enrolled(tmp_path)
    configured=replace(settings(updated),database_url=f'sqlite:///{tmp_path}/backend.sqlite')
    app=create_app(configured);app.state.clock=FakeClock(NOW);app.state.mac_delivery_clock=FakeClock(NOW)
    with app.state.session_factory() as s:
        row=m.Message(direction='out',phone=NEW,body='Synthetic historical queue',purpose='signup_reply',kind='ai',
            status='queued',provider_sid='MAC'+updated['test_sessions'][NEW]['id']+':old',created_at=NOW-timedelta(seconds=1))
        s.add(row);s.commit();ident=row.id
    with TestClient(app) as client:
        result=client.post('/mac/outbound/pull',headers={'Authorization':'Bearer '+TOKEN},json={})
        assert result.status_code==200 and result.json()['messages']==[]
        result=client.post('/mac/inbound',headers={'Authorization':'Bearer '+TOKEN},json={
            'guid':'new-actual-stop','phone':NEW,'body':'STOP','service':'SMS','session_id':updated['test_sessions'][NEW]['id']})
        assert result.status_code==200
    with app.state.session_factory() as s:
        assert s.get(m.Message,ident).status=='blocked_test_session'
        assert s.get(m.Policy,'sms_opt_out:'+NEW).value['value'] is True
        assert s.scalar(select(MacDeliveryClaim)) is None
