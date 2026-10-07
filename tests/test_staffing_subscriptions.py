"""Explicit subscriptions, frozen reviews and fake native boundaries, no delivery."""
from datetime import timedelta
import hashlib
import json

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core import confirmations, notifications, staffing_subscriptions as staffing
from app.db import models as m
from tests.test_pre_event_updates import SyntheticGloo
from tests.test_admin_setup import setup_client, save, OWNER_B
from tests.test_admin_text_settings import enable
from tests.test_demo_acceptance_review import acceptance_app, pull, BRIDGE_TOKEN


def selected(*values):
    return {staffing.PREFERENCE: {'mode': 'selected', 'ministries': list(values)}}


def notice(session):
    return session.scalar(select(m.Notification).where(m.Notification.key.startswith('staffing:')))


def compose(session, clock, provider, make_volunteer, make_shift, *, exact=True):
    session.info['record_authorized'] = True  # Synthetic setup only; SMS still requires exact review.
    admin = make_volunteer(coordinator=True)
    shift = make_shift(starts=clock.now()+timedelta(days=1))
    shift.role.ministry = 'Kids'
    session.info[confirmations.MODE_KEY] = exact
    ctx = FillContext(session, clock, provider, SyntheticGloo())
    notifications.queue_staffing(ctx, shift.event)
    clock.advance(timedelta(minutes=5))
    notifications.flush_due(ctx)
    row = notice(session)
    review = session.get(m.Approval, row.detail.get('approval_id')) if exact else None
    return ctx, admin, shift, row, review


def test_defaults_selected_mixed_event_and_interest_is_not_authority(session, clock, provider, make_volunteer, make_shift):
    whole = make_volunteer(coordinator=True, prefs={'preferred_ministry': 'Unrelated'})
    kids = make_volunteer(coordinator=True, prefs=selected('Kids'))
    production = make_volunteer(coordinator=True, prefs=selected('Production'))
    make_volunteer(prefs=selected('Kids'))  # Subscription never grants admin authority.
    make_volunteer(coordinator=True, opt_in=False, prefs=selected('Kids'))
    make_volunteer(coordinator=True, status='inactive', prefs=selected('Kids'))
    stopped = make_volunteer(coordinator=True)
    session.add(m.Policy(key='sms_opt_out:'+stopped.phone, value={'value': True}))
    shift = make_shift('Kids role'); shift.role.ministry = 'Kids'
    other = make_shift('Production role'); other.role.ministry = 'Production'
    ctx = FillContext(session, clock, provider, SyntheticGloo())
    notifications.queue_staffing(ctx, shift.event)
    rows = session.scalars(select(m.Notification)).all()
    assert {row.volunteer_id for row in rows} == {whole.id, kids.id}
    other.event_id = shift.event_id
    session.flush()
    notifications.queue_staffing(ctx, shift.event)
    rows = session.scalars(select(m.Notification)).all()
    assert len(rows) == 3 and {row.volunteer_id for row in rows} == {whole.id, kids.id, production.id}


def test_recipe_without_shift_routes_only_positive_requirements(session, make_volunteer, make_shift):
    shift = make_shift(); shift.role.ministry = 'Kids'
    event_type = m.EventType(name='Sunday', title_patterns=[])
    session.add(event_type); session.flush()
    shift.event.event_type_id = event_type.id
    recipe = m.RoleRecipe(event_type_id=event_type.id, role_id=shift.role_id, count=1)
    session.add(recipe); session.delete(shift); session.flush()
    admin = make_volunteer(coordinator=True, prefs=selected('Kids'))
    event = session.scalar(select(m.Event).where(m.Event.event_type_id == event_type.id))
    assert staffing.matches(session, event, admin)
    recipe.count = 0; session.flush()
    assert not staffing.matches(session, event, admin)


@pytest.mark.parametrize('value', [None, {}, {'mode': [], 'ministries': []},
    {'mode': 'selected', 'ministries': []}, {'mode': 'all', 'ministries': ['Kids']},
    {'mode': 'selected', 'ministries': ['Unknown']}, {'mode': 'selected', 'ministries': ['Ki\nds']},
    {'mode': 'selected', 'ministries': [None]}, {'mode': 'selected', 'ministries': 'Kids'}])
def test_bad_explicit_scope_is_rejected(value):
    with pytest.raises(ValueError):
        staffing.validate_scope(value, ['Kids'])


