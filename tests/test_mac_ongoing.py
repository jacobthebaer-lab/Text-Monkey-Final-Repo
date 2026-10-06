"""Explicit ongoing Mac authorization, synthetic native data and HTTP only."""
import hashlib
import json
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import Settings
from app.clock import FakeClock
from app.db import models as m
from app.integrations.mac_ongoing import authorize,adopt,verify
from app.integrations.test_sessions import parse_sessions
from app.integrations.mac_messages import MacWorker
from app.sms.mac_provider import MacMessagesProvider
from app.main import create_app
from tests.test_mac_natural_signup import reader

PHONE='+15555550101';LINE='+15555550200';TOKEN='synthetic-token-'+('x'*40)
NOW=datetime.now(timezone.utc);BODY='First and third Sundays are fine.'


def prepared(tmp_path,now=NOW):
    spec={'id':'a'*32,'starts_at':(now-timedelta(hours=3)).isoformat(),
          'expires_at':(now-timedelta(hours=1)).isoformat()}
    config={'backend_url':'http://127.0.0.1:61887','phones':[PHONE],'receiving_number':LINE,
        'services':['SMS','iMessage'],'input_mode':'natural','test_sessions':{PHONE:spec},
        'token':TOKEN,'state_path':str(tmp_path/'checkpoint.json')}
    state={'after':10,'phones':[PHONE],'receiving_number':LINE,'services':['SMS','iMessage'],
        'input_mode':'natural','test_sessions':{PHONE:spec},'dispatches':{'7':{'token':'old-token','outcome':'submitted'}}}
    raw=json.dumps(state).encode();Path(config['state_path']).write_bytes(raw)
    ongoing=authorize(config,raw,phone=PHONE,guid='actual-synthetic-guid',row_id=12,
        body_hash=hashlib.sha256(BODY.encode()).hexdigest(),received_at=(now-timedelta(minutes=10)).isoformat(),
        actor='Explicit operator keep this exact demo live until stopped',operator_confirmed=True,now=now)
    return config,state,raw,ongoing


def provider(config):
    return MacMessagesProvider(Settings(sms_provider='mac_messages',mac_bridge_enabled=True,
        mac_bridge_token=TOKEN,admin_password='synthetic-password-123',mac_demo_phones=PHONE,
        mac_message_services='SMS,iMessage',mac_test_sessions=json.dumps(config['test_sessions']),
        mac_ongoing_authorization=json.dumps(config['ongoing_authorization'])))


def test_ongoing_is_explicit_mac_only_and_reviews_stay_bounded(tmp_path):
    old,state,raw,new=prepared(tmp_path)
    assert parse_sessions(old['test_sessions'],{PHONE})[PHONE].expires_at is not None
    with pytest.raises(ValueError):parse_sessions(new['test_sessions'],{PHONE})
    selected=provider(new).test_sessions[PHONE]
    assert selected.id==old['test_sessions'][PHONE]['id']
    assert selected.expires_at is None and selected.original_expires_at.isoformat()==old['test_sessions'][PHONE]['expires_at']
    assert selected.active(NOW+timedelta(days=30))
    assert not selected.active(NOW-timedelta(seconds=1))
    assert selected.review_until(NOW+timedelta(days=30))==NOW+timedelta(days=30,hours=2)
    assert selected.spec()==new['test_sessions'][PHONE]


@pytest.mark.parametrize('field',['signature','sessions','route','target','actor'])
def test_tampered_authorization_is_rejected(tmp_path,field):
    old,state,raw,new=prepared(tmp_path)
    new['ongoing_authorization'][field]='changed'
    with pytest.raises(ValueError):provider(new)


