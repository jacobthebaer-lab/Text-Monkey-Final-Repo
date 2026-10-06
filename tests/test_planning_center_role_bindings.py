"""Canonical roles remain explicit, reviewed and subject to real hard rules."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import eligibility
from app.db.models import Assignment, Policy, Role, Shift
from app.integrations.planning_center import PCOBase, PCOPositionScope, PlanningCenterError, sync_schedule
from app.integrations.planning_center_role_bindings import (
    KEY, PREFIX, propose_role_binding, apply_role_binding,
)
from app.integrations.planning_center_staffing import (
    enqueue_staffing_intent, process_staffing_outbox, refresh_staffing, map_volunteer,
)
from tests.test_planning_center import CONFIG, client
from tests.test_planning_center_frequency_reviews import USER
from tests.test_planning_center_staffing import StaffingAPI
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from app.main import create_app
from app.web.texty import admin


class RoleAPI(StaffingAPI):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.position_name = 'Greeter'
        self.duplicate_position = False
        self.local_change = None

    def collection(self, path):
        if path == '/services/v2/service_types/20/team_positions':
            rows = [{'type': 'TeamPosition', 'id': '90', 'attributes': {'name': self.position_name},
                'relationships': {'team': {'data': {'type': 'Team', 'id': '30'}}}}]
            if self.duplicate_position:
                rows.append({**deepcopy(rows[0]), 'id': '91'})
            return rows
        if path.endswith('/needed_positions'):
            if self.local_change:
                callback, self.local_change = self.local_change, None
                callback()
        return super().collection(path)


@pytest.fixture
def lane(session, clock):
    PCOBase.metadata.create_all(session.get_bind())
    with client(quantity=2) as remote:
        sync_schedule(session, remote, CONFIG)
    shift = session.scalar(select(Shift))
    role = Role(name='Greeter', ministry='Serving', required_qualifications=[],
        criticality='standard', fill_policy='needs_approval')
    session.add(role); session.commit()
    session.info[KEY] = b'synthetic-private-role-signing-key-0001'
    api = RoleAPI(starts=shift.event.starts_at, ends=shift.event.ends_at, quantity=2)
    settings = Settings(admin_email_allowlist=USER['email'])
    mapping = dict(shift_id=shift.id, local_role_id=role.id, team_id='30', position_id='90', plan_time_id='60')
    return dict(session=session, clock=clock, api=api, settings=settings, mapping=mapping, role=role)


def review(lane):
    return propose_role_binding(lane['session'], lane['api'], CONFIG, lane['settings'],
        user=USER, now=lane['clock'].now(), **lane['mapping'])


def apply(lane, proposal):
    return apply_role_binding(lane['session'], lane['api'], CONFIG, lane['settings'], user=USER,
        now=lane['clock'].now(), review_hash=proposal['review_hash'], review_token=proposal['review_token'],
        **lane['mapping'])


def test_reviewed_binding_preserves_policy_and_import_refresh(lane, make_volunteer):
    s = lane['session']; before = deepcopy(lane['role'].required_qualifications)
    proposal = review(lane)
    assert not list(s.scalars(select(Policy).where(Policy.key.like(PREFIX + '%'))))
    result = apply(lane, proposal); s.commit(); s.expire_all()
    assert len(result['shift_ids']) == 2 and not lane['api'].writes
    assert {x.role_id for x in s.scalars(select(Shift))} == {lane['role'].id}
    assert lane['role'].required_qualifications == before
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete','interested_roles':['Greeter'],
        'role_frequency_caps':[{'role_id':lane['role'].id,'role_name':'Greeter','max_per_month':1}]})
    shift = s.get(Shift, result['shift_ids'][0]); assert eligibility.check(s, volunteer, shift)
    s.add(Assignment(shift_id=shift.id, volunteer_id=volunteer.id, status='confirmed', source='admin',
        created_at=lane['clock'].now(), updated_at=lane['clock'].now()));s.commit()
    second = s.get(Shift, result['shift_ids'][1])
    assert not eligibility.check(s, volunteer, second, _exclude_assignment_id=None)
    with client(quantity=1) as schedule:
        # The schedule reader and strict role preflight share the fake API.
        original_request, original_collection = schedule.request, schedule.collection
        schedule.request = lambda method,path,**kw: lane['api'].request(method,path,**kw) if path in (
            '/services/v2/teams/30','/services/v2/service_types/20/team_positions/90',
            '/services/v2/service_types/20/plans/40') else original_request(method,path,**kw)
        schedule.collection = lambda path: lane['api'].collection(path) if path.endswith('/team_members') or path.endswith('/team_positions') else original_collection(path)
        sync_schedule(s, schedule, CONFIG)
    assert {x.role_id for x in s.scalars(select(Shift))} == {lane['role'].id}


@pytest.mark.parametrize('fault', ['role_policy','native_position','actor','expired','signature','assignment'])
def test_changed_or_unauthorized_review_never_rebinds(lane, make_volunteer, fault):
    s=lane['session']; old_ids=[x.role_id for x in s.scalars(select(Shift))];proposal=review(lane)
    if fault=='role_policy':lane['role'].required_qualifications=['sound_training'];s.commit()
    elif fault=='native_position':lane['api'].position_name='Renamed'
    elif fault=='actor':proposal['review_token']['document']=proposal['review_token']['document'].replace(USER['id'],'00000000-0000-4000-8000-000000000002')
    elif fault=='expired':lane['clock'].advance(timedelta(minutes=11))
    elif fault=='signature':proposal['review_token']['signature']='0'*64
    else:
        volunteer=make_volunteer();s.add(Assignment(shift_id=lane['mapping']['shift_id'],volunteer_id=volunteer.id,
            status='cancelled',source='admin',created_at=lane['clock'].now(),updated_at=lane['clock'].now()));s.commit()
    with pytest.raises(PlanningCenterError):apply(lane,proposal)
    s.rollback()
    assert [x.role_id for x in s.scalars(select(Shift))]==old_ids and not lane['api'].writes


def test_duplicate_native_position_requires_exact_review(lane):
    lane['api'].duplicate_position=True
    with pytest.raises(PlanningCenterError,match='ambiguous'):review(lane)


def test_worker_and_inbound_reconciliation_keep_canonical_qualifications(lane,make_volunteer):
    s=lane['session'];lane['role'].required_qualifications=['sound_training'];s.commit()
    result=apply(lane,review(lane));s.commit()
    volunteer=make_volunteer(prefs={'onboarding_stage':'complete','interested_roles':['Greeter']})
    map_volunteer(s,CONFIG,volunteer.id,'70',lane['clock'].now(),client=lane['api'])
    assignment=Assignment(shift_id=result['shift_ids'][0],volunteer_id=volunteer.id,status='confirmed',
        source='admin',created_at=lane['clock'].now(),updated_at=lane['clock'].now());s.add(assignment);s.flush()
    intent=enqueue_staffing_intent(s,CONFIG,assignment_id=assignment.id,action='accept',now=lane['clock'].now());s.commit()
    factory=sessionmaker(bind=s.get_bind(),expire_on_commit=False,info={KEY:s.info[KEY]})
    assert process_staffing_outbox(factory,lane['api'],CONFIG,lane['clock'].now(),enabled=True)['held']==1
    assert not lane['api'].writes
    assignment.status='cancelled';s.commit()
    # Remove only the synthetic held intent so inbound eligibility is the tested barrier.
    s.delete(intent);s.commit();lane['api'].rows=[lane['api'].member()]
    assert refresh_staffing(s,lane['api'],CONFIG,lane['clock'].now(),service_type_id='20',plan_id='40')['conflicts']==1
    assert assignment.status=='cancelled' and not volunteer.qualifications


def test_tampered_review_or_role_policy_holds_future_import(lane):
    s=lane['session'];apply(lane,review(lane));s.commit()
    lane['role'].fill_policy='auto';s.commit()
    with client() as api:
        with pytest.raises(PlanningCenterError,match='local_policy_changed'):sync_schedule(s,api,CONFIG)


@pytest.mark.parametrize('qualified',[False,True])
def test_unsaved_canonical_probe_preserves_qualification_gate_with_whole_roots(lane,make_volunteer,qualified):
    s=lane['session'];lane['role'].required_qualifications=['sound_training'];s.commit()
    apply(lane,review(lane));s.commit()
    person=make_volunteer(quals=[('sound_training','verified',None)] if qualified else [],
        prefs={'onboarding_stage':'complete','interested_roles':[lane['role'].name]})
    map_volunteer(s,CONFIG,person.id,'70',lane['clock'].now(),client=lane['api'])
    s.commit();lane['api'].rows=[lane['api'].member()]
    result=refresh_staffing(s,lane['api'],CONFIG,lane['clock'].now(),service_type_id='20',plan_id='40')
    assert result['conflicts']==(0 if qualified else 1)
    assert bool(s.scalar(select(Assignment).where(Assignment.volunteer_id==person.id))) is qualified
    assert not lane['api'].writes


def test_local_change_during_native_reads_holds_before_rebind(lane):
    proposal=review(lane);s=lane['session']
    def change():
        with sessionmaker(bind=s.get_bind())() as other:
            other.get(Role,lane['role'].id).required_qualifications=['sound_training'];other.commit()
    lane['api'].local_change=change
    with pytest.raises(PlanningCenterError,match='context_changed'):apply(lane,proposal)
    s.rollback()
    assert s.get(Shift,lane['mapping']['shift_id']).role_id!=lane['role'].id


def test_signed_scope_tampering_and_missing_key_hold_import(lane):
    s=lane['session'];apply(lane,review(lane));s.commit()
    scope=s.scalar(select(PCOPositionScope));scope.position_id='91';s.commit()
    with client() as api:
        with pytest.raises(PlanningCenterError,match='changed'):sync_schedule(s,api,CONFIG)
    scope.position_id='90';s.commit();s.info.pop(KEY)
    with client() as api:
        with pytest.raises(PlanningCenterError,match='signing_key'):sync_schedule(s,api,CONFIG)


def test_policy_change_can_be_explicitly_reviewed_again_without_rewriting_preferences(lane):
    s=lane['session'];apply(lane,review(lane));s.commit()
    lane['role'].required_qualifications=['sound_training'];s.commit()
    proposal=review(lane);assert proposal['snapshot']['local']['role']['required_qualifications']==['sound_training']
    apply(lane,proposal);s.commit()
    assert lane['role'].required_qualifications==['sound_training']


@pytest.fixture
def api_lane(lane,tmp_path,monkeypatch):
    key=tmp_path/'signer.bin';key.write_bytes(lane['session'].info[KEY]);key.chmod(0o600)
    settings=replace(lane['settings'],database_url='sqlite://',automation_enabled=False,
        pco_position_mapping_enabled=True,pco_review_signing_key_path=str(key))
    app=create_app(settings);original=app.state.engine
    app.state.engine=lane['session'].get_bind()
    app.state.session_factory=sessionmaker(bind=app.state.engine,expire_on_commit=False)
    app.state.pco_config=CONFIG;app.state.mac_delivery_clock=lane['clock']
    app.dependency_overrides[admin]=lambda:dict(USER)
    monkeypatch.setattr('app.web.planning_center_roles.PCOClient',lambda _:lane['api'])
    with TestClient(app) as client:yield lane,app,client
    original.dispose()


def test_authenticated_api_catalogue_and_exact_apply(api_lane):
    lane,app,c=api_lane
    catalogue=c.get('/api/planning-center/role-bindings/catalogue');assert catalogue.status_code==200,catalogue.text
    assert len(catalogue.json()['shifts'])==2 and not catalogue.json()['native_writes']
    proposal=c.post('/api/planning-center/role-bindings/proposal',json=lane['mapping'])
    assert proposal.status_code==200,proposal.text
    value=proposal.json();assert value['snapshot']['local']['role']['name']=='Greeter'
    body={**lane['mapping'],'review_hash':value['review_hash'],'review_token':value['review_token']}
    result=c.post('/api/planning-center/role-bindings',json=body)
    assert result.status_code==200,result.text
    assert result.json()['local_role_id']==lane['role'].id and not lane['api'].writes
    assert not app.state.settings.pco_staffing_write_enabled and not app.state.settings.pco_staffing_poll_enabled


@pytest.mark.parametrize('fault',['disabled','missing_key','missing_auth','foreign_actor','client_actor','coerced_role'])
def test_api_holds_without_current_owner_review(api_lane,fault):
    lane,app,c=api_lane;body=dict(lane['mapping']);expected=409
    if fault=='disabled':app.state.settings=replace(app.state.settings,pco_position_mapping_enabled=False);expected=404
    elif fault=='missing_key':app.state.settings=replace(app.state.settings,pco_review_signing_key_path='');expected=503
    elif fault=='missing_auth':app.dependency_overrides.pop(admin);expected=503
    elif fault=='foreign_actor':app.dependency_overrides[admin]=lambda:{**USER,'email':'foreign@example.test'}
    elif fault=='client_actor':body['actor']=USER;expected=422
    else:body['local_role_id']=str(body['local_role_id']);expected=422
    result=c.post('/api/planning-center/role-bindings/proposal',json=body)
    assert result.status_code==expected,result.text
    assert not lane['api'].writes
    assert not list(lane['session'].scalars(select(Policy).where(Policy.key.like(PREFIX+'%'))))
