"""Real protocol, synthetic roster/claims and disposable connector files only."""
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.integrations import mac_roster as r
from app.integrations.mac_models import MacDeliveryClaim
from app.integrations.mac_messages import MacWorker,atomic_json
from app.integrations.mac_ongoing import adopt,enroll,_digest
from app.main import create_app
from app.web.texty import admin
from tests.test_mac_participant_enrollment import enrolled,settings,NEW
from tests.test_mac_ongoing import PHONE,NOW,TOKEN

ADDED='+13035552001'


class Reader:
    def __init__(self): self.reads=[]
    def new_messages(self,after): self.reads.append(after);return []


@pytest.fixture(params=('v1','v2'))
def roster(tmp_path,request):
    current,state,raw,config=enrolled(tmp_path)
    if request.param=='v1': config=current
    state,_=adopt(config,state,raw,[],datetime.now(timezone.utc))
    atomic_json(Path(config['state_path']),state)
    canonical=tmp_path/'connector.json';atomic_json(canonical,config)
    selected=settings(config)
    selected=replace(selected,database_url=f'sqlite:///{tmp_path}/app.sqlite',gloo_signup_replies=True)
    app=create_app(selected)
    app.state.mac_delivery_clock=SimpleNamespace(now=lambda:datetime.now(timezone.utc))
    with app.state.session_factory() as session:
        for i,phone in enumerate([*config["phones"],ADDED]):
            session.add(m.Volunteer(name=f'Example Participant {i}',phone=phone,status='active',sms_opt_in=True,
                preferences={},created_at=datetime.now(timezone.utc)))
        session.add(m.Message(id=7,phone=PHONE,direction='out',kind='admin',purpose='reply',status='submitted',
            provider_sid=app.state.provider.test_sessions[PHONE].outbound_prefix+'fixture',body='Synthetic earlier receipt',
            created_at=datetime.now(timezone.utc)))
        session.add(MacDeliveryClaim(message_id=7,token='old-token'))
        session.add(m.Policy(key=r.POLICY,value={'enabled':True,'actor':'Authorized fixture coordinator'}))
        session.add(m.Policy(key='full_text_onboarding',value={'value':True}))
        session.commit()
        person_id=session.scalar(select(m.Volunteer.id).where(m.Volunteer.phone==ADDED))
    app.dependency_overrides[admin]=lambda:{'email':'coordinator@example.test'}
    calls=[];fault={}
    with TestClient(app) as backend:
        def transport(request):
            calls.append(request.url.path)
            if hook:=fault.get('before'): hook(request)
            response=backend.request(request.method,request.url.path,json=json.loads(request.content),headers={'Authorization':'Bearer '+TOKEN})
            if fault.get('lose')==request.url.path:
                fault.pop('lose');raise httpx.ReadError('Synthetic lost response')
            return httpx.Response(response.status_code,json=response.json())
        client=httpx.Client(transport=httpx.MockTransport(transport))
        reader=Reader()
        def worker():
            return MacWorker(json.loads(canonical.read_text()),client=client,reader=reader,
                roster_enrollment=True,config_path=canonical,sender=lambda *args:pytest.fail('No native sends permitted'))
        yield SimpleNamespace(app=app,backend=backend,client=client,reader=reader,worker=worker,config=config,
            state=state,canonical=canonical,person_id=person_id,calls=calls,fault=fault,tmp_path=tmp_path)
        client.close()


def assert_unchanged_records(f):
    with f.app.state.session_factory() as session:
        assert len(session.scalars(select(m.Message)).all())==1
        assert len(session.scalars(select(MacDeliveryClaim)).all())==1
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.AgentRun)) is None
        assert session.scalar(select(m.Assignment)) is None
        assert not session.get(m.Volunteer,f.person_id).preferences.get('onboarding_stage')