@pytest.mark.parametrize('change',['phone','service','input','checkpoint','pending','uncertain','active','attempting','session'])
def test_adoption_preserves_fail_closed_boundaries(tmp_path,change):
    old,state,raw,new=prepared(tmp_path);active=[]
    if change=='phone':new['phones']=['+15555550999']
    if change=='service':new['services']=['iMessage']
    if change=='input':new['input_mode']='marked'
    if change=='checkpoint':raw+=b' '
    if change=='pending':state['claim_response_pending']=True
    if change=='uncertain':state['claim_response_uncertain']=True
    if change=='active':active=[{'id':8}]
    if change=='attempting':state['dispatches']['7']['outcome']='attempting'
    if change=='session':state['test_sessions'][PHONE]['id']='b'*32
    with pytest.raises(ValueError):adopt(new,state,raw,active,NOW)


def test_explicit_transition_and_restart_preserve_cursor_and_receipts(tmp_path):
    old,state,raw,new=prepared(tmp_path)
    adopted,journal=adopt(new,state,raw,[],NOW)
    assert adopted['after']==10 and adopted['dispatches']==state['dispatches']
    assert adopted['test_sessions']==new['test_sessions'] and state['test_sessions']==old['test_sessions']
    restarted,_=adopt(new,adopted,b'not-the-original-bytes',[],NOW+timedelta(days=30))
    assert restarted==adopted
    adopted['test_sessions']=old['test_sessions']
    with pytest.raises(ValueError):adopt(new,adopted,b'',[],NOW)


class Reader:
    def watermark(self):pytest.fail('Ongoing transition must never reset cursor')
    def new_messages(self,after):
        return [] if after>=12 else [{'row_id':12,'guid':'actual-synthetic-guid','phone':PHONE,
            'body':BODY,'service':'SMS','session_id':'a'*32}]


def test_worker_reads_authorized_original_guid_once_without_native_send(tmp_path):
    old,state,raw,new=prepared(tmp_path,now=NOW-timedelta(seconds=1));calls=[]
    def server(request):
        assert request.url.path=='/mac/inbound';calls.append(json.loads(request.content))
        return httpx.Response(200,json={'received':True})
    client=httpx.Client(transport=httpx.MockTransport(server))
    worker=MacWorker(new,client=client,reader=Reader(),sender=lambda *a:pytest.fail('No native send'))
    assert worker.state['after']==10 and worker.state['dispatches']==state['dispatches']
    worker.once()
    resumed=MacWorker(new,client=client,reader=Reader());resumed.once()
    assert len(calls)==1 and calls[0]['guid']=='actual-synthetic-guid'
    assert resumed.state['after']==12 and resumed.state['ongoing_target_received'] is True
    assert resumed.state['dispatches']==state['dispatches']


def test_changed_authorized_body_holds_cursor(tmp_path):
    old,state,raw,new=prepared(tmp_path,now=NOW-timedelta(seconds=1))
    class WrongReader(Reader):
        def new_messages(self,after):return [{**super().new_messages(after)[0],'body':'Wrong actual body'}]
    worker=MacWorker(new,reader=WrongReader(),client=httpx.Client(transport=httpx.MockTransport(lambda _:pytest.fail('No ingress'))))
    with pytest.raises(ValueError):worker.once()
    assert worker.state['after']==10


def test_native_reader_excludes_gap_history_and_preserves_route_and_stop(reader,tmp_path):
    from tests.test_mac_natural_signup import NOW as native_now
    r,add,current=reader
    old,state,raw,new=prepared(tmp_path,now=native_now)
    target_time=native_now-timedelta(minutes=10)
    gap=add(body='Unapproved gap text',when=native_now-timedelta(minutes=20))
    target=add(body=BODY,when=target_time)
    # Use synthetic row numbering; checkpoint approved before both native rows.
    state['after']=0;raw=json.dumps(state).encode()
    new=authorize(old,raw,phone=PHONE,guid='guid-1',row_id=target,body_hash=hashlib.sha256(BODY.encode()).hexdigest(),
        received_at=target_time.isoformat(),actor='Exact operator authority',operator_confirmed=True,now=native_now)
    r.test_sessions=parse_sessions(new['test_sessions'],{PHONE},allow_ongoing=True)
    r.ongoing_journal=new['ongoing_authorization']
    add(body='STOP',when=native_now-timedelta(minutes=5))
    add(body='New authorized input',when=native_now)
    add(body='Wrong receiving line',chat=3,line='+15555550300',when=native_now)
    add(body='Group input',chat=4,when=native_now)
    add(body='Future timestamp',when=native_now+timedelta(days=1))
    assert [row['body'] for row in r.new_messages(0)]==[BODY,'STOP','New authorized input']
    current[0]=native_now+timedelta(days=5)
    assert BODY in [row['body'] for row in r.new_messages(0)]


