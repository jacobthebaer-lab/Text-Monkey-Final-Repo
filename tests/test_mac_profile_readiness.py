"""Lossless isolated Mac profile transition; no native or remote operations."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

import pytest
from sqlalchemy import select

from app.core import profile_sync as sync
from app.core.consent_controls import prior_disclosed_consent
from app.core.signup_copy import WELCOME
from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt
from tests.test_profile_sync import stores, settings, PHONE, queue


def test_mac_identity_to_full_keeps_original_source_and_verified_constraints_without_qualifying_roles(stores,settings,clock):
    local,volunteer,factory=stores
    sid='a'*32
    local.add(m.Message(direction='out',phone=PHONE,body=WELCOME,purpose='signup_reply',kind='ai',
        status='sent',created_at=clock.now()-timedelta(seconds=1)))
    local.add(m.Message(direction='in',phone=PHONE,body=volunteer.name,purpose='test:'+sid,kind='mac_test_in',
        status='received',created_at=clock.now()))
    volunteer.preferences={**volunteer.preferences,'consent_source':'sms_name_reply_to_exact_invitation',
        'onboarding_stage':'availability'}
    name_guid='synthetic-accepted-name'
    local.add(MacInboundReceipt(guid=name_guid,
        fingerprint=hashlib.sha256((PHONE+'\0iMessage\0'+sid+'\0'+volunteer.name).encode()).hexdigest(),
        result={'intent':'onboarding_interests','session_id':sid}))
    local.flush()
    assert prior_disclosed_consent(local,volunteer)
    first=sync.capture(local,settings,phone=PHONE,guid=name_guid,route='onboarding_interests',before=None,effective_at=clock.now())
    local.commit();sync.publish_pending(local,factory,settings,identity_only=True)
    cloud_id=first.cloud_id;source_id=first.source_id
    with factory() as cloud:
        original=cloud.get(m.Volunteer,cloud_id)
        assert original.status=='inactive' and original.sms_opt_in
        assert original.preferences['consent_source']=='sms_name_reply_to_exact_invitation'
        original.preferences={**original.preferences,'admin_text_owner':'existing-cloud-owner','paused_roles':['Usher']}
        cloud.add_all([
            m.Role(id=101,name='greeter',ministry='Welcome',required_qualifications=[],criticality='standard',fill_policy='auto'),
            m.Role(id=113,name='Child Care',ministry='Kids',required_qualifications=['background_check','child_safety_training'],criticality='standard',fill_policy='auto'),
            m.Role(id=117,name='Production',ministry='Production',required_qualifications=['sound_training'],criticality='standard',fill_policy='auto')])
        cloud.commit()
    local.add_all([
        m.Role(id=11,name='Greeter',ministry='Welcome',required_qualifications=[],criticality='standard',fill_policy='auto'),
        m.Role(id=13,name='Child Care',ministry='Kids',required_qualifications=['background_check','child_safety_training'],criticality='standard',fill_policy='auto'),
        m.Role(id=17,name='Production',ministry='Production',required_qualifications=['sound_training'],criticality='standard',fill_policy='auto')])
    local.flush()
    before=sync.snapshot(local,PHONE)
    windows=[{'weekday':6,'role_ids':[11],'role_label':'Greeter','any_role':False,
        'start_time':'08:00','end_time':'10:00','all_day':False,'event_context':None}]
    draft={'availability_known':False,'frequency_known':True,'max_per_month':None,'weekdays':[],
        'recurring_windows':windows,'role_frequency_caps':[{'role_id':11,'role_name':'Greeter','max_per_month':2}]}
    volunteer.preferences={**volunteer.preferences,'interested_roles':['Greeter','Production','Child Care'],
        'preferred_ministry':'Kids, Production, Welcome','any_role':False,'onboarding_availability_draft':draft}
    clock.advance(timedelta(seconds=1))
    partial=sync.capture(local,settings,phone=PHONE,guid='synthetic-partial-availability',route='onboarding_clarify',before=before,effective_at=clock.now())
    local.commit();sync.publish_pending(local,factory,settings,identity_only=True)
    sync.publish_pending(local,factory,settings,limit=20)
    assert partial.state=='held' and partial.detail=='incomplete_availability_draft'
    assert partial.payload['profile']['availability_draft']['availability_known'] is False
    with factory() as cloud:
        assert cloud.get(m.Volunteer,cloud_id).status=='inactive'
        assert 'preferred_ministry' not in cloud.get(m.Volunteer,cloud_id).preferences
    before=sync.snapshot(local,PHONE)
    volunteer.preferences={k:v for k,v in volunteer.preferences.items() if k!='onboarding_availability_draft'}
    volunteer.preferences={**volunteer.preferences,'onboarding_stage':'complete','availability_frequency_known':True,
        'max_per_month':None,'recurring_windows':windows,
        'role_frequency_caps':[{'role_id':11,'role_name':'Greeter','max_per_month':2}]}
    exclusions=['2026-12-24','2026-12-25']
    local.add(m.Availability(volunteer_id=volunteer.id,month='2026-12',available_dates=[],unavailable_dates=exclusions,
        raw_reply='synthetic private schedule answer',parsed_at=clock.now()))
    clock.advance(timedelta(seconds=1))
    guid='synthetic-original-complete-reply';body='Synthetic validated complete availability reply'
    local.add(m.Message(direction='in',phone=PHONE,body=body,purpose='test:'+sid,kind='mac_test_in',
        status='received',created_at=clock.now()))
    local.add(MacInboundReceipt(guid=guid,fingerprint=hashlib.sha256((PHONE+'\0iMessage\0'+sid+'\0'+body).encode()).hexdigest(),
        result={'intent':'onboarding_complete','session_id':sid}))
    final=sync.capture(local,settings,phone=PHONE,guid=guid,route='onboarding_complete',before=before,effective_at=clock.now())
    local.commit()
    configured=replace(settings,profile_sync_role_map=json.dumps({'Greeter':'greeter'}))
    sync.publish_pending(local,factory,configured,limit=20)
    assert final.state=='synced' and final.source_id==source_id and final.source_guid==guid and final.cloud_id==cloud_id
    assert local.get(m.Policy,'profile_sync_source').value['id']==source_id
    with factory() as cloud:
        person=cloud.get(m.Volunteer,cloud_id);prefs=person.preferences
        assert len(cloud.scalars(select(m.Volunteer)).all())==1
        assert person.status=='active' and person.sms_opt_in and prefs['onboarding_stage']=='complete'
        assert prefs['preferred_ministry']=='Kids, Production, Welcome'
        assert prefs['interested_roles']==['greeter','Production','Child Care']
        assert prefs['role_frequency_caps']==[{'role_id':101,'role_name':'greeter','max_per_month':2}]
        assert prefs['max_per_month'] is None and prefs['recurring_windows']==[{**windows[0],'role_ids':[101]}]
        assert prefs['admin_text_owner']=='existing-cloud-owner' and prefs['paused_roles']==['Usher']
        assert not person.is_coordinator and not person.is_pastor
        assert cloud.scalar(select(m.Qualification)) is None
        saved=cloud.scalar(select(m.Availability))
        assert saved.unavailable_dates==exclusions and saved.raw_reply is None
    final.state='pending';local.commit();sync.publish_pending(local,factory,configured)
    assert final.detail=='already_applied'
    with factory() as cloud:
        assert len(cloud.scalars(select(m.Volunteer)).all())==1 and len(cloud.scalars(select(m.Availability)).all())==1


@pytest.mark.parametrize('value',[None,{},[],False,'','x'*501,'Kids\0Production'])
def test_invalid_ministry_preference_never_publishes_or_disappears_silently(stores,settings,clock,value):
    local,volunteer,factory=stores
    volunteer.preferences={**volunteer.preferences,'preferred_ministry':value};local.commit()
    row=queue(stores,settings,clock)
    assert row.state=='held' and row.payload['profile'] is None
    assert volunteer.preferences['preferred_ministry']==value
    assert sync.publish_pending(local,factory,settings)==[]
    with factory() as cloud:assert cloud.scalar(select(m.Volunteer)) is None