@pytest.mark.parametrize('value', [None, {'mode': 'selected', 'ministries': ['Unknown']}])
def test_malformed_saved_preference_never_falls_back_to_all(session, make_volunteer, make_shift, value):
    admin = make_volunteer(coordinator=True, prefs={staffing.PREFERENCE: value})
    shift = make_shift(); shift.role.ministry = 'Kids'
    assert not staffing.matches(session, shift.event, admin)


@pytest.mark.parametrize('change', ['scope', 'ministry', 'recipe', 'event', 'owner', 'consent', 'authority', 'delete'])
def test_exact_review_holds_source_drift_without_rewriting_hash(session, clock, provider, make_volunteer, make_shift, change):
    ctx, admin, shift, row, review = compose(session, clock, provider, make_volunteer, make_shift)
    payload = dict(review.payload)
    session.info['record_authorized'] = True
    assert confirmations.valid(review, clock.now()) and payload['staffing_source']['facts']['subscription']['mode'] == 'all'
    if change == 'scope': admin.preferences = selected('Kids')
    elif change == 'ministry': shift.role.ministry = 'Production'
    elif change == 'recipe':
        kind = m.EventType(name='Added recipe', title_patterns=[]); session.add(kind); session.flush()
        shift.event.event_type_id = kind.id
        session.add(m.RoleRecipe(event_type_id=kind.id, role_id=shift.role_id, count=2))
    elif change == 'event': shift.event.title = 'Changed service'
    elif change == 'owner': admin.preferences = {'admin_text_owner': OWNER_B}
    elif change == 'consent': admin.sms_opt_in = False
    elif change == 'authority': admin.is_coordinator = False
    else: session.delete(admin)
    session.flush()
    confirmations.decide(session, ctx.gate, review, approve=True, actor='fixture@example.test',
                         expected=payload['content_hash'], now=clock.now())
    assert not provider.sent and not session.scalar(select(m.Message))
    assert review.status == 'expired' and review.payload == payload


def test_pending_review_dedupes_and_current_exact_review_sends(session, clock, provider, make_volunteer, make_shift):
    ctx, admin, shift, row, review = compose(session, clock, provider, make_volunteer, make_shift)
    body, digest = review.payload['body'], review.payload['content_hash']
    notifications.queue_staffing(ctx, shift.event)
    clock.advance(timedelta(minutes=5)); notifications.flush_due(ctx)
    assert len(session.scalars(select(m.Approval)).all()) == 1
    confirmations.decide(session, ctx.gate, review, approve=True, actor='fixture@example.test', expected=digest, now=clock.now())
    assert len(provider.sent) == 1 and provider.sent[0].to == admin.phone and provider.sent[0].body == body
    message = session.get(m.Message, review.payload['message_id'])
    assert staffing.native_problem(session, message, clock.now(), review) is None
    assert review.payload['content_hash'] == digest


def test_per_message_proof_survives_digest_reuse_and_blocks_changed_source(session, clock, provider, make_volunteer, make_shift, assign):
    ctx, admin, shift, row, _ = compose(session, clock, provider, make_volunteer, make_shift, exact=False)
    message = session.get(m.Message, row.message_id)
    binding = dict(row.detail['staffing_source'])
    assert staffing.native_problem(session, message, clock.now()) is None
    notifications.queue_staffing(ctx, shift.event)
    assert row.state == 'pending' and staffing.native_problem(session, message, clock.now()) is None
    assign(make_volunteer(), shift, status='confirmed')
    clock.advance(timedelta(minutes=15)); notifications.flush_due(ctx)
    assert len(provider.sent) == 2 and row.detail['staffing_source'] != binding
    current = session.get(m.Message, row.message_id)
    assert staffing.native_problem(session, message, clock.now())
    assert row.state == 'sent'  # Holding an old receipt does not block a newer digest.
    assert staffing.native_problem(session, current, clock.now()) is None


def test_source_change_during_gloo_holds_before_review(session, clock, provider, make_volunteer, make_shift):
    session.info['record_authorized'] = True
    admin = make_volunteer(coordinator=True)
    shift = make_shift(); shift.role.ministry = 'Kids'
    class ChangedGloo(SyntheticGloo):
        def create_response(self, **kwargs):
            output = super().create_response(**kwargs)
            admin.preferences = selected('Kids'); session.flush()
            return output
    ctx = FillContext(session, clock, provider, ChangedGloo())
    session.info[confirmations.MODE_KEY] = True
    notifications.queue_staffing(ctx, shift.event); clock.advance(timedelta(minutes=5)); notifications.flush_due(ctx)
    assert notice(session).state == 'blocked_policy'
    assert not provider.sent and not session.scalar(select(m.Approval)) and not session.scalar(select(m.Message))