def test_website_ready_roster_enrolls_without_welcome_and_preserves_existing_scope(roster):
    f=roster;w=f.worker()
    before=f.backend.get('/api/state').json()
    person=next(v for v in before['volunteers'] if v['id']==str(f.person_id))
    assert person['text_setup_block_code']=='enrollment_pending'
    assert 'Connecting this volunteer' in person['text_setup_block_reason']
    started=datetime.now(timezone.utc);w.once()
    assert ADDED in w.phones and ADDED in f.app.state.provider.phones
    assert w.state['after']==f.state['after'] and w.state['dispatches']==f.state['dispatches']
    assert w.config['test_sessions'][PHONE]==f.config['test_sessions'][PHONE]
    for phone,spec in f.config['test_sessions'].items():
        assert w.config['test_sessions'][phone]==spec
    assert w.test_sessions[ADDED].starts_at>=started
    assert not w.test_sessions[ADDED].active(started-timedelta(seconds=1))
    assert not w.enrollment_path.exists()
    assert f.reader.reads==[f.state['after']]
    after=f.backend.get('/api/state').json()
    assert next(v for v in after['volunteers'] if v['id']==str(f.person_id))['can_start_text_setup']
    with f.app.state.session_factory() as session:
        scope=session.get(m.Policy,r.SCOPE).value['journal']
        assert scope['journal_id']==w.ongoing_journal['journal_id']
        assert session.get(m.Policy,'conversational_signup:'+ADDED).value['session_id']==w.test_sessions[ADDED].id
    restarted=create_app(f.app.state.settings)
    assert ADDED in restarted.state.provider.phones
    assert_unchanged_records(f)


@pytest.mark.parametrize('lost',['/mac/roster/prepare','/mac/roster/adopted'])
def test_lost_response_resumes_same_signed_revision_and_durable_ack(roster,lost):
    f=roster;w=f.worker();f.fault['lose']=lost
    with pytest.raises(httpx.ReadError): w.once()
    assert w.enrollment_path.exists() and f.reader.reads==[]
    transaction=json.loads(w.enrollment_path.read_text())
    session_id=transaction['config']['test_sessions'][ADDED]['id']
    w=f.worker();w.once()
    assert w.test_sessions[ADDED].id==session_id
    assert not w.enrollment_path.exists() and len(f.reader.reads)==1
    assert_unchanged_records(f)


@pytest.mark.parametrize('where',['configuration','checkpoint'])
def test_crash_between_split_writes_resumes_prepared_revision(roster,monkeypatch,where):
    import app.integrations.mac_messages as module
    f=roster;original=module.atomic_json
    target=f.canonical if where=='configuration' else Path(f.config['state_path'])
    def crashing(path,value):
        original(path,value)
        if Path(path)==target: raise OSError('Synthetic crash after atomic write')
    w=f.worker();monkeypatch.setattr(module,'atomic_json',crashing)
    with pytest.raises(OSError): w.once()
    assert f.reader.reads==[] and w.enrollment_path.exists()
    assert ADDED not in f.app.state.provider.phones
    staged=json.loads(f.canonical.read_text())
    if where=='configuration':
        # Backend may restart from the canonical staged config. It must restore
        # accepted scope rather than admitting the unacknowledged participant.
        restarted=create_app(replace(settings(staged),database_url=f.app.state.settings.database_url))
        assert ADDED not in restarted.state.provider.phones
    monkeypatch.setattr(module,'atomic_json',original)
    w=f.worker();w.once()
    assert ADDED in w.phones and ADDED in f.app.state.provider.phones
    assert_unchanged_records(f)


@pytest.mark.parametrize('revoke',['consent','stop','policy','status'])
def test_revocation_after_worker_adoption_before_backend_activation_rolls_back_exactly(roster,revoke):
    f=roster;old_bytes=Path(f.config['state_path']).read_bytes()
    def before(request):
        if request.url.path!='/mac/roster/adopted': return
        f.fault.pop('before')
        assert ADDED in json.loads(f.canonical.read_text())['phones']
        assert ADDED not in f.app.state.provider.phones
        with f.app.state.session_factory() as session:
            person=session.get(m.Volunteer,f.person_id)
            if revoke=='consent': person.sms_opt_in=False
            elif revoke=='status': person.status='inactive'
            elif revoke=='stop': session.add(m.Policy(key='sms_opt_out:'+ADDED,value={'value':True}))
            else: session.get(m.Policy,r.POLICY).value={'enabled':False}
            session.commit()
    f.fault['before']=before;w=f.worker();w.once()
    assert ADDED not in w.phones and ADDED not in f.app.state.provider.phones
    assert json.loads(f.canonical.read_text())==f.config
    assert Path(f.config['state_path']).read_bytes()==old_bytes
    assert not w.enrollment_path.exists()
    assert_unchanged_records(f)


