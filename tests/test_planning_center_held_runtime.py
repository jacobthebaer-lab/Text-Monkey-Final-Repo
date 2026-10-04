"""Mounted real app/auth flow, temporary synthetic SQLite and fake HTTP only."""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from app.config import Settings, settings_from_env
from app.core import planning_center_held_preview as capture
from app.db import models as m
from app.integrations.planning_center import PCOClient
from app.integrations.planning_center_availability import MembershipBinding, PCOAvailabilityIntent, PCOAvailabilityPreview
from app.integrations.planning_center_review_models import PCOFrequencyReviewReceipt
from app.integrations.profile_models import ProfileOutbox
from app.main import create_app
from tests.test_planning_center_frequency_reviews import lane, USER, CONFIG, KEY
from tests.test_planning_center_frequency_executor import Native

FEATURES={'pco_availability_previews','pco_availability_intents','pco_frequency_claims',
          'pco_frequency_attempts','pco_frequency_ownership','pco_frequency_review_receipts'}
LEGACY={'pco_event_links','pco_shift_links','pco_deliveries','pco_volunteer_people',
        'pco_staffing_links','pco_staffing_intents','pco_position_scopes','pco_staffing_leases','pco_staffing_polls'}

@pytest.fixture
def runtime(lane,tmp_path,monkeypatch):
    with lane['factory']() as s:
        binding=lane['proposal'](s)['membership']
    bindings=tmp_path/'bindings.json';key=tmp_path/'key'
    document={'schema':1,'organization_id':'10','volunteer_id':lane['volunteer_id'],
              'person_id':'70','memberships':[binding]}
    bindings.write_text(json.dumps(document));bindings.chmod(0o600)
    key.write_bytes(KEY);key.chmod(0o600)
    settings=replace(lane['settings'],database_url=str(lane['engine'].url),automation_enabled=False,
        pco_review_enabled=True,pco_review_bindings_path=str(bindings),pco_review_signing_key_path=str(key))
    native=Native([MembershipBinding(**binding)],lane['factory'])
    monkeypatch.setattr(capture,'PCOClient',lambda config:PCOClient(config,transport=httpx.MockTransport(native.handle)))
    user=dict(USER)
    class Auth:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,headers):
            assert url==settings.supabase_url+'/auth/v1/user'
            assert headers['Authorization']=='Bearer synthetic-session'
            return httpx.Response(200,json=user)
    monkeypatch.setattr('app.web.texty.httpx.AsyncClient',lambda **kwargs:Auth())
    app=create_app(settings);app.state.pco_config=CONFIG
    with TestClient(app) as client:
        yield dict(lane=lane,settings=settings,client=client,app=app,native=native,user=user,
                   bindings=bindings,document=document,key=key,headers={'Authorization':'Bearer synthetic-session'})
    app.state.engine.dispose()


def post(runtime):
    return runtime['client'].post('/api/planning-center/held-previews',headers=runtime['headers'],
                                 json={'volunteer_id':runtime['lane']['volunteer_id']})


def test_actual_mounted_capture_review_receipt_is_held_and_side_effect_free(runtime):
    lane=runtime['lane']
    with lane['factory']() as s: saved=deepcopy(s.get(m.Volunteer,lane['volunteer_id']).preferences)
    result=post(runtime);assert result.status_code==200,result.text
    data=result.json();assert data['preview_id'] and data['execution_enabled'] is False
    op=data['operations'][0]
    assert op['state']=='held' and 'unowned_remote_frequency_preserved' in op['holds']
    assert 'notification_policy_not_verified' in op['holds']
    path='/api/planning-center/frequency-reviews/'+op['intent_key']
    proposal=runtime['client'].get(path,headers=runtime['headers']);assert proposal.status_code==200,proposal.text
    assert proposal.json()['native_snapshot']=={'schedule_preference':'Every week',
                                               'saved_at':data['native_snapshot_saved_at']}
    exact={k:proposal.json()[k] for k in ('preview_hash','source_hash','remote_hash','operation_hash')}
    receipt=runtime['client'].post(path,headers=runtime['headers'],json=exact)
    assert receipt.status_code==200 and receipt.json()['state']=='reviewed_held'
    assert receipt.json()['execution_enabled'] is False
    assert runtime['client'].post(path+'/execute',headers=runtime['headers'],json={}).status_code==404
    assert runtime['native'].requests and all(method=='GET' for method,_ in runtime['native'].requests)
    with lane['factory']() as s:
        assert s.get(m.Volunteer,lane['volunteer_id']).preferences==saved
        assert len(s.scalars(select(m.Message)).all())==1
        assert not s.scalars(select(m.Assignment)).all()
        assert len(s.scalars(select(PCOFrequencyReviewReceipt)).all())==1
    # Same source/native snapshot has deterministic IDs and no duplicate outbox.
    repeat=post(runtime);assert repeat.status_code==200 and repeat.json()['preview_id']==data['preview_id']


