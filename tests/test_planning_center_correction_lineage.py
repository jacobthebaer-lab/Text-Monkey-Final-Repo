"""Synthetic exact historical lineage, fake auth and native SQLite only."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import sqlite3

import httpx
import pytest
from sqlalchemy import select

from app.core import planning_center_correction_lineage as lineage
from app.core.planning_center_committed_source import CommittedAvailabilityReader
from app.core import planning_center_frequency_reviews as reviews
from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt
from app.integrations.planning_center import PlanningCenterError
from app.integrations.planning_center_availability import (FrozenSnapshot, PCOAvailabilityIntent,
    PCOAvailabilityPreview, OwnedResource, PreviewPolicy, build_preview, enqueue_preview, resource_hash, _hash, _json)
from app.integrations.planning_center_frequency_executor import execute_frequency_intent, _supported
from app.integrations.profile_models import ProfileOutbox
from scripts.pco_provision_correction_lineage import accepted_manifest, authenticated_user, native_observer
from tests.test_planning_center_frequency_reviews import lane, CONFIG, USER, KEY, PHONE


@pytest.fixture
def correction(lane, tmp_path):
    now = lane['clock'][0]
    with lane['factory']() as s:
        current = s.get(ProfileOutbox, lane['profile_key'])
        current.payload = {**current.payload, 'route': 'onboarding_complete'}
        receipt = s.get(MacInboundReceipt, current.source_guid)
        receipt.result = {**receipt.result, 'intent': 'onboarding_review'}
        message = s.scalar(select(m.Message)); message.created_at = now - timedelta(seconds=2)
        old_profile = deepcopy(current.payload['profile'])
        old_profile['preferences']['role_frequency_caps'][0]['max_per_month'] = 1
        old_key = hashlib.sha256((current.source_id+'\0'+current.source_guid+'\0'+_json(old_profile)).encode()).hexdigest()
        s.add(ProfileOutbox(key=old_key, source_id=current.source_id, source_guid=current.source_guid,
            phone=PHONE, payload={'route': 'onboarding_review', 'profile': old_profile}, state='held',
            detail='synthetic_held', created_at=now-timedelta(seconds=1)))
        s.commit()
    signing = tmp_path/'lineage-key'; signing.write_bytes(KEY); signing.chmod(0o600)
    settings = replace(lane['settings'], pco_correction_lineage_enabled=True,
                       pco_correction_lineage_key_path=str(signing))
    reader = CommittedAvailabilityReader(settings, CONFIG, volunteer_id=lane['volunteer_id'],
        profile_key=lane['profile_key'], source_id=lane['source_id'], clock=lambda: lane['clock'][0],
        allow_audited_correction=True)
    with lane['factory']() as s:
        captured, target = reader._checked_read(s, inspecting=True)
    native_db = tmp_path/'native.sqlite'
    receiving = '+12025550148'
    with sqlite3.connect(native_db) as db:
        db.executescript('''CREATE TABLE message(guid TEXT, handle_id INTEGER, text TEXT, attributedBody BLOB,
            service TEXT, is_from_me INTEGER, destination_caller_id TEXT);
            CREATE TABLE handle(id TEXT); CREATE TABLE chat(service_name TEXT);
            CREATE TABLE chat_message_join(message_id INTEGER,chat_id INTEGER);
            CREATE TABLE chat_handle_join(chat_id INTEGER,handle_id INTEGER);''')
        db.execute('INSERT INTO message VALUES(?,?,?,?,?,?,?)',
            ('synthetic-guid',1,'Synthetic availability answer',None,'iMessage',0,receiving))
        db.execute('INSERT INTO handle VALUES(?)',(PHONE,))
        db.execute("INSERT INTO chat VALUES('iMessage')")
        db.execute('INSERT INTO chat_message_join VALUES(1,1)')
        db.execute('INSERT INTO chat_handle_join VALUES(1,1)')
    config = tmp_path/'native-config'; config.write_text(_json({'messages_db':str(native_db),
        'receiving_number':receiving, 'phones':[PHONE], 'services':['iMessage']})); config.chmod(0o600)
    observe = native_observer(str(config), {'row_id':1})
    with lane['factory']() as s: native = observe(s,target)
    manifest = {'schema':1,'binding':{**target,'previous_profile_key':old_key,
        'previous_profile_hash':_hash(old_profile),'previous_profile_state':'held','native':native},
        'audit':{**{k:'a'*64 for k in lineage.AUDIT_HASHES},'producer_commit':'a'*40,
                 'completed_at':now.isoformat()}}
    def provision(*, apply=True, user=USER, manifest_value=None):
        manifest_value = manifest if manifest_value is None else manifest_value
        with lane['factory']() as s:
            result = lineage.provision(s, settings, CONFIG, manifest=manifest_value,
                accepted_manifest_sha256=hashlib.sha256(_json(manifest_value).encode()).hexdigest(), user=user,
                clock=lambda:lane['clock'][0],native_observer=observe,apply=apply)
            s.commit()
            return result
    return dict(lane=lane,settings=settings,reader=reader,captured=captured,target=target,manifest=manifest,
        provision=provision,observe=observe,native_db=native_db,config=config,signing=signing)


def test_only_exact_committed_lineage_allows_tagged_preview(correction):
    c=correction; lane=c['lane']
    with lane['factory']() as s:
        with pytest.raises(PlanningCenterError,match='signed_record_required'):c['reader'](s)
    result=c['provision'](apply=False);assert result['applied'] is False
    with lane['factory']() as s:assert s.get(m.Policy,result['policy_key']) is None
    result=c['provision']();assert result['applied'] and len(result['policy_key'])==76
    with lane['factory']() as s:
        tagged=c['reader'](s)
        assert tagged.value['correction_lineage']['provider_signature_verified'] is False
        assert tagged.value['correction_lineage']['execution_allowed'] is False
        assert tagged.digest!=c['captured'].digest
        with pytest.raises(PlanningCenterError,match='availability_receipt_required'):lane['reader'](s)
    with pytest.raises(PlanningCenterError,match='already_exists'):c['provision']()


def test_original_rows_unchanged_and_new_policy_must_commit_before_read(correction):
    c=correction; lane=c['lane']
    with lane['factory']() as s:
        before=(deepcopy(s.get(MacInboundReceipt,'synthetic-guid').result),
                deepcopy(s.get(ProfileOutbox,lane['profile_key']).payload),
                deepcopy(s.get(m.Volunteer,lane['volunteer_id']).preferences))
        result=lineage.provision(s,c['settings'],CONFIG,manifest=c['manifest'],
            accepted_manifest_sha256=hashlib.sha256(_json(c['manifest']).encode()).hexdigest(),user=USER,
            clock=lambda:lane['clock'][0],native_observer=c['observe'],apply=True)
        with pytest.raises(PlanningCenterError,match='clean_session'):c['reader'](s)
        s.rollback()
    with lane['factory']() as s:assert s.get(m.Policy,result['policy_key']) is None
    c['provision']()
    with lane['factory']() as s:
        assert before==(s.get(MacInboundReceipt,'synthetic-guid').result,
                        s.get(ProfileOutbox,lane['profile_key']).payload,
                        s.get(m.Volunteer,lane['volunteer_id']).preferences)
        assert len(s.scalars(select(m.Message)).all())==1
        assert not s.scalars(select(m.Assignment)).all()


@pytest.mark.parametrize('fault',['signature','domain','action','provider_claim','execute_claim','actor',
    'guid','fingerprint','receipt_hash','audit','manifest_digest','revoked','time'])
def test_signed_record_tamper_or_overclaimed_authority_holds(correction,fault):
    c=correction; result=c['provision']()
    with c['lane']['factory']() as s:
        row=s.get(m.Policy,result['policy_key']);value=deepcopy(row.value);data=json.loads(value['document'])
        if fault=='signature':value['signature']='b'*64
        elif fault=='domain':value['signature']=hashlib.sha256(value['document'].encode()).hexdigest()
        elif fault=='revoked':value['state']='revoked'
        else:
            if fault=='action':data['action']='approve_frequency_write'
            elif fault=='provider_claim':data['provider_signature_verified']=True
            elif fault=='execute_claim':data['execution_allowed']=True
            elif fault=='actor':data['provisioner']['email']='foreign@example.test'
            elif fault=='guid':data['binding']['guid']='foreign-guid'
            elif fault=='fingerprint':data['binding']['fingerprint']='b'*64
            elif fault=='receipt_hash':data['binding']['receipt_result_hash']='b'*64
            elif fault=='audit':data['audit']['audit_sha256']='b'*64
            elif fault=='manifest_digest':data['accepted_manifest_sha256']='b'*64
            elif fault=='time':data['provisioned_at']=(c['lane']['clock'][0]+timedelta(seconds=1)).isoformat()
            value['document']=_json(data);value['signature']=lineage.signature(value['document'],KEY)
        row.value=value;s.commit()
    with c['lane']['factory']() as s:
        with pytest.raises(PlanningCenterError):c['reader'](s)


@pytest.mark.parametrize('fault',['optout','phone_scope','mapping','receipt','profile','newer','old_profile',
                                  'key_missing','flag_off','actor_removed'])
def test_current_source_permission_and_key_guards_survive_exception(correction,fault):
    c=correction;lane=c['lane'];c['provision']()
    settings=c['settings']
    if fault=='phone_scope':settings=replace(settings,mac_demo_phones='')
    elif fault=='flag_off':settings=replace(settings,pco_correction_lineage_enabled=False)
    elif fault=='actor_removed':settings=replace(settings,admin_email_allowlist='other@example.test')
    elif fault=='key_missing':c['signing'].unlink()
    else:
        with lane['factory']() as s:
            if fault=='optout':s.get(m.Volunteer,lane['volunteer_id']).sms_opt_in=False
            elif fault=='mapping':
                from app.integrations.planning_center import PCOVolunteerPerson
                s.scalar(select(PCOVolunteerPerson)).person_id='71'
            elif fault=='receipt':
                r=s.get(MacInboundReceipt,'synthetic-guid');r.result={**r.result,'session_id':'foreign'}
            elif fault=='profile':s.get(m.Volunteer,lane['volunteer_id']).name='Changed'
            elif fault=='old_profile':
                old=s.get(ProfileOutbox,c['manifest']['binding']['previous_profile_key']);old.payload={'route':'onboarding_review','profile':{}}
            else:
                current=s.get(ProfileOutbox,lane['profile_key'])
                s.add(ProfileOutbox(key='f'*64,source_id=current.source_id,source_guid=current.source_guid,
                    phone=PHONE,payload=current.payload,state='pending',created_at=lane['clock'][0]+timedelta(seconds=1)))
            s.commit()
    reader=CommittedAvailabilityReader(settings,CONFIG,volunteer_id=lane['volunteer_id'],
        profile_key=lane['profile_key'],source_id=lane['source_id'],clock=lambda:lane['clock'][0],allow_audited_correction=True)
    with lane['factory']() as s:
        with pytest.raises(PlanningCenterError):reader(s)


def test_private_manifest_and_real_auth_boundary(correction,tmp_path):
    c=correction;path=tmp_path/'manifest';raw=_json(c['manifest']).encode();path.write_bytes(raw);path.chmod(0o600)
    sha=hashlib.sha256(raw).hexdigest()
    assert accepted_manifest(str(path),sha)==c['manifest']
    with pytest.raises(PlanningCenterError):accepted_manifest(str(path),'a'*64)
    path.write_bytes(raw+b' ')
    with pytest.raises(PlanningCenterError,match='noncanonical'):
        accepted_manifest(str(path),hashlib.sha256(raw+b' ').hexdigest())
    def auth(request):
        assert request.url.path=='/auth/v1/user' and request.headers['Authorization']=='Bearer synthetic-session'
        return httpx.Response(200,json=USER)
    assert authenticated_user(c['settings'],'synthetic-session',transport=httpx.MockTransport(auth))==USER
    for user in ({**USER,'email_confirmed_at':None},{**USER,'email':'foreign@example.test'}):
        with pytest.raises(PlanningCenterError,match='verified_coordinator'):c['provision'](user=user)
    manifest=deepcopy(c['manifest']);manifest['actor']=USER
    with pytest.raises(PlanningCenterError,match='manifest_schema'):c['provision'](manifest_value=manifest)


@pytest.mark.parametrize('change',['body','sender','receiving_line','group','foreign_chat_handle','outgoing'])
def test_provision_reads_exact_native_row_and_refuses_changed_context(correction,change):
    c=correction
    with sqlite3.connect(c['native_db']) as db:
        if change=='body':db.execute("UPDATE message SET text='Changed'")
        elif change=='sender':db.execute("UPDATE handle SET id='+12025550149'")
        elif change=='receiving_line':db.execute("UPDATE message SET destination_caller_id='+12025550149'")
        elif change=='group':db.execute('INSERT INTO chat_handle_join VALUES(1,2)')
        elif change=='foreign_chat_handle':db.execute('UPDATE chat_handle_join SET handle_id=2')
        else:db.execute('UPDATE message SET is_from_me=1')
    with pytest.raises(PlanningCenterError):c['provision']()
    with c['lane']['factory']() as s:
        assert not [r for r in s.scalars(select(m.Policy)) if r.key.startswith('profile_fix:')]


def test_corrected_preview_can_be_reviewed_but_executor_cannot_use_exception(correction):
    c=correction;lane=c['lane'];c['provision']()
    with lane['factory']() as s:
        source=c['reader'](s)
        saved=s.scalar(select(PCOAvailabilityPreview));remote=FrozenSnapshot.capture(json.loads(saved.document)['remote'])
        member=remote.value['memberships'][0];binding=member['binding']
        # Even fabricate the low-level release/ownership inputs, which cannot
        # establish application authority. The executor must reject the tag.
        preview=build_preview(source,remote,owned=[OwnedResource('10','70','membership_frequency',
            'role:'+str(binding['role_id']),'80',resource_hash(member['resource']))],
            policy=PreviewPolicy(remote.digest,'a'*64,False,True,''))
        record=enqueue_preview(s,preview,source=source,remote=remote,now=lane['clock'][0]);s.commit()
        intent=s.scalar(select(PCOAvailabilityIntent).where(PCOAvailabilityIntent.preview_key==record.key))
        proposal=reviews.review_proposal(s,c['settings'],CONFIG,intent_key=intent.key,user=USER,clock=lambda:lane['clock'][0])
        assert 'audited_local_correction_preview_only' in proposal['release_holds']
        exact={k:proposal[k] for k in ('preview_hash','source_hash','remote_hash','operation_hash')}
        receipt=reviews.issue_review_receipt(s,c['settings'],CONFIG,intent_key=intent.key,user=USER,
            expected=exact,clock=lambda:lane['clock'][0],signing_key=KEY);s.commit()
        with pytest.raises(PlanningCenterError,match='live_release_evidence_unavailable'):
            reviews.authorize_frequency_execution(s,c['settings'],CONFIG,receipt_id=receipt['receipt_id'],
                user=USER,clock=lambda:lane['clock'][0],signing_key=KEY)
        intent_key=intent.key
    class NoRemote:
        def __getattr__(self,name):raise AssertionError('No native request permitted')
    with pytest.raises(PlanningCenterError,match='corrected_source_is_preview_only'):
        _supported(CONFIG,preview,preview.value['operations'][0])
    result=execute_frequency_intent(lane['factory'],NoRemote(),CONFIG,intent_key,
        source_reader=c['reader'],clock=lambda:lane['clock'][0],enabled=True)
    assert result.state=='held' and result.reason=='preflight_or_readback_not_verified'


def test_actual_held_capture_consumes_lineage_with_gets_only(correction,tmp_path):
    from app.core.planning_center_held_preview import capture_held_preview
    from app.integrations.planning_center import PCOClient
    from app.integrations.planning_center_availability import MembershipBinding
    from tests.test_planning_center_frequency_executor import Native
    c=correction;lane=c['lane'];c['provision']()
    with lane['factory']() as s:
        saved=s.scalar(select(PCOAvailabilityPreview))
        binding=json.loads(saved.document)['remote']['memberships'][0]['binding']
    path=tmp_path/'bindings';path.write_text(_json({'schema':1,'organization_id':'10',
        'volunteer_id':lane['volunteer_id'],'person_id':'70','memberships':[binding]}));path.chmod(0o600)
    settings=replace(c['settings'],pco_review_bindings_path=str(path))
    native=Native([MembershipBinding(**binding)],lane['factory'])
    native.rows['80']['attributes']['schedule_preference']='Twice a month'
    result=capture_held_preview(lane['factory'],settings,CONFIG,volunteer_id=lane['volunteer_id'],
        user=USER,clock=lambda:lane['clock'][0],
        client_factory=lambda config:PCOClient(config,transport=httpx.MockTransport(native.handle)))
    assert result['operations'][0]['state']=='noop' and result['execution_enabled'] is False
    assert 'audited_local_correction_preview_only' in result['release_holds']
    assert native.requests and all(method=='GET' for method,_ in native.requests)


def test_normal_producer_keeps_original_equality_even_if_feature_enabled(lane):
    settings=replace(lane['settings'],pco_correction_lineage_enabled=True)
    reader=CommittedAvailabilityReader(settings,CONFIG,volunteer_id=lane['volunteer_id'],
        profile_key=lane['profile_key'],source_id=lane['source_id'],clock=lambda:lane['clock'][0],allow_audited_correction=True)
    with lane['factory']() as s:
        assert 'correction_lineage' not in reader(s).value
        with pytest.raises(PlanningCenterError,match='not_required'):reader._checked_read(s,inspecting=True)


def test_lineage_is_independent_default_off_configuration(monkeypatch):
    from app.config import settings_from_env
    monkeypatch.delenv('PCO_CORRECTION_LINEAGE_ENABLED',raising=False)
    monkeypatch.delenv('PCO_CORRECTION_LINEAGE_KEY_PATH',raising=False)
    settings=settings_from_env()
    assert settings.pco_correction_lineage_enabled is False and settings.pco_correction_lineage_key_path==''
    monkeypatch.setenv('PCO_CORRECTION_LINEAGE_ENABLED','true')
    monkeypatch.setenv('PCO_CORRECTION_LINEAGE_KEY_PATH','/synthetic/private/correction-key')
    settings=settings_from_env()
    assert settings.pco_correction_lineage_enabled and settings.pco_correction_lineage_key_path.endswith('correction-key')


def test_selected_native_read_is_read_only_and_manifest_acceptance_is_exact(correction):
    c=correction;before=c['native_db'].read_bytes()
    c['provision'](apply=False)
    assert c['native_db'].read_bytes()==before
    manifest=deepcopy(c['manifest']);manifest['audit']['audit_sha256']='b'*64
    with c['lane']['factory']() as s:
        with pytest.raises(PlanningCenterError,match='accepted_manifest_digest_changed'):
            lineage.provision(s,c['settings'],CONFIG,manifest=manifest,
                accepted_manifest_sha256=hashlib.sha256(_json(c['manifest']).encode()).hexdigest(),user=USER,
                clock=lambda:c['lane']['clock'][0],native_observer=c['observe'],apply=True)


def test_check_only_cli_refuses_missing_database_without_creation(correction,tmp_path,monkeypatch,capsys):
    from scripts import pco_provision_correction_lineage as tool
    c=correction;missing=tmp_path/'mistyped.sqlite';manifest=tmp_path/'manifest'
    manifest.write_bytes(_json(c['manifest']).encode());manifest.chmod(0o600)
    monkeypatch.setattr(tool,'settings_from_env',lambda:replace(c['settings'],database_url='sqlite:///'+str(missing)))
    def no_auth(*args):raise AssertionError('Local target rejection must precede authentication')
    monkeypatch.setattr(tool,'authenticated_user',no_auth)
    assert tool.main(['--manifest',str(manifest),'--accepted-manifest-sha256',
        hashlib.sha256(manifest.read_bytes()).hexdigest(),'--mac-config',str(c['config'])])==1
    assert not missing.exists()
    assert json.loads(capsys.readouterr().out)['reason']=='correction_lineage_existing_file_sqlite_required'


def test_validated_database_deleted_before_connect_cannot_be_recreated(tmp_path):
    from scripts.pco_provision_correction_lineage import selected_engine
    from sqlalchemy.exc import OperationalError
    path=tmp_path/'existing.sqlite'
    with sqlite3.connect(path):pass
    engine=selected_engine('sqlite:///'+str(path));path.unlink()
    try:
        with pytest.raises(OperationalError):
            with engine.connect():pass
        assert not path.exists()
    finally:engine.dispose()


def test_private_cli_default_checks_existing_source_without_provision(correction,tmp_path,monkeypatch,capsys):
    from types import SimpleNamespace
    from scripts import pco_provision_correction_lineage as tool
    c=correction;manifest=tmp_path/'manifest'
    manifest.write_bytes(_json(c['manifest']).encode());manifest.chmod(0o600)
    monkeypatch.setattr(tool,'settings_from_env',lambda:replace(c['settings'],database_url=str(c['lane']['engine'].url)))
    monkeypatch.setattr(tool,'PCOConfig',SimpleNamespace(from_env=lambda:CONFIG))
    monkeypatch.setenv('PCO_LINEAGE_REVIEW_BEARER','synthetic-session')
    auth=[]
    def handle(request):
        auth.append(request.url.path)
        return httpx.Response(200,json=USER)
    monkeypatch.setattr(tool,'authenticated_user',lambda settings,bearer:
        authenticated_user(settings,bearer,transport=httpx.MockTransport(handle)))
    native_before=c['native_db'].read_bytes()
    # The private CLI uses wall time; the synthetic correction predates it.
    assert tool.main(['--manifest',str(manifest),'--accepted-manifest-sha256',
        hashlib.sha256(manifest.read_bytes()).hexdigest(),'--mac-config',str(c['config'])])==0
    result=json.loads(capsys.readouterr().out)
    assert result['applied'] is False and auth==['/auth/v1/user']
    assert c['native_db'].read_bytes()==native_before
    with c['lane']['factory']() as s:assert s.get(m.Policy,result['policy_key']) is None