def test_crash_during_cancelled_rollback_recovers_before_native_reads(roster,monkeypatch):
    import app.integrations.mac_messages as module
    f=roster;original=module.atomic_json
    def before(request):
        if request.url.path=='/mac/roster/adopted':
            with f.app.state.session_factory() as session:
                session.get(m.Volunteer,f.person_id).sms_opt_in=False;session.commit()
    def crashing(path,value):
        original(path,value)
        if Path(path)==f.canonical and ADDED not in value['phones']:
            raise OSError('Synthetic rollback split-write crash')
    w=f.worker();f.fault['before']=before;monkeypatch.setattr(module,'atomic_json',crashing)
    with pytest.raises(OSError): w.once()
    assert f.reader.reads==[]
    monkeypatch.setattr(module,'atomic_json',original);f.fault.clear()
    w=f.worker();w.once()
    assert ADDED not in w.phones and w.state==f.state and not w.enrollment_path.exists()
    assert_unchanged_records(f)


@pytest.mark.parametrize('exclude',['consent','paused','stopped','synthetic','seed','dataset','name','reserved','pending_consent'])
def test_ineligible_and_fictional_candidates_never_receive_an_offer(roster,exclude):
    f=roster
    with f.app.state.session_factory() as session:
        person=session.get(m.Volunteer,f.person_id)
        if exclude=='consent': person.sms_opt_in=False
        elif exclude=='paused': person.status='inactive'
        elif exclude=='stopped': session.add(m.Policy(key='sms_opt_out:'+ADDED,value={'value':True}))
        elif exclude=='name': person.name+=' [Fictional]'
        elif exclude=='reserved': person.phone='+12025550142'
        else: person.preferences={ {'synthetic':'synthetic','seed':'fictional_seed','dataset':'synthetic_dataset','pending_consent':'consent_pending'}[exclude]:True }
        session.commit()
    w=f.worker();w.once()
    assert f.calls==['/mac/roster/poll']
    assert w.phones==frozenset(f.config['phones'])
    with f.app.state.session_factory() as session:
        assert session.get(m.Policy,r.PENDING) is None
    assert_unchanged_records(f)


def test_default_off_requires_both_operator_policy_and_explicit_worker_flag(roster):
    f=roster
    w=MacWorker(f.config,client=f.client,reader=f.reader)
    w.once();assert f.calls==[]
    with f.app.state.session_factory() as session:
        session.get(m.Policy,r.POLICY).value={'enabled':False};session.commit()
    w=f.worker();w.once()
    assert f.calls==['/mac/roster/poll'] and ADDED not in w.phones


def test_local_pending_and_unknown_claims_stay_held_without_guessing_from_empty_queue(roster):
    f=roster;w=f.worker()
    w.state['claim_response_uncertain']=True;w.save()
    with f.app.state.session_factory() as session:
        session.add(m.Message(id=8,phone=PHONE,direction='out',kind='admin',purpose='reply',status='submitted',
            provider_sid=f.app.state.provider.test_sessions[PHONE].outbound_prefix+'unknown',body='Synthetic unknown receipt',created_at=datetime.now(timezone.utc)))
        session.add(MacDeliveryClaim(message_id=8,token='unknown-token'));session.commit()
    w.once()
    assert w.state['claim_response_uncertain'] is True
    assert f.calls==['/mac/roster/ledger'] and ADDED not in w.phones
    w.state['dispatches']['8']={'token':'unknown-token','outcome':'submitted'};w.save()
    w.once()
    assert not w.state.get('claim_response_uncertain') and ADDED in w.phones


def test_unresolved_native_claim_blocks_offer_and_ledger_reconciliation(roster):
    f=roster
    with f.app.state.session_factory() as session:
        session.get(m.Message,7).status='uncertain';session.commit()
    w=f.worker();w.state['claim_response_uncertain']=True;w.save();w.once()
    assert w.state['claim_response_uncertain'] is True and ADDED not in w.phones


@pytest.mark.parametrize('recorded',[False,True])
def test_orphan_native_claim_never_clears_uncertainty_or_enrolls(roster,recorded):
    f=roster;w=f.worker()
    with f.app.state.session_factory() as session:
        session.add(MacDeliveryClaim(message_id=9876,token='synthetic-orphan-token'));session.commit()
    w.state['claim_response_uncertain']=True
    if recorded:w.state['dispatches']['9876']={'token':'synthetic-orphan-token','outcome':'submitted'}
    w.save();w.once()
    assert w.state['claim_response_uncertain'] is True
    assert f.calls==['/mac/roster/ledger'] and ADDED not in w.phones