def test_http_old_queue_remains_held_after_ongoing_transition(tmp_path):
    from app.integrations.mac_models import MacDeliveryClaim
    old,state,raw,new=prepared(tmp_path)
    settings=Settings(database_url=f'sqlite:///{tmp_path}/backend.sqlite',sms_provider='mac_messages',
        mac_bridge_enabled=True,mac_bridge_token=TOKEN,admin_password='synthetic-password-123',
        mac_demo_phones=PHONE,mac_message_services='SMS,iMessage',mac_test_sessions=json.dumps(new['test_sessions']),
        mac_ongoing_authorization=json.dumps(new['ongoing_authorization']))
    app=create_app(settings);app.state.clock=FakeClock(NOW);app.state.mac_delivery_clock=FakeClock(NOW)
    with app.state.session_factory() as s:
        row=m.Message(direction='out',phone=PHONE,body='Old reviewed body',purpose='signup_reply',kind='ai',status='queued',
            provider_sid='MAC'+'a'*32+':old',created_at=NOW-timedelta(hours=1))
        s.add(row);s.commit();rowid=row.id
    with TestClient(app) as client:
        result=client.post('/mac/outbound/pull',headers={'Authorization':'Bearer '+TOKEN},json={})
        assert result.status_code==200 and result.json()['messages']==[]
    with app.state.session_factory() as s:
        assert s.get(m.Message,rowid).status=='blocked_test_session'
        assert s.scalar(select(MacDeliveryClaim)) is None


def test_ongoing_history_preserves_original_sender_facts_and_stop_guard(tmp_path,session):
    from app.core.conversational_signup import sender_history,source
    from app.core.signup_review_authority import received
    old,state,raw,new=prepared(tmp_path)
    selected=provider(new).test_sessions[PHONE]
    session.info['mac_test_session']=selected
    person=m.Volunteer(name='Synthetic Person',phone=PHONE,sms_opt_in=True,status='active',preferences={},created_at=selected.starts_at)
    session.add(person);session.flush()
    session.add(m.Policy(key='conversational_signup:'+PHONE,value={'value':True,'session_id':selected.id}))
    records=[]
    for body,when in [('Any two Sundays.',NOW-timedelta(hours=2)),(BODY,NOW)]:
        record=m.Message(volunteer_id=person.id,phone=PHONE,body=body,kind='mac_test_in',
            direction='in',status='received',purpose='test:'+selected.id,created_at=when)
        session.add(record);records.append(record)
    session.flush()
    assert [r['body'] for r in sender_history(session,person,NOW)]==['Any two Sundays.',BODY]
    assert source(session,person,records[-1].id,NOW) is records[-1]
    assert received(session,person,records[-1].id,NOW) is records[-1]
    session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}));session.flush()
    assert source(session,person,records[-1].id,NOW) is None
    assert received(session,person,records[-1].id,NOW) is None


def test_ongoing_needs_journal_and_explicit_enabled_mac_transport(tmp_path):
    from app.sms.provider import get_provider
    old,state,raw,new=prepared(tmp_path)
    from dataclasses import replace
    settings=Settings(sms_provider='mac_messages',mac_bridge_enabled=True,mac_bridge_token=TOKEN,
        admin_password='synthetic-password-123',mac_demo_phones=PHONE,mac_message_services='SMS,iMessage',
        mac_test_sessions=json.dumps(new['test_sessions']),mac_ongoing_authorization='')
    with pytest.raises(ValueError):MacMessagesProvider(settings)
    assert not isinstance(get_provider(replace(settings,mac_bridge_enabled=False)),MacMessagesProvider)