def test_equal_native_preference_is_noop_without_inventing_ownership(runtime):
    runtime['native'].rows['80']['attributes']['schedule_preference']='Twice a month'
    result=post(runtime);assert result.status_code==200,result.text
    assert result.json()['operations'][0]['state']=='noop'
    assert result.json()['execution_enabled'] is False


def test_latest_profile_route_must_match_genuine_receipt_before_native_get(runtime):
    with runtime['lane']['factory']() as s:
        row=s.get(ProfileOutbox,runtime['lane']['profile_key'])
        row.payload={**row.payload,'route':'availability'}
        s.commit()
    response=post(runtime)
    assert response.status_code==409
    assert response.json()['detail']['reason']=='committed_source_availability_receipt_required'
    assert not runtime['native'].requests


@pytest.mark.parametrize('fault',['no_auth','foreign_actor','unconfirmed','extra_payload','unknown_volunteer'])
def test_auth_and_strict_selection_reject_before_native_get(runtime,fault):
    client=runtime['client'];headers=runtime['headers'];body={'volunteer_id':runtime['lane']['volunteer_id']}
    expected=409
    if fault=='no_auth':headers={};expected=401
    elif fault=='foreign_actor':runtime['user']['email']='foreign@example.test';expected=403
    elif fault=='unconfirmed':runtime['user']['email_confirmed_at']=None;expected=403
    elif fault=='extra_payload':body['memberships']=runtime['document']['memberships'];expected=422
    else:body['volunteer_id']=999999
    assert client.post('/api/planning-center/held-previews',headers=headers,json=body).status_code==expected
    assert not runtime['native'].requests


@pytest.mark.parametrize('fault',['foreign_person','foreign_org','foreign_role','foreign_service','missing_file','public_file'])
def test_invalid_operator_bindings_hold_before_native_get(runtime,fault):
    doc=runtime['document']
    if fault=='foreign_person':doc['person_id']='71'
    elif fault=='foreign_org':doc['organization_id']='11'
    elif fault=='foreign_role':doc['memberships'][0]['role_name']='Coffee'
    elif fault=='foreign_service':doc['memberships'][0]['service_type_id']='999'
    if fault=='missing_file':runtime['bindings'].unlink()
    else:
        runtime['bindings'].write_text(json.dumps(doc))
        if fault=='public_file':runtime['bindings'].chmod(0o644)
    result=post(runtime);assert result.status_code==409
    assert not runtime['native'].requests


@pytest.mark.parametrize('fault',['missing','weak','public'])
def test_missing_or_invalid_signing_key_keeps_preview_but_holds_receipt(runtime,fault):
    if fault=='missing':runtime['key'].unlink()
    elif fault=='weak':runtime['key'].write_bytes(b'short')
    else:runtime['key'].chmod(0o644)
    runtime['app'].state.pco_review_signing_key=capture.signing_key(str(runtime['key']))
    result=post(runtime);assert result.status_code==200
    path='/api/planning-center/frequency-reviews/'+result.json()['operations'][0]['intent_key']
    proposal=runtime['client'].get(path,headers=runtime['headers']).json()
    exact={k:proposal[k] for k in ('preview_hash','source_hash','remote_hash','operation_hash')}
    assert runtime['client'].post(path,headers=runtime['headers'],json=exact).status_code==409
    with runtime['lane']['factory']() as s:assert not s.scalars(select(PCOFrequencyReviewReceipt)).all()


