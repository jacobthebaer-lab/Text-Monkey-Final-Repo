"""Synthetic native HTTP/committed-save tests. No real people or API calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.core.planning_center_committed_source import committed_source_factory
from app.db.models import Policy, Volunteer
from app.integrations.planning_center import PCOBase, PCOClient, PCOConfig, PCOVolunteerPerson, PlanningCenterError
from app.integrations.planning_center_availability import capture_source, resource_hash
from app.integrations.planning_center_blockouts import (
    _key, _signed, blockout_status, bootstrap_owned_blockout, issue_blockout_policy,
    queue_blockout_sync, sign_blockout_acceptance, sync_person_blockouts,
)

CONFIG = PCOConfig('synthetic','synthetic','10',('20',))
KEY = b'synthetic-blockout-key-for-tests-only-12345'
NOW = datetime(2026,10,1,16,tzinfo=timezone.utc)
USER = {'id':'00000000-0000-4000-8000-000000000001','email':'admin@example.test','email_confirmed_at':'2026-10-01T00:00:00Z'}
SETTINGS = Settings(admin_email_allowlist='admin@example.test')


def rel(kind, identifier):
    return {'data':{'type':kind,'id':identifier}}


def block(identifier, attrs):
    return {'type':'Blockout','id':identifier,'attributes':{**deepcopy(attrs),
        'time_zone':'America/Denver','updated_at':'2026-10-01T16:00:00Z'},
        'relationships':{'person':rel('Person','70'),'organization':rel('Organization','10')}}


class Native:
    def __init__(self, factory, vid):
        self.factory, self.vid = factory, vid
        self.rows=[]; self.requests=[];self.sequence=90;self.mode='success';self.before_write=None
        self.after_write=None;self.bad_generated=False;self.before_get=None;self.booking_rows=[]
        self.organization='10';self.wrong_person=False;self.fail_readback=False

    def handle(self, request):
        path=request.url.path;method=request.method;self.requests.append((method,path))
        assert request.headers['X-PCO-API-Version']=='2018-11-01'
        if self.before_get and method=='GET':self.before_get(path)
        if method=='GET' and self.fail_readback and any(method!='GET' for method,_ in self.requests):
            return httpx.Response(503,json={})
        base='/services/v2/people/70/blockouts'
        if method!='GET':
            assert path==base or path.startswith(base+'/')
            # There is a committed immutable unknown claim before HTTP; no
            # local SQLite write transaction may survive into this callback.
            with self.factory() as db:
                _,journal=_signed(db,_key('j','10',self.vid),KEY)
                assert journal['unknown']
                _,attempt=_signed(db,journal['unknown'],KEY)
                assert attempt['state']=='unknown' and attempt['document']['operation']['method']==method
            with self.factory() as db:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE');db.rollback()
            if self.before_write:self.before_write()
            if self.mode=='timeout_before':raise httpx.ReadTimeout('synthetic',request=request)
            if method=='POST':
                self.sequence+=1;row=block(str(self.sequence),json.loads(request.content)['data']['attributes']);self.rows.append(row)
            elif method=='PATCH':
                row=next(row for row in self.rows if row['id']==path.rsplit('/',1)[-1])
                row['attributes'].update(json.loads(request.content)['data']['attributes'])
                row['attributes']['updated_at']='2026-10-01T16:01:00Z'
            elif method=='DELETE':
                self.rows=[row for row in self.rows if row['id']!=path.rsplit('/',1)[-1]];row=None
            else:raise AssertionError(method)
            if self.after_write:self.after_write()
            if self.mode=='timeout_after':raise httpx.ReadTimeout('synthetic',request=request)
            return httpx.Response(204 if method=='DELETE' else 200,json={'data':deepcopy(row)} if row else None)
        if path=='/services/v2':data={'type':'Organization','id':self.organization}
        elif path=='/services/v2/people/70':data={'type':'Person','id':'71' if self.wrong_person else '70','attributes':{}}
        elif path=='/services/v2/people/70/plan_people':data=deepcopy(self.booking_rows)
        elif path==base:data=deepcopy(self.rows)
        elif path.endswith('/blockout_dates'):
            row=next(row for row in self.rows if row['id']==path.split('/')[-2]);attrs=row['attributes']
            data=[{'type':'BlockoutDate','id':'200'+row['id'],'attributes':{
                'starts_at_utc':attrs['starts_at'],'ends_at_utc':attrs['ends_at'],
                'time_zone':'UTC' if self.bad_generated else 'America/Denver'}}]
        else:raise AssertionError(path)
        return httpx.Response(200,json={'data':data,'links':{}})


@pytest.fixture
def lane(session,make_volunteer):
    PCOBase.metadata.create_all(session.get_bind())
    volunteer=make_volunteer(prefs={'consent_at':NOW.isoformat(),'consent_source':'synthetic',
        'onboarding_availability_draft':{'unavailable_dates':['2026-10-18'],'availability_known':True,
            'frequency_known':True,'all_day':True,'weekdays':[],'max_per_month':None}})
    session.add(PCOVolunteerPerson(organization_id='10',volunteer_id=volunteer.id,person_id='70',created_at=NOW));session.commit()
    factory=committed_source_factory(session.get_bind())
    native=Native(factory,volunteer.id);client=PCOClient(CONFIG,transport=httpx.MockTransport(native.handle))
    revision=['1'];now=[NOW]
    def reader(db):
        return capture_source(db,CONFIG,volunteer_id=volunteer.id,
            provenance={'source_id':'synthetic-source','receipt_id':'synthetic-receipt','revision':revision[0]},
            tz='America/Denver',now=now[0])
    acceptance=sign_blockout_acceptance(CONFIG,timezone_name='America/Denver',evidence_hash='a'*64,
                                       verified_at=NOW.isoformat(),signing_key=KEY)
    with factory() as db:
        issue_blockout_policy(db,SETTINGS,CONFIG,volunteer.id,user=USER,clock=lambda:now[0],signing_key=KEY,
                             enabled=True,acceptance=acceptance)
        queue_blockout_sync(db,'10',volunteer.id,revision='1');db.commit()
    def sync(**kwargs):
        return sync_person_blockouts(factory,client,CONFIG,volunteer.id,source_reader=reader,
                                     clock=lambda:now[0],enabled=kwargs.pop('enabled',True),signing_key=KEY,acceptance=kwargs.pop('acceptance',acceptance),**kwargs)
    def save(dates):
        revision[0]=str(int(revision[0])+1)
        with factory() as db:
            person=db.get(Volunteer,volunteer.id);prefs=deepcopy(person.preferences)
            prefs['onboarding_availability_draft']['unavailable_dates']=dates;person.preferences=prefs
            queue_blockout_sync(db,'10',volunteer.id,revision=revision[0]);db.commit()
    yield SimpleNamespace(factory=factory,native=native,client=client,vid=volunteer.id,reader=reader,
                          sync=sync,save=save,revision=revision,acceptance=acceptance)
    client.http.close()


def writes(lane):return [method for method,_ in lane.native.requests if method!='GET']


def test_add_change_remove_and_replay_have_one_owned_identity(lane):
    assert lane.sync()['state']=='verified';assert writes(lane)==['POST']
    original=lane.native.rows[0]['id']
    assert lane.sync()['state']=='verified';assert writes(lane)==['POST']
    lane.save(['2026-10-18','2026-10-19'])
    assert lane.sync()['state']=='verified';assert writes(lane)==['POST','PATCH']
    assert lane.native.rows[0]['id']==original
    assert lane.native.rows[0]['attributes']['ends_at']=='2026-10-20T05:59:59Z'
    lane.save([]);assert lane.sync()['state']=='verified';assert writes(lane)==['POST','PATCH','DELETE']
    assert lane.native.rows==[]
    with lane.factory() as db:
        status=blockout_status(db,CONFIG,lane.vid,signing_key=KEY,runtime_enabled=True,acceptance=lane.acceptance)
        assert status['owned_count']==0 and status['verified_revision']==lane.revision[0]


def test_range_move_creates_replacement_before_deleting_owned_old_range(lane):
    assert lane.sync()['state']=='verified'
    lane.save(['2026-10-25']);assert lane.sync()['state']=='verified'
    assert writes(lane)==['POST','POST','DELETE']
    assert lane.native.rows[0]['attributes']['starts_at']=='2026-10-25T06:00:00Z'


@pytest.mark.parametrize('method',['POST','PATCH','DELETE'])
def test_lost_ack_get_reconciles_without_resending(method,lane):
    if method!='POST':assert lane.sync()['state']=='verified'
    if method=='PATCH':lane.save(['2026-10-18','2026-10-19'])
    if method=='DELETE':lane.save([])
    lane.native.mode='timeout_after'
    assert lane.sync()['state']=='unknown'
    before=writes(lane).copy();lane.native.mode='success'
    expected='unknown' if method=='POST' else 'verified'
    assert lane.sync()['state']==expected;assert writes(lane)==before


def test_absent_post_after_timeout_never_blind_retries_or_marks_verified(lane):
    lane.native.mode='timeout_before';assert lane.sync()['state']=='unknown'
    lane.native.mode='success'
    assert lane.sync()['state']=='unknown';assert writes(lane)==['POST']
    lane.save(['2026-10-25']);assert lane.sync()['state']=='unknown';assert writes(lane)==['POST']


def test_duplicate_new_post_rows_remain_unknown(lane):
    lane.native.after_write=lambda:lane.native.rows.append(block('999',lane.native.rows[0]['attributes']))
    assert lane.sync()['state']=='unknown';assert writes(lane)==['POST']
    assert lane.sync()['state']=='unknown';assert writes(lane)==['POST']


def test_coordinator_ranges_never_adopted_or_deleted(lane):
    attrs={'starts_at':'2026-10-18T06:00:00Z','ends_at':'2026-10-19T05:59:59Z',
           'reason':'Coordinator exclusion','repeat_frequency':'no_repeat','share':False}
    lane.native.rows=[block('900',attrs)]
    assert lane.sync()['state']=='verified';assert writes(lane)==[]
    lane.save([]);assert lane.sync()['state']=='verified';assert writes(lane)==[]
    assert lane.native.rows[0]['id']=='900'


def test_coordinator_edit_to_owned_range_holds_overwrite_and_delete(lane):
    assert lane.sync()['state']=='verified'
    lane.native.rows[0]['attributes']['reason']='Coordinator edit'
    lane.save([]);assert lane.sync()=={'state':'held','reason':'blockout_owned_native_baseline_changed'}
    assert writes(lane)==['POST']


def test_queue_rollback_and_same_revision_dedup(lane):
    with lane.factory() as db:
        key=queue_blockout_sync(db,'10',lane.vid,revision='2');db.rollback()
    with lane.factory() as db:
        assert db.get(Policy,key).value['revision']=='1'
        queue_blockout_sync(db,'10',lane.vid,revision='1');db.commit()
    assert lane.sync()['state']=='verified';assert writes(lane)==['POST']


@pytest.mark.parametrize('change',['optout','phone','mapping','source','pending'])
def test_stale_consent_identity_or_source_holds_without_native_write(change,lane):
    with lane.factory() as db:
        volunteer=db.get(Volunteer,lane.vid)
        if change=='optout':volunteer.sms_opt_in=False
        if change=='phone':volunteer.phone='+13035550998'
        if change=='mapping':db.scalar(select(PCOVolunteerPerson)).person_id='71'
        if change=='pending':volunteer.preferences={**volunteer.preferences,'pending_constraints':['unresolved']}
        db.commit()
    if change=='source':lane.revision[0]='2'
    assert lane.sync()['state']=='held';assert writes(lane)==[]


def test_source_change_after_write_recovers_owned_result_then_applies_new_source(lane):
    lane.native.after_write=lambda:lane.save(['2026-10-25'])
    lane.native.fail_readback=True;assert lane.sync()['state']=='unknown'
    lane.native.after_write=None;lane.native.fail_readback=False;lane.native.mode='success'
    assert lane.sync()['state']=='verified';assert writes(lane)==['POST','POST','DELETE']


def test_disable_preserves_unknown_and_reconciles_get_only(lane):
    lane.native.fail_readback=True;assert lane.sync()['state']=='unknown'
    with lane.factory() as db:
        issue_blockout_policy(db,SETTINGS,CONFIG,lane.vid,user=USER,clock=lambda:NOW,
                             signing_key=None,enabled=False,acceptance=None);db.commit()
    lane.native.fail_readback=False;lane.native.mode='success';assert lane.sync()['state']=='held'
    assert writes(lane)==['POST']
    with lane.factory() as db:
        state=blockout_status(db,CONFIG,lane.vid,signing_key=KEY,runtime_enabled=True,acceptance=None)
        assert state['policy_enabled'] is False and state['owned_count']==1 and state['state']=='disabled'


def test_native_booking_changes_are_reported_never_cancelled_by_executor(lane):
    lane.native.booking_rows=[{'type':'PlanPerson','id':'700','attributes':{'status':'C'},'relationships':{'person':rel('Person','70')}}]
    lane.native.after_write=lambda:lane.native.booking_rows[0]['attributes'].update(status='D')
    assert lane.sync()=={'state':'unknown','reason':'native_bookings_changed'}
    assert writes(lane)==['POST']


def test_generated_dates_must_verify_timezone_and_exact_requested_span(lane):
    lane.native.bad_generated=True;assert lane.sync()['state']=='unknown'
    assert writes(lane)==['POST']
    assert lane.sync()['state']=='unknown';assert writes(lane)==['POST']


def test_server_acceptance_required_and_tampered_policy_rejected(lane):
    with lane.factory() as db:
        with pytest.raises(PlanningCenterError,match='server_acceptance'):
            issue_blockout_policy(db,SETTINGS,CONFIG,lane.vid,user=USER,clock=lambda:NOW,
                signing_key=KEY,enabled=True,acceptance={'evidence_hash':'a'*64})
        db.rollback()
        row=db.get(Policy,_key('p','10',lane.vid));value=deepcopy(row.value)
        value['document']['person_id']='71';row.value=value;db.commit()
    assert lane.sync()['state']=='held';assert writes(lane)==[]


def test_runtime_off_is_get_only_and_role_hour_limits_never_projected(lane):
    assert lane.sync(enabled=False)['state']=='disabled';assert writes(lane)==[]
    with lane.factory() as db:
        volunteer=db.get(Volunteer,lane.vid);prefs=deepcopy(volunteer.preferences)
        prefs['onboarding_availability_draft']['unavailable_dates']=[]
        prefs['onboarding_availability_draft']['recurring_windows']=[{'weekday':6,'any_role':True,
            'role_ids':[],'role_label':None,'start_time':'08:00','end_time':'10:00','all_day':False,'event_context':None}]
        volunteer.preferences=prefs;db.commit()
    assert lane.sync()['state']=='verified';assert writes(lane)==[]


def test_prior_owned_receipt_bootstrap_exact_hash_then_removal(lane):
    attrs={'starts_at':'2026-10-18T06:00:00Z','ends_at':'2026-10-19T05:59:59Z',
           'reason':'Text Monkey unavailable dates','repeat_frequency':'no_repeat','share':False}
    row=block('900',attrs);lane.native.rows=[row]
    proof={'organization_id':'10','person_id':'70','volunteer_id':lane.vid,'remote_id':'900',
           'body':{'data':{'type':'Blockout','attributes':attrs}},'snapshot_hash':resource_hash(row),
           'receipt_hash':'b'*64,'logical_key':'date:2026-10-18'}
    assert bootstrap_owned_blockout(lane.factory,lane.client,CONFIG,lane.vid,receipt_verifier=lambda:proof,
                                   signing_key=KEY,clock=lambda:NOW)['state']=='owned'
    lane.save([]);assert lane.sync()['state']=='verified';assert writes(lane)==['DELETE']


def test_unknown_recovery_after_optout_gets_owner_without_new_effects(lane):
    lane.native.fail_readback=True;assert lane.sync()['state']=='unknown'
    with lane.factory() as db:
        db.get(Volunteer,lane.vid).sms_opt_in=False;db.commit()
    lane.native.fail_readback=False;lane.native.mode='success';assert lane.sync()['state']=='held';assert writes(lane)==['POST']
    with lane.factory() as db:
        _,journal=_signed(db,_key('j','10',lane.vid),KEY)
        assert journal['unknown'] is None and len(journal['owned'])==1


def test_changed_installed_acceptance_cannot_use_old_grant(lane):
    accepted=sign_blockout_acceptance(CONFIG,timezone_name='America/Denver',evidence_hash='c'*64,
                                     verified_at=NOW.isoformat(),signing_key=KEY)
    assert lane.sync(acceptance=accepted)=={'state':'held','reason':'blockout_policy_acceptance_changed'}
    assert writes(lane)==[]


@pytest.mark.parametrize('bad',['organization','person'])
def test_native_scope_mismatch_cannot_write(bad,lane):
    if bad=='organization':lane.native.organization='11'
    else:lane.native.wrong_person=True
    assert lane.sync()['state']=='held';assert writes(lane)==[]


def test_fresh_native_edit_after_claim_cannot_write(lane):
    called=[False]
    def edit(path):
        with lane.factory() as db:
            _,journal=_signed(db,_key('j','10',lane.vid),KEY)
        if path.endswith('/blockouts') and journal and journal['unknown'] and not called[0]:
            called[0]=True
            lane.native.rows.append(block('900',{'starts_at':'2026-10-25T06:00:00Z',
                'ends_at':'2026-10-26T05:59:59Z','reason':'Coordinator','repeat_frequency':'no_repeat','share':False}))
    lane.native.before_get=edit
    assert lane.sync()=={'state':'held','reason':'blockout_pre_http_source_or_native_changed'};assert writes(lane)==[]
    lane.native.before_get=None
    assert lane.sync()['state']=='verified';assert writes(lane)==['POST']


def test_second_worker_can_only_get_reconcile_existing_claim(lane):
    results=[]
    lane.native.before_write=lambda:results.append(lane.sync())
    assert lane.sync()['state']=='verified'
    assert results and results[0]['state']=='unknown';assert writes(lane)==['POST']


@pytest.mark.parametrize('dates,start,end',[
    (['2026-11-01'],'2026-11-01T06:00:00Z','2026-11-02T06:59:59Z'),
    (['2027-03-14'],'2027-03-14T07:00:00Z','2027-03-15T05:59:59Z'),
])
def test_executor_keeps_finite_native_dst_day_contract(lane,dates,start,end):
    lane.save(dates);assert lane.sync()['state']=='verified'
    assert lane.native.rows[0]['attributes']['starts_at']==start
    assert lane.native.rows[0]['attributes']['ends_at']==end


@pytest.mark.parametrize('tamper',['identity','hash','body'])
def test_prior_ownership_bootstrap_refuses_unverified_original_record(tamper,lane):
    attrs={'starts_at':'2026-10-18T06:00:00Z','ends_at':'2026-10-19T05:59:59Z',
           'reason':'Text Monkey unavailable dates','repeat_frequency':'no_repeat','share':False}
    row=block('900',attrs);lane.native.rows=[row]
    proof={'organization_id':'10','person_id':'70','volunteer_id':lane.vid,'remote_id':'900',
           'body':{'data':{'type':'Blockout','attributes':attrs}},'snapshot_hash':resource_hash(row),
           'receipt_hash':'b'*64,'logical_key':'date:2026-10-18'}
    if tamper=='identity':proof['person_id']='71'
    if tamper=='hash':proof['snapshot_hash']='c'*64
    if tamper=='body':proof['body']['data']['attributes']['ends_at']='2026-10-19T06:00:00Z'
    with pytest.raises(PlanningCenterError):
        bootstrap_owned_blockout(lane.factory,lane.client,CONFIG,lane.vid,receipt_verifier=lambda:proof,
                                signing_key=KEY,clock=lambda:NOW)
    assert writes(lane)==[]


@pytest.fixture
def api(lane,tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.web.planning_center_blockouts import router
    from app.web.texty import admin
    # Caller lane independently tests full committed-source enable bootstrap.
    monkeypatch.setattr('app.web.planning_center_blockouts._enqueue_existing',
        lambda session,state,vid,user:queue_blockout_sync(session,state.pco_config.organization_id,vid,revision=lane.revision[0]))
    key=tmp_path/'key';key.write_bytes(KEY);key.chmod(0o600)
    accepted=tmp_path/'acceptance.json';accepted.write_text(json.dumps(lane.acceptance));accepted.chmod(0o600)
    settings=Settings(admin_email_allowlist='admin@example.test',pco_blockout_write_enabled=True,
        pco_blockout_signing_key_path=str(key),pco_blockout_acceptance_path=str(accepted))
    app=FastAPI();app.state.settings=settings;app.state.pco_config=CONFIG
    app.state.clock=SimpleNamespace(now=lambda:NOW);app.state.session_factory=lane.factory
    app.include_router(router);app.dependency_overrides[admin]=lambda:USER
    with TestClient(app) as client:
        yield client,app,key,accepted


def test_api_enable_resolves_server_acceptance_no_client_evidence_or_native_ids(lane,api):
    client,app,_,_=api
    response=client.get(f'/api/planning-center/blockouts/{lane.vid}');assert response.status_code==200
    value=response.json();assert value['acceptance']['available'] and value['readiness']['mapping_ready']
    assert value['effect_scope']=='all_services_teams' and value['notification_mode']=='provider_managed'
    response=client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':True});assert response.status_code==200
    assert response.json()['policy_enabled'];assert writes(lane)==[]
    response=client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':True,'evidence_hash':'a'*64})
    assert response.status_code==422
    response=client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':'true'})
    assert response.status_code==422


def test_api_disable_works_missing_key_acceptance_mapping_and_consent(lane,api):
    client,app,key,accepted=api
    key.unlink();accepted.unlink()
    with lane.factory() as db:
        person=db.get(Volunteer,lane.vid);person.sms_opt_in=False
        mapping=db.scalar(select(PCOVolunteerPerson));db.delete(mapping);db.commit()
    response=client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':False})
    assert response.status_code==200 and response.json()['state']=='disabled'
    assert response.json()['policy_enabled'] is False
    response=client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':True})
    assert response.status_code==409 and response.json()['detail']['reason']=='blockout_server_acceptance_required'


def test_api_unsigned_or_foreign_installed_acceptance_cannot_enable(lane,api):
    client,app,_,accepted=api
    document=deepcopy(lane.acceptance);document['document']['organization_id']='11'
    accepted.write_text(json.dumps(document))
    assert client.get(f'/api/planning-center/blockouts/{lane.vid}').json()['acceptance']['available'] is False
    assert client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':True}).status_code==409
    assert writes(lane)==[]


def test_api_unauthenticated_cannot_change_policy(lane,api):
    client,app,_,_=api;app.dependency_overrides.clear()
    response=client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':False})
    assert response.status_code in (401,503);assert writes(lane)==[]



def test_api_failed_existing_source_enqueue_rolls_back_policy_enable(lane,api,monkeypatch):
    client,app,_,_=api
    assert client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':False}).status_code==200
    def fail(*args):raise PlanningCenterError('blockout_source_saved_availability_required')
    monkeypatch.setattr('app.web.planning_center_blockouts._enqueue_existing',fail)
    response=client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':True})
    assert response.status_code==409
    assert client.get(f'/api/planning-center/blockouts/{lane.vid}').json()['policy_enabled'] is False



@pytest.mark.parametrize('broken',['key','mapping','acceptance'])
def test_enabled_saved_choice_stays_on_while_readiness_holds(lane,api,broken):
    client,app,key,accepted=api
    if broken=='key':key.unlink()
    elif broken=='acceptance':accepted.unlink()
    else:
        with lane.factory() as db:db.delete(db.scalar(select(PCOVolunteerPerson)));db.commit()
    response=client.get(f'/api/planning-center/blockouts/{lane.vid}');assert response.status_code==200
    state=response.json()
    assert state['policy_enabled'] is True and state['state']=='held'
    assert state['readiness']['authority_ready'] is False and type(state['unknown_attempt']) is bool
    assert client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':False}).status_code==200


def test_expired_server_acceptance_holds_without_unsetting_saved_choice(lane):
    expired=sign_blockout_acceptance(CONFIG,timezone_name='America/Denver',evidence_hash='a'*64,
        verified_at=(NOW-timedelta(days=100)).isoformat(),signing_key=KEY)
    assert lane.sync(acceptance=expired)['reason']=='blockout_acceptance_expired_or_future'
    with lane.factory() as db:
        status=blockout_status(db,CONFIG,lane.vid,signing_key=KEY,runtime_enabled=True,acceptance=expired,now=NOW)
        assert status['policy_enabled'] is True and status['state']=='held' and not status['acceptance']['available']
    assert writes(lane)==[]



def test_process_death_after_claim_never_assumes_http_was_not_called(lane):
    def death(path):
        with lane.factory() as db:_,journal=_signed(db,_key('j','10',lane.vid),KEY)
        if journal and journal['unknown']:raise KeyboardInterrupt('synthetic death')
    lane.native.before_get=death
    with pytest.raises(KeyboardInterrupt):lane.sync()
    lane.native.before_get=None
    assert lane.sync()['state']=='unknown';assert writes(lane)==[]


@pytest.mark.parametrize('document',[None,[],{'broken':True}])
def test_status_malformed_stored_policy_does_not_release_or_crash(lane,api,document):
    client,_,_,_=api
    with lane.factory() as db:
        row=db.get(Policy,_key('p','10',lane.vid));row.value={'document':document,'signature':'invalid'};db.commit()
    response=client.get(f'/api/planning-center/blockouts/{lane.vid}')
    assert response.status_code==200 and response.json()['state']=='held'
    assert response.json()['readiness']['authority_ready'] is False
    assert client.put(f'/api/planning-center/blockouts/{lane.vid}/policy',json={'enabled':False}).status_code==200
    assert writes(lane)==[]


def test_file_backed_http_can_take_independent_sqlite_writer_lock(lane,tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    path=tmp_path/'isolated.sqlite'
    raw=lane.factory.kw['bind'].raw_connection()
    try:
        with sqlite3.connect(path) as destination:raw.driver_connection.backup(destination)
    finally:raw.close()
    engine=create_engine('sqlite:///'+str(path));factory=committed_source_factory(engine)
    previous=lane.native.factory;lane.native.factory=factory
    try:
        result=sync_person_blockouts(factory,lane.client,CONFIG,lane.vid,source_reader=lane.reader,
            clock=lambda:NOW,enabled=True,signing_key=KEY,acceptance=lane.acceptance)
        assert result['state']=='verified' and writes(lane)==['POST']
        with factory() as db:
            keys=db.scalars(select(Policy.key)).all();assert all(len(key)<=80 for key in keys)
    finally:lane.native.factory=previous;engine.dispose()



def test_mutating_or_flushed_source_callback_cannot_release_native_write(lane):
    def mutate(db):
        source=lane.reader(db)
        db.get(Volunteer,lane.vid).name='Uncommitted change';db.flush()
        return source
    result=sync_person_blockouts(lane.factory,lane.client,CONFIG,lane.vid,source_reader=mutate,
        clock=lambda:NOW,enabled=True,signing_key=KEY,acceptance=lane.acceptance)
    assert result=={'state':'held','reason':'blockout_uncommitted_source_write'}
    assert writes(lane)==[]



def test_lost_post_cannot_adopt_identical_foreign_blockout_or_delete_it(lane):
    lane.native.mode='timeout_before';assert lane.sync()['state']=='unknown'
    attrs={'starts_at':'2026-10-18T06:00:00Z','ends_at':'2026-10-19T05:59:59Z',
           'reason':'Text Monkey unavailable dates','repeat_frequency':'no_repeat','share':False}
    lane.native.rows=[block('777',attrs)];lane.native.mode='success'
    assert lane.sync()=={'state':'unknown','reason':'post_identity_unknown'}
    lane.save([]);assert lane.sync()['state']=='unknown'
    assert writes(lane)==['POST'] and lane.native.rows[0]['id']=='777'
    with lane.factory() as db:
        _,journal=_signed(db,_key('j','10',lane.vid),KEY);assert journal['owned']==[]


def test_returned_post_identity_persists_before_failed_get_and_recovers(lane):
    lane.native.fail_readback=True;assert lane.sync()['state']=='unknown'
    with lane.factory() as db:
        _,journal=_signed(db,_key('j','10',lane.vid),KEY)
        _,attempt=_signed(db,journal['unknown'],KEY)
        assert attempt['returned_id']==lane.native.rows[0]['id']
    lane.native.fail_readback=False;assert lane.sync()['state']=='verified';assert writes(lane)==['POST']



def test_prior_owned_reason_preserved_when_dates_match_and_held_preview_unchanged(lane):
    from app.integrations.planning_center_availability import FrozenSnapshot, OwnedResource, build_preview
    attrs={'starts_at':'2026-10-18T06:00:00Z','ends_at':'2026-10-19T05:59:59Z',
           'reason':'Original reviewed absence note','repeat_frequency':'no_repeat','share':False}
    row=block('900',attrs);lane.native.rows=[row]
    proof={'organization_id':'10','person_id':'70','volunteer_id':lane.vid,'remote_id':'900',
           'body':{'data':{'type':'Blockout','attributes':attrs}},'snapshot_hash':resource_hash(row),
           'receipt_hash':'b'*64,'logical_key':'date:2026-10-18'}
    bootstrap_owned_blockout(lane.factory,lane.client,CONFIG,lane.vid,receipt_verifier=lambda:proof,signing_key=KEY,clock=lambda:NOW)
    with lane.factory() as db:
        source=lane.reader(db)
        _,journal=_signed(db,_key('j','10',lane.vid),KEY)
    remote=FrozenSnapshot.capture({'organization_id':'10','person_id':'70','blockouts':[row],
        'blockout_dates':{'900':[]},'memberships':[]})
    preview=build_preview(source,remote,owned=[OwnedResource(**owner) for owner in journal['owned']])
    digest=preview.digest
    assert preview.value['operations'][0]['method']=='PATCH' and preview.value['operations'][0]['state']=='held'
    assert lane.sync()['state']=='verified' and writes(lane)==[]
    assert lane.native.rows[0]['attributes']['reason']=='Original reviewed absence note'
    assert preview.digest==digest
    lane.save(['2026-10-18','2026-10-19'])
    assert lane.sync()['state']=='verified' and writes(lane)==['PATCH']
    assert lane.native.rows[0]['attributes']['ends_at']=='2026-10-20T05:59:59Z'


def test_matching_dates_with_foreign_reason_edit_still_hold_full_ownership_hash(lane):
    assert lane.sync()['state']=='verified'
    lane.native.rows[0]['attributes']['reason']='Coordinator changed the owned reason'
    assert lane.sync()=={'state':'held','reason':'blockout_owned_native_baseline_changed'}
    assert writes(lane)==['POST']