def test_legacy_hash_continuity_and_restricted_scope_hold(session, clock, make_volunteer, make_shift):
    admin = make_volunteer(coordinator=True); shift = make_shift(); shift.role.ministry = 'Kids'
    payload = {'action': 'send_text', 'volunteer_id': admin.id, 'phone': admin.phone,
               'body': 'Still needs cover: Sunday. Kids (1).', 'purpose': 'coordinator_notify'}
    old_keys = tuple(k for k in confirmations.CONTENT_KEYS if k != 'staffing_source')
    old_hash = hashlib.sha256(json.dumps({k: payload[k] for k in old_keys if k in payload}, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    assert confirmations.digest(payload) == old_hash
    review = confirmations.stage(session, clock.now(), payload)
    saved = dict(review.payload)
    assert staffing.approval_problem(session, review, clock.now()) is None
    admin.preferences = selected('Kids')
    assert staffing.approval_problem(session, review, clock.now())
    assert review.payload == saved
    assert staffing.legacy_problem(session, admin.id, 'coordinator_notify', 'Pre-event update: Still needs cover: Sunday.') is None


def test_missing_per_message_receipt_holds_even_after_digest_reuse(session, clock, provider, make_volunteer, make_shift):
    ctx, _, shift, row, _ = compose(session, clock, provider, make_volunteer, make_shift, exact=False)
    message = session.get(m.Message, row.message_id)
    key = row.detail['staffing_source']['receipt_key']
    notifications.queue_staffing(ctx, shift.event)
    session.delete(session.get(m.Policy, key)); session.flush()
    assert 'missing' in staffing.native_problem(session, message, clock.now()).lower()


def test_new_digest_never_rewrites_a_pending_legacy_review(session, clock, provider, make_volunteer, make_shift):
    ctx, _, shift, row, original = compose(session, clock, provider, make_volunteer, make_shift)
    legacy = {k: v for k, v in original.payload.items() if k != 'staffing_source'}
    legacy['content_hash'] = confirmations.digest(legacy)
    original.payload = legacy; session.flush()
    notifications.queue_staffing(ctx, shift.event); clock.advance(timedelta(minutes=5)); notifications.flush_due(ctx)
    current = session.get(m.Approval, row.detail['approval_id'])
    assert current.id != original.id and original.status == 'expired' and original.payload == legacy
    assert current.payload['staffing_source'] and confirmations.valid(current, clock.now())


def test_legacy_restricted_review_blocks_at_actual_approval_without_copy_rewrite(session, clock, provider, make_volunteer, make_shift):
    admin = make_volunteer(coordinator=True, prefs=selected('Kids'))
    shift = make_shift(); shift.role.ministry = 'Kids'; session.flush()
    session.info[confirmations.MODE_KEY] = True
    review = confirmations.stage(session, clock.now(), {'action': 'send_text', 'volunteer_id': admin.id,
        'phone': admin.phone, 'kind': 'ai', 'purpose': 'coordinator_notify', 'body': 'Fully staffed: Sunday. All roles covered.'})
    original = dict(review.payload)
    confirmations.decide(session, FillContext(session, clock, provider, SyntheticGloo()).gate, review,
        approve=True, actor='fixture@example.test', expected=original['content_hash'], now=clock.now())
    assert review.status == 'expired' and review.payload == original and not provider.sent


def setup_scope(client, app):
    save(client, complete=True); enable(client)
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        session.add_all([m.Role(name='Check-in', ministry='Kids', criticality='standard', fill_policy='auto'), m.Role(name='Sound', ministry='Production', criticality='standard', fill_policy='auto')]); session.commit()
    return client.get('/api/setup/admin-texts').json()


def save_scope(client, status, scope):
    return client.post('/api/setup/admin-texts/staffing-scope', json={
        'recipient_id': status['staffing_scope_recipient_id'], 'record_hash': status['staffing_scope_record_hash'], 'scope': scope})


def test_owner_setting_normalizes_persists_and_reset_sends_nothing(setup_client):
    client, app, _ = setup_client
    status = setup_scope(client, app)
    assert status['staffing_scope'] == {'mode': 'all', 'ministries': []}
    assert status['staffing_scope_configurable'] and status['staffing_ministries'] == ['Kids', 'Production']
    result = save_scope(client, status, {'mode': 'selected', 'ministries': [' Kids ', 'Kids']})
    assert result.status_code == 200 and result.json()['staffing_scope'] == {'mode': 'selected', 'ministries': ['Kids']}
    assert save_scope(client, status, {'mode': 'all', 'ministries': []}).status_code == 409  # stale exact record
    assert save_scope(client, result.json(), {'mode': 'all', 'ministries': []}).status_code == 200
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        assert len(session.scalars(select(m.Volunteer)).all()) == 1
        assert not session.scalar(select(m.Message)) and not session.scalar(select(m.Approval))
        assert len(session.scalars(select(m.Role)).all()) == 2


@pytest.mark.parametrize('change', ['owner', 'consent', 'authority', 'inactive', 'stop', 'ministry', 'malformed'])
def test_api_rejects_stale_foreign_or_ineligible_record_and_unknown_choices(setup_client, change):
    client, app, user = setup_client
    status = setup_scope(client, app)
    if change == 'owner': user['id'] = OWNER_B
    elif change in ('ministry', 'malformed'): pass
    else:
        with app.state.session_factory() as session:
            session.info['record_authorized'] = True
            person = session.get(m.Volunteer, status['staffing_scope_recipient_id'])
            if change == 'consent': person.sms_opt_in = False
            elif change == 'authority': person.is_coordinator = False
            elif change == 'inactive': person.status = 'inactive'
            else: session.add(m.Policy(key='sms_opt_out:'+person.phone, value={'value': True}))
            session.commit()
    selection = {'mode': 'selected', 'ministries': ['Unknown' if change == 'ministry' else 'Kids']}
    if change == 'malformed': selection['mode'] = []
    assert save_scope(client, status, selection).status_code == (422 if change in ('ministry', 'malformed') else 409)


def test_invalid_stored_scope_is_reported_held_and_can_be_corrected(setup_client):
    client, app, _ = setup_client
    status = setup_scope(client, app)
    save_scope(client, status, {'mode': 'selected', 'ministries': ['Kids']})
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        role = session.scalar(select(m.Role).where(m.Role.ministry == 'Kids')); role.ministry = 'Renamed'; session.commit()
    status = client.get('/api/setup/admin-texts').json()
    assert status['staffing_scope'] is None and status['staffing_scope_error'] and status['staffing_scope_configurable']
    assert save_scope(client, status, {'mode': 'all', 'ministries': []}).status_code == 200


@pytest.mark.parametrize('boundary', ['pull', 'verify'])
@pytest.mark.parametrize('change', ['scope', 'ministry', 'owner', 'removed'])
def test_actual_fake_mac_claim_and_preflight_hold_staffing_drift(acceptance_app, boundary, change):
    client, app, gloo, clock = acceptance_app
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        person = session.scalar(select(m.Volunteer))
        role = m.Role(name='Check-in', ministry='Kids', criticality='standard', fill_policy='auto'); session.add(role); session.flush()
        event = m.Event(title='Sunday', starts_at=clock.now()+timedelta(days=1), ends_at=clock.now()+timedelta(days=1, hours=1), status='scheduled')
        session.add(event); session.flush(); session.add(m.Shift(event_id=event.id, role_id=role.id, slot_index=0)); session.flush()
        ctx = FillContext(session, clock, app.state.provider, gloo)
        notifications.queue_staffing(ctx, event); clock.advance(timedelta(minutes=5)); notifications.flush_due(ctx)
        row = notice(session); assert row.state == 'sent'
        ident, person_id, role_id = row.message_id, person.id, role.id
        session.commit()
    claim = pull(client).json()['messages'][0] if boundary == 'verify' else None
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        person = session.get(m.Volunteer, person_id)
        if change == 'scope': person.preferences = {**person.preferences, **selected('Kids')}
        elif change == 'ministry': session.get(m.Role, role_id).ministry = 'Production'
        elif change == 'owner': person.preferences = {**person.preferences, 'admin_text_owner': OWNER_B}
        else: session.delete(person)
        session.commit()
    if boundary == 'pull': assert not pull(client).json()['messages']
    else:
        assert client.post(f'/mac/outbound/{ident}/verify', json={'token': claim['token']},
            headers={'Authorization': 'Bearer '+BRIDGE_TOKEN}).status_code == 409
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        message = session.get(m.Message, ident)
        assert message.status in {'superseded', 'blocked_policy', 'blocked_confirmation'}
        assert message.status != 'submitted'
