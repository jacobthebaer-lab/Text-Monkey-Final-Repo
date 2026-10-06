"""Real offline intake plus two independent stores prove scoped mirror safety."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core import profile_sync as sync
from app.db import models as m
from app.db.session import make_engine
from app.integrations.google_voice_demo import RECIPIENT_KEY
from app.integrations.google_voice_models import GoogleVoiceInboundReceipt
from app.integrations.google_voice_profile_sync import capture_google_profile, scoped_settings
from app.integrations.profile_models import ProfileOutbox
from tests.test_google_voice_self_reported_name import begin, EXPECTED
from tests.test_google_voice_signup import signup, inbound, PHONE
from tests.test_google_voice_demo import demo, dynamic_demo
from app.integrations.google_voice_signup import tick_signup


@pytest.fixture
def mirror(signup, tmp_path):
    path = tmp_path / 'scope.json'
    signup.state.settings = replace(signup.state.settings,
        google_voice_profile_sync_enabled=True, google_voice_profile_sync_scope_file=str(path))
    begin(signup)
    with signup.state.session_factory() as local:
        registration = local.get(m.Policy, RECIPIENT_KEY + PHONE).value
        path.write_text(json.dumps({'transport': 'google_voice', 'phones': [PHONE],
            'project_ref': 'abcdefghijklmnopqrst',
            'session_id': registration['session']['id'],
            'sender_fingerprint': registration['sender_fingerprint']}))
    engine = make_engine('sqlite://')
    m.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield signup, factory, path
    engine.dispose()


def valid(mirror):
    app, factory, path = mirror
    inbound(app, 'My name is Judge Example', 'correct-profile')
    tick_signup(app.state)
    return app, factory, path


def publish(app, factory, **kwargs):
    configured, _ = scoped_settings(app.state.settings)
    with app.state.session_factory() as local:
        return sync.publish_pending(local, factory, configured, identity_when_incomplete=True, **kwargs)


def test_partial_name_has_no_volunteer_no_outbox_no_remote_row_then_full_name_captures_once(mirror):
    app, factory, _ = mirror
    inbound(app, 'My name is Judge', 'partial-profile')
    tick_signup(app.state)
    with app.state.session_factory() as local:
        assert local.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
        assert local.scalars(select(ProfileOutbox)).all() == []
    assert publish(app, factory) == []
    with factory() as cloud:
        assert cloud.scalars(select(m.Volunteer)).all() == []
    valid(mirror)
    with app.state.session_factory() as local:
        rows = local.scalars(select(ProfileOutbox)).all()
        assert len(rows) == 1
        receipt = local.get(GoogleVoiceInboundReceipt, 'correct-profile')
        assert rows[0].payload['google_voice_provenance']['source_message_id'] == receipt.result['source_message_id']
    inbound(app, 'My name is Judge Example', 'correct-profile')
    tick_signup(app.state)
    with app.state.session_factory() as local:
        assert len(local.scalars(select(ProfileOutbox)).all()) == 1
    result = publish(app, factory)
    assert result[0]['detail'] == 'identity_synced_preferences_pending'
    assert publish(app, factory) == []
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.name == 'Judge Example' and person.sms_opt_in and person.status == 'inactive'
        assert person.preferences.get('onboarding_stage') != 'complete'


def test_actual_self_reported_name_supplies_scoped_supabase_payload_despite_legacy_names(mirror):
    app, factory, path = mirror
    scope = json.loads(path.read_text())
    scope['expected_name'] = EXPECTED  # Old private scope remains readable, never authenticates.
    path.write_text(json.dumps(scope))
    with app.state.session_factory() as local:
        row = local.get(m.Policy, RECIPIENT_KEY + PHONE)
        row.value = {**row.value, 'expected_name': EXPECTED}
        local.commit()
    inbound(app, 'Other Different', 'self-reported-profile')
    tick_signup(app.state)
    with app.state.session_factory() as local:
        outbox = local.scalar(select(ProfileOutbox))
        assert outbox.payload['profile']['name'] == 'Other Different'
        assert outbox.payload['profile']['sms_opt_in'] is True
        assert 'expected_name' not in outbox.payload['google_voice_provenance']
        receipt = local.get(GoogleVoiceInboundReceipt, 'self-reported-profile')
        assert outbox.payload['google_voice_provenance']['source_message_id'] == receipt.result['source_message_id']
        # Previously queued provenance included this obsolete admin restriction.
        outbox.payload = {**outbox.payload, 'google_voice_provenance': {
            **outbox.payload['google_voice_provenance'], 'expected_name': EXPECTED}}
        local.commit()
    assert publish(app, factory)[0]['detail'] == 'identity_synced_preferences_pending'
    assert publish(app, factory) == []
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.name == 'Other Different' and person.sms_opt_in
        assert person.preferences.get('onboarding_stage') != 'complete'
        assert cloud.scalars(select(m.Qualification)).all() == []


@pytest.mark.parametrize('change', ['original_body', 'invitation', 'session', 'sender', 'scope', 'stop', 'receipt', 'approval', 'claim'])
def test_modified_or_revoked_evidence_is_held_before_any_remote_write(mirror, change):
    app, factory, path = valid(mirror)
    with app.state.session_factory() as local:
        registration = local.get(m.Policy, RECIPIENT_KEY + PHONE)
        proof = registration.value['consent']
        if change == 'original_body':
            local.get(m.Message, proof['reply_message_id']).body = 'Other Example'
        elif change == 'invitation':
            local.get(m.Message, proof['disclosure_message_id']).body = 'Changed disclosure'
        elif change == 'session':
            registration.value = {**registration.value, 'session': {**registration.value['session'], 'id':'other'}}
        elif change == 'sender':
            registration.value = {**registration.value, 'sender_fingerprint': 'f' * 64}
        elif change == 'stop':
            local.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)).sms_opt_in = False
            local.add(m.Policy(key='sms_opt_out:' + PHONE, value={'value': True}))
        elif change == 'receipt':
            local.get(GoogleVoiceInboundReceipt, 'correct-profile').fingerprint = 'f' * 64
        elif change == 'approval':
            local.get(m.Approval, registration.value['invitation']['approval_id']).status = 'rejected'
        elif change == 'claim':
            from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
            local.get(GoogleVoiceDeliveryClaim, proof['disclosure_message_id']).idempotency_key = 'altered-key'
        elif change == 'scope':
            scope = json.loads(path.read_text())
            scope['project_ref'] = 'z' * 20
            path.write_text(json.dumps(scope))
        local.commit()
    result = publish(app, factory)
    assert result[0]['state'] == 'held'
    with factory() as cloud:
        assert cloud.scalars(select(m.Volunteer)).all() == []


def test_outage_retry_and_lost_ack_are_idempotent_and_preserve_unrelated_fields(mirror):
    app, factory, _ = valid(mirror)
    with factory() as cloud:
        cloud.add(m.Volunteer(name='Judge Example', phone=PHONE, sms_opt_in=True, status='active',
            is_coordinator=True, is_pastor=True, preferences={'unrelated': 'retain'}, created_at=app.state.google_voice_clock.now()))
        cloud.add(m.Volunteer(name='Unrelated Person', phone='+12025550999', sms_opt_in=False,
            status='inactive', preferences={'retain': True}, created_at=app.state.google_voice_clock.now()))
        cloud.flush()
        volunteer = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        cloud.add(m.Qualification(volunteer_id=volunteer.id, type='background_check', status='verified'))
        cloud.commit()
    def unavailable():
        raise RuntimeError('SECRET_MUST_NOT_APPEAR')
    assert publish(app, unavailable)[0]['state'] == 'failed'
    assert publish(app, factory)[0]['detail'] == 'identity_synced_preferences_pending'
    with app.state.session_factory() as local:
        row = local.scalar(select(ProfileOutbox))
        row.detail = ''  # Simulate a successful remote commit whose local acknowledgment was lost.
        local.commit()
    publish(app, factory)
    with factory() as cloud:
        people = cloud.scalars(select(m.Volunteer)).all()
        assert len(people) == 2
        person = next(p for p in people if p.phone == PHONE)
        assert person.preferences['unrelated'] == 'retain' and person.is_coordinator and person.is_pastor
        assert cloud.scalar(select(m.Qualification)).status == 'verified'
        assert next(p for p in people if p.phone != PHONE).preferences == {'retain': True}


@pytest.mark.parametrize('existing', [False, True])
def test_actual_stop_updates_only_existing_profile_and_precedes_stale_identity(mirror, existing):
    app, factory, _ = valid(mirror)
    if existing:
        publish(app, factory)
    inbound(app, 'STOP', 'actual-stop')
    tick_signup(app.state)
    results = publish(app, factory, limit=20)
    with app.state.session_factory() as local:
        stop = local.scalar(select(ProfileOutbox).where(ProfileOutbox.source_guid == 'actual-stop'))
        assert stop is not None
        assert stop.state == ('synced' if existing else 'held')
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        if existing:
            assert person is not None and not person.sms_opt_in
        else:
            assert person is None


def test_default_off_and_out_of_scope_do_not_capture(mirror):
    app, _, path = valid(mirror)
    with app.state.session_factory() as local:
        assert capture_google_profile(local, replace(app.state.settings, google_voice_profile_sync_enabled=False),
            phone=PHONE, guid='correct-profile', route='signup_complete', before=None,
            effective_at=app.state.google_voice_clock.now()) is None
    scope = json.loads(path.read_text())
    scope['phones'] = ['+12025550998']
    path.write_text(json.dumps(scope))
    with app.state.session_factory() as local:
        assert capture_google_profile(local, app.state.settings, phone=PHONE,
            guid='correct-profile', route='signup_complete', before=None,
            effective_at=app.state.google_voice_clock.now()) is None


def test_actual_preferences_complete_and_map_without_granting_qualifications(mirror):
    app, factory, _ = valid(mirror)
    with factory() as cloud:
        cloud.add(m.Role(name='Greeter', ministry='Welcome', criticality='standard', fill_policy='auto'))
        cloud.commit()
    publish(app, factory)
    for body, guid in [('Greeter', 'role-profile'), ('Sundays at9am twice a month', 'availability-profile')]:
        inbound(app, body, guid)
        tick_signup(app.state)
    publish(app, factory, limit=20)
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.preferences['onboarding_stage'] == 'complete'
        assert person.preferences['interested_roles'] == ['Greeter']
        assert person.preferences['availability_weekdays'] == [6]
        assert person.preferences['preferred_services'] == ['sun_9']
        assert person.preferences['max_per_month'] == 2
        assert cloud.scalars(select(m.Qualification)).all() == []


def test_incomplete_availability_never_completes_and_stop_does_not_wait_for_it(mirror):
    app, factory, _ = valid(mirror)
    with app.state.session_factory() as local:
        person = local.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        person.preferences = {**person.preferences, 'onboarding_availability_draft': {'weekdays': [6]}}
        # Mirror a new revision tied to its still-valid original actual name input.
        receipt = local.get(GoogleVoiceInboundReceipt, 'correct-profile')
        capture_google_profile(local, app.state.settings, phone=PHONE, guid='correct-profile',
            route=receipt.result['intent'], before=None, effective_at=app.state.google_voice_clock.now())
        local.commit()
    publish(app, factory, limit=20)
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.status == 'inactive' and person.preferences.get('onboarding_stage') != 'complete'
    inbound(app, 'STOP', 'draft-stop')
    tick_signup(app.state)
    publish(app, factory, limit=20)
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert not person.sms_opt_in and person.status == 'inactive'


def test_malformed_scope_cannot_roll_back_actual_stop(mirror):
    app, _, path = valid(mirror)
    path.write_text('{invalid')
    inbound(app, 'STOP', 'bad-scope-stop')
    tick_signup(app.state)
    with app.state.session_factory() as local:
        assert not local.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)).sms_opt_in
        assert local.get(m.Policy, 'sms_opt_out:' + PHONE)
        assert local.get(GoogleVoiceInboundReceipt, 'bad-scope-stop').result['intent'] == 'stop'
        assert local.scalar(select(ProfileOutbox).where(ProfileOutbox.source_guid == 'bad-scope-stop')) is None


def test_stop_has_priority_over_older_unpublished_identity_with_default_single_row_limit(mirror):
    app, factory, _ = valid(mirror)
    inbound(app, 'STOP', 'priority-stop')
    tick_signup(app.state)
    result = publish(app, factory)
    with app.state.session_factory() as local:
        row = local.scalar(select(ProfileOutbox).where(ProfileOutbox.source_guid == 'priority-stop'))
        assert result[0]['key'] == row.key and row.detail == 'stop_cloud_profile_missing'
    with factory() as cloud:
        assert cloud.scalars(select(m.Volunteer)).all() == []


@pytest.mark.parametrize('invalid', ['deleted_role', 'renamed_role', 'volunteer_reference', 'availability'])
def test_actual_stop_mirrors_revocation_despite_unrelated_invalid_profile(mirror, invalid):
    app, factory, _ = valid(mirror)
    publish(app, factory)
    with app.state.session_factory() as local:
        local.info['record_authorized'] = True  # Synthetic coordinator edits, not sender permission.
        person = local.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        if invalid in {'deleted_role', 'renamed_role'}:
            role = local.scalar(select(m.Role).where(m.Role.name == 'Greeter'))
            person.preferences = {**person.preferences, 'interested_roles': ['Greeter']}
            if invalid == 'deleted_role':
                local.delete(role)
            else:
                role.name = 'Renamed Greeter'
        elif invalid == 'volunteer_reference':
            person.preferences = {**person.preferences, 'serves_with_volunteer_id': 999999}
        else:
            person.preferences = {**person.preferences, 'onboarding_availability_draft': {'weekdays': ['invalid']}}
        local.commit()
        assert sync.safe_snapshot(local, PHONE).get('_held')
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        person.preferences = {**person.preferences, 'unrelated': 'retained'}
        before = {key: value for key, value in person.preferences.items() if key != sync.MARKER}
        cloud.commit()
    inbound(app, 'STOP', 'invalid-profile-stop')
    tick_signup(app.state)
    with app.state.session_factory() as local:
        row = local.scalar(select(ProfileOutbox).where(ProfileOutbox.source_guid == 'invalid-profile-stop'))
        assert row.payload['profile'] == {'phone': PHONE, 'sms_opt_in': False, 'preferences': {}, 'availability': []}
        assert row.state == 'pending'
    assert publish(app, factory)[0]['state'] == 'synced'
    with factory() as cloud:
        person = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert not person.sms_opt_in
        assert {key: value for key, value in person.preferences.items() if key != sync.MARKER} == before


@pytest.mark.parametrize('change', ['name_proof', 'consent_resumed', 'stop_input'])
def test_consent_only_stop_still_requires_original_proof_and_current_revocation(mirror, change):
    app, factory, _ = valid(mirror)
    publish(app, factory)
    inbound(app, 'STOP', 'proof-stop')
    tick_signup(app.state)
    with app.state.session_factory() as local:
        local.info['record_authorized'] = True
        record = local.get(m.Policy, RECIPIENT_KEY + PHONE)
        if change == 'name_proof':
            local.get(m.Message, record.value['consent']['reply_message_id']).body = 'Other Example'
        elif change == 'consent_resumed':
            local.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)).sms_opt_in = True
        else:
            receipt = local.get(GoogleVoiceInboundReceipt, 'proof-stop')
            local.get(m.Message, receipt.result['source_message_id']).body = 'START'
        local.commit()
    assert publish(app, factory)[0]['state'] == 'held'
    with factory() as cloud:
        assert cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)).sms_opt_in