def test_competing_signed_proposal_and_wrong_or_stale_adoption_are_rejected(roster):
    f=roster;w=f.worker();headers={'Authorization':'Bearer '+TOKEN}
    offer=w.post('/mac/roster/poll',{'journal_id':w.ongoing_journal['journal_id']})
    raw=w.state_path.read_bytes();now=datetime.now(timezone.utc)
    first=enroll(w.config,raw,phone=ADDED,name=offer['name'],actor=offer['actor'],operator_confirmed=True,active=[],now=now)
    second=enroll(w.config,raw,phone=ADDED,name=offer['name'],actor=offer['actor'],operator_confirmed=True,active=[],now=now)
    body={'intent_id':offer['intent_id'],'journal':first['ongoing_authorization']}
    assert f.backend.post('/mac/roster/prepare',json=body,headers=headers).json()['phase']=='prepared'
    assert f.backend.post('/mac/roster/prepare',json={**body,'journal':second['ongoing_authorization']},headers=headers).status_code==409
    assert f.backend.post('/mac/outbound/pull',json={},headers=headers).json()['roster_enrollment_pending']
    wrong={'intent_id':offer['intent_id'],'journal_id':'f'*32,'checkpoint_sha256':'a'*64,'configuration_sha256':'b'*64}
    assert f.backend.post('/mac/roster/adopted',json=wrong,headers=headers).status_code==409
    assert ADDED not in f.app.state.provider.phones
    assert f.backend.post('/mac/roster/status',json={'intent_id':'0'*32},headers=headers).status_code==409
    assert f.backend.post('/mac/roster/poll',json={'journal_id':'0'*32},headers=headers).status_code==409
    assert f.backend.post('/mac/roster/poll',json={'journal_id':w.ongoing_journal['journal_id']}).status_code==401
    assert_unchanged_records(f)


def test_later_website_additions_enroll_and_stale_ack_cannot_rewind_scope(roster):
    f=roster;w=f.worker();w.once()
    with f.app.state.session_factory() as session:
        old_scope=session.get(m.Policy,r.SCOPE).value
        old_intent=session.get(m.Policy,r.INTENT+old_scope['intent_id']).value
    added=f.backend.post('/api/volunteers',json={'first_name':'Future','last_name':'Example',
        'phone':'+13035552002','consent':True,'ministry':'Production'})
    assert added.status_code==200,added.text
    candidate='+13035552002';w.once()
    assert candidate in w.phones and w.test_sessions[candidate].id!=w.test_sessions[ADDED].id
    assert w.state['dispatches']==f.state['dispatches'] and w.state['after']==f.state['after']
    ack={'intent_id':old_scope['intent_id'],'journal_id':old_intent['journal_id'],
         'configuration_sha256':old_intent['journal']['configuration_sha256'],
         'checkpoint_sha256':old_intent['adopted_checkpoint_sha256']}
    assert f.backend.post('/mac/roster/adopted',json=ack,headers={'Authorization':'Bearer '+TOKEN}).status_code==409
    assert candidate in f.app.state.provider.phones
    assert_unchanged_records(f)


def test_backend_commit_before_provider_install_recovers_from_durable_scope(roster,monkeypatch):
    f=roster;original=r.install
    def fail(state,value):
        if ADDED in value['sessions']: raise OSError('Synthetic crash after backend commit')
        return original(state,value)
    w=f.worker();monkeypatch.setattr(r,'install',fail)
    with pytest.raises(OSError):w.once()
    assert ADDED not in f.app.state.provider.phones and f.reader.reads==[]
    monkeypatch.setattr(r,'install',original)
    w=f.worker();w.once()
    assert ADDED in f.app.state.provider.phones and ADDED in w.phones
    assert_unchanged_records(f)


def test_40_generic_enrollments_preserve_base_session_cursor_ledger_and_hold_new_history(roster):
    f=roster;w=f.worker()
    with f.app.state.session_factory() as session:
        for i in range(39):
            session.add(m.Volunteer(name=f'Additional Example {i}',phone=f'+13035553{i:03}',status='active',
                sms_opt_in=True,preferences={},created_at=datetime.now(timezone.utc)))
        session.commit()
    for _ in range(40):w.once()
    assert len(w.phones)==len(f.config['phones'])+40
    assert len({selected.id for selected in w.test_sessions.values()})==len(w.phones)
    assert w.test_sessions[PHONE].spec()==f.config['test_sessions'][PHONE]
    assert w.state['after']==f.state['after'] and w.state['dispatches']==f.state['dispatches']
    for phone,selected in w.test_sessions.items():
        if phone not in f.config['phones']:
            assert not selected.active(selected.starts_at-timedelta(microseconds=1))
    w=f.worker();w.once()
    assert len(w.phones)==len(f.config['phones'])+40
    assert_unchanged_records(f)