@pytest.mark.parametrize('fault',['source','native','binding_file'])
def test_change_during_native_reads_prevents_new_preview_commit(runtime,fault):
    original=runtime['native'].handle;changed=[]
    def handle(request):
        response=original(request)
        if not changed and request.url.path.endswith('/40/person_team_position_assignments/80'):
            changed.append(True)
            if fault=='source':
                with runtime['lane']['factory']() as s:
                    person=s.get(m.Volunteer,runtime['lane']['volunteer_id']);person.sms_opt_in=False;s.commit()
            elif fault=='native':runtime['native'].rows['80']['attributes']['preferred_weeks']=[2]
            else:
                document=runtime['document'];document['memberships'][0]['position_name']='Changed'
                runtime['bindings'].write_text(json.dumps(document))
        return response
    original_client=capture.PCOClient
    # Point the existing fake factory at the new behavior, not a network client.
    capture.PCOClient=lambda config:PCOClient(config,transport=httpx.MockTransport(handle))
    try:assert post(runtime).status_code==409
    finally:capture.PCOClient=original_client
    with runtime['lane']['factory']() as s:assert len(s.scalars(select(PCOAvailabilityPreview)).all())==1
    assert all(method=='GET' for method,_ in runtime['native'].requests)


@pytest.mark.parametrize('enabled',[False,True])
def test_startup_never_creates_feature_schema_even_after_models_are_imported(tmp_path,enabled):
    db=tmp_path/'fresh.sqlite'
    settings=Settings(database_url='sqlite:///'+str(db),automation_enabled=False,pco_review_enabled=enabled,
        supabase_url='https://synthetic.supabase.test',supabase_publishable_key='synthetic',
        admin_email_allowlist=USER['email'])
    first=create_app(settings);second=create_app(settings)
    names=set(inspect(second.state.engine).get_table_names())
    assert LEGACY<=names and not FEATURES&names
    with TestClient(second) as client:
        response=client.post('/api/planning-center/held-previews',json={'volunteer_id':1})
        assert response.status_code==(401 if enabled else 404)
    first.state.engine.dispose();second.state.engine.dispose()


def test_missing_schema_holds_authenticated_capture_and_existing_review_without_creation(runtime):
    with runtime['app'].state.engine.begin() as connection:
        connection.execute(text('DROP TABLE pco_frequency_review_receipts'))
    assert post(runtime).status_code==503
    path='/api/planning-center/frequency-reviews/'+runtime['lane']['intent_key']
    assert runtime['client'].get(path,headers=runtime['headers']).status_code==503
    assert 'pco_frequency_review_receipts' not in inspect(runtime['app'].state.engine).get_table_names()
    assert not runtime['native'].requests


def test_review_configuration_is_separate_and_default_off(monkeypatch):
    for key in ('PCO_REVIEW_ENABLED','PCO_REVIEW_BINDINGS_PATH','PCO_REVIEW_SIGNING_KEY_PATH'):
        monkeypatch.delenv(key,raising=False)
    assert not settings_from_env().pco_review_enabled
    monkeypatch.setenv('PCO_REVIEW_ENABLED','true')
    monkeypatch.setenv('PCO_REVIEW_BINDINGS_PATH','/synthetic/private/bindings.json')
    monkeypatch.setenv('PCO_REVIEW_SIGNING_KEY_PATH','/synthetic/private/key')
    settings=settings_from_env()
    assert settings.pco_review_enabled and settings.pco_review_bindings_path.endswith('bindings.json')
    assert not settings.pco_staffing_write_enabled and not settings.pco_staffing_poll_enabled
