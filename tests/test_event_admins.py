"""Shared church events, explicit saved recipients, real API and fake transport."""
from datetime import timedelta
from dataclasses import replace
import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core import confirmations, event_admins, notifications, staffing_subscriptions
from app.db import models as m
from app.web.texty import admin as auth_admin
from tests.test_admin_setup import setup_client, save, OWNER_A, OWNER_B, DETAILS
from tests.test_admin_text_settings import enable
from tests.test_pre_event_updates import SyntheticGloo
from tests.test_demo_acceptance_review import acceptance_app, pull, BRIDGE_TOKEN


def seed_event(app, *, phone='+12025550198', existing_coordinator=False):
    with app.state.session_factory() as s:
        s.info['record_authorized'] = True
        now=app.state.clock.now()
        person=m.Volunteer(name='Casey Event Admin',phone=phone,is_coordinator=existing_coordinator,
            sms_opt_in=True,status='active',preferences={'admin_staffing_scope':{'mode':'selected','ministries':['Kids']}} if existing_coordinator else {},created_at=now)
        role=m.Role(name='Check-in',ministry='Kids',criticality='standard',fill_policy='auto')
        s.add_all([person,role]);s.flush()
        for i in range(2):
            event=m.Event(title=f'Sunday event {i}',starts_at=now+timedelta(hours=2+i),ends_at=now+timedelta(hours=3+i),status='scheduled')
            s.add(event);s.flush();s.add(m.Shift(event_id=event.id,role_id=role.id,slot_index=0))
        s.commit()
        return person.id


def enroll(client):
    reviewed=client.post('/api/setup/admin-texts/review',json={'phone':'2025550198'})
    assert reviewed.status_code==200
    proof=reviewed.json()
    data={key:proof[key] for key in ('review_id','record_hash','primary_hash')}
    data.update(phone=proof['recipient']['phone'],operator_consent=True,consent=False)
    result=client.post('/api/setup/admin-texts/event-recipient',json=data)
    assert result.status_code==200,result.text
    return proof,data,result.json()


def status(client):
    response=client.get('/api/setup/event-admins');assert response.status_code==200
    return response.json()


def select_event(client, state, event_index, ids, mode='selected'):
    event=state['events'][event_index]
    admins={p['id']:p for p in state['admins']}
    return client.post(f"/api/setup/event-admins/{event['id']}",json={'event_hash':event['event_hash'],
        'mode':mode,'recipients':[{'id':i,'record_hash':admins[i]['record_hash']} for i in ids]})


@pytest.mark.parametrize('existing_coordinator',[False,True])
def test_save_additional_admin_keeps_primary_phone_consent_and_staffing_permissions(setup_client,existing_coordinator):
    client,app,_=setup_client;save(client,complete=True);enable(client)
    primary=client.get('/api/setup/admin-texts').json()
    person_id=seed_event(app,existing_coordinator=existing_coordinator)
    proof,data,result=enroll(client)
    assert result['phone']==primary['phone'] and result['enabled']
    assert client.post('/api/setup/admin-texts/event-recipient',json=data).status_code==409
    state=status(client)
    assert {p['id'] for p in state['admins']}=={person_id,primary['staffing_scope_recipient_id']}
    assert state['default_admins']==[{'id':primary['staffing_scope_recipient_id'],'name':primary['recipient_name']}]
    with app.state.session_factory() as s:
        person=s.get(m.Volunteer,person_id);current=s.get(m.Volunteer,primary['staffing_scope_recipient_id'])
        assert person.is_coordinator == existing_coordinator
        assert current.status=='active' and current.phone==primary['phone'] and current.sms_opt_in
        assert current.preferences['admin_text_consent_key']==primary_consent_key(s,current)
        assert len(s.scalars(select(m.Volunteer)).all())==2 and not s.scalar(select(m.Message))
        event=s.scalar(select(m.Event))
        assert staffing_subscriptions.matches(s,event,person)==existing_coordinator
        if existing_coordinator:assert person.preferences['admin_staffing_scope']['ministries']==['Kids']


def primary_consent_key(session, person):
    records=session.scalars(select(m.Notification).where(m.Notification.volunteer_id==person.id,
        m.Notification.state=='enrolled')).all()
    assert len(records)==1
    return records[0].key


def test_portal_event_isolation_multiple_recipients_empty_override_and_new_event_defaults(setup_client):
    client,app,_=setup_client;save(client,complete=True);enable(client);person_id=seed_event(app);enroll(client)
    state=status(client);primary=state['default_admins'][0]['id']
    assert all(e['mode']=='inherit' and e['recipient_ids']==[] for e in state['events'])
    saved=select_event(client,state,0,[person_id,primary]);assert saved.status_code==200
    assert status(client)['events'][0]['recipient_ids']==sorted([person_id,primary])
    assert status(client)['events'][1]['mode']=='inherit'
    assert select_event(client,state,0,[person_id]).status_code==409  # exact stale event route
    saved=select_event(client,status(client),0,[]);assert saved.status_code==200
    with app.state.session_factory() as s:
        events=list(s.scalars(select(m.Event).order_by(m.Event.id)))
        assert event_admins.recipients(s,events[0])==[]
        assert [p.id for p in event_admins.recipients(s,events[1])]==[primary]
        new=m.Event(title='New event',starts_at=app.state.clock.now()+timedelta(days=3),ends_at=app.state.clock.now()+timedelta(days=3,hours=1),status='scheduled')
        s.add(new);s.flush();assert [p.id for p in event_admins.recipients(s,new)]==[primary]
        assert not s.scalar(select(m.Message))
    assert select_event(client,status(client),0,[],mode='inherit').status_code==200


@pytest.mark.parametrize('change',['stop','inactive','authority','foreign_owner','record','deleted'])
def test_selected_admin_eligibility_and_record_hash_rechecked(setup_client,change):
    client,app,_=setup_client;save(client,complete=True);enable(client);person_id=seed_event(app);enroll(client)
    state=status(client)
    with app.state.session_factory() as s:
        s.info['record_authorized']=True;p=s.get(m.Volunteer,person_id)
        if change=='stop':s.add(m.Policy(key='sms_opt_out:'+p.phone,value={'value':True}))
        elif change=='inactive':p.status='inactive'
        elif change=='authority':p.preferences={**p.preferences,event_admins.EVENT_ONLY:False}
        elif change=='foreign_owner':p.preferences={**p.preferences,event_admins.EVENT_OWNER:OWNER_B}
        elif change=='record':p.name='Changed admin'
        else:s.delete(p)
        s.commit()
    assert select_event(client,state,0,[person_id]).status_code==409


def test_two_coordinators_share_approved_schedule_without_config_or_review_token_leak(setup_client):
    client,app,user=setup_client;save(client,complete=True);enable(client);person_id=seed_event(app)
    proof,data,_=enroll(client)
    a=status(client);assert select_event(client,a,0,[person_id]).status_code==200
    approved_a=client.get('/api/state').json();assert approved_a['shifts']
    user['id']=OWNER_B
    assert save(client,details={**DETAILS,'coordinator_phone':'2025550180'},complete=True).status_code==200
    assert enable(client,phone='2025550180').status_code==200
    b=status(client);approved_b=client.get('/api/state').json()
    assert approved_b['shifts']==approved_a['shifts']
    foreign=b['events'][0]
    assert not foreign['editable'] and foreign['event_hash'] is None and foreign['recipient_ids']==[]
    assert person_id not in {p['id'] for p in b['admins']}
    assert client.post('/api/setup/admin-texts/event-recipient',json=data).status_code==409
    assert client.post('/api/setup/admin-texts/review',json={'phone':'2025550198'}).status_code==409
    assert client.post(f"/api/setup/event-admins/{a['events'][0]['id']}",json={'event_hash':a['events'][0]['event_hash'],
        'mode':'selected','recipients':[{'id':person_id,'record_hash':proof['record_hash']}]}).status_code==403
    user['id']=OWNER_A;assert status(client)['events'][0]['recipient_ids']==[person_id]


def test_default_primary_stop_does_not_fall_back_to_other_coordinator(setup_client):
    client,app,_=setup_client;save(client,complete=True);enable(client);seed_event(app,existing_coordinator=True)
    with app.state.session_factory() as s:
        primary=s.scalar(select(m.Volunteer).where(m.Volunteer.phone=='+12025550199'))
        s.add(m.Policy(key='sms_opt_out:'+primary.phone,value={'value':True}));s.commit()
        assert event_admins.default_admins(s)==[]


@pytest.mark.parametrize('pause_first',[False,True])
def test_multiple_owner_primaries_never_use_id_order_or_divert_a_paused_primary(setup_client,pause_first):
    client,app,user=setup_client;save(client,complete=True);enable(client)
    first=status(client)['default_admins'][0]['id']
    user['id']=OWNER_B
    save(client,details={**DETAILS,'coordinator_phone':'2025550180'},complete=True);enable(client,phone='2025550180')
    with app.state.session_factory() as s:
        s.info['record_authorized']=True
        if pause_first:s.get(m.Volunteer,first).status='inactive';s.flush()
        assert event_admins.default_admins(s)==[]


def test_within_owner_reviewed_primary_replacement_uses_workspace_phone_authority(setup_client):
    client,app,_=setup_client;save(client,complete=True);enable(client);person_id=seed_event(app)
    reviewed=client.post('/api/setup/admin-texts/review',json={'phone':'2025550198'}).json()
    data={key:reviewed[key] for key in ('review_id','record_hash','primary_hash')}
    data.update(phone=reviewed['recipient']['phone'],operator_consent=True,consent=False,enabled=True)
    assert client.post('/api/setup/admin-texts',json=data).status_code==200
    with app.state.session_factory() as s:assert [p.id for p in event_admins.default_admins(s)]==[person_id]


def test_factual_cancelled_and_filled_names_leave_initial_roster_separate(session,clock,provider,make_volunteer,make_shift,assign):
    admin=make_volunteer(coordinator=True)
    shift=make_shift('Greeter',starts=clock.now()+timedelta(hours=2))
    cancelled=assign(make_volunteer('Jordan Cancelled'),shift,status='cancelled')
    cancelled.updated_at=clock.now()-timedelta(hours=1)
    replacement=assign(make_volunteer('Casey Fill-in'),shift,status='confirmed')
    replacement.source='fill';replacement.created_at=clock.now()-timedelta(minutes=30)
    other=make_shift('Sound',starts=shift.starts_at);unused=other.event;other.event=shift.event;session.delete(unused)
    initial=assign(make_volunteer('Riley Original'),other,status='confirmed')
    open_slot=make_shift('Parking',starts=shift.starts_at);unused=open_slot.event;open_slot.event=shift.event;session.delete(unused);session.flush()
    facts=event_admins.changes(session,shift.event)
    assert [p['name'] for p in facts['cancelled']]==['Jordan Cancelled']
    assert [p['name'] for p in facts['filled']]==['Casey Fill-in']
    assert [p['name'] for p in facts['initial_roster']]==['Riley Original']
    ctx=FillContext(session,clock,provider,SyntheticGloo())
    notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
    body=provider.sent_to(admin.phone)[0].body
    assert 'Canceled: Jordan Cancelled (Greeter)' in body and 'Filled spots: Casey Fill-in (Greeter)' in body
    assert 'Parking (1)' in body and 'Riley Original' not in body and 'replaced Jordan' not in body
    notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
    assert len(provider.sent_to(admin.phone))==1


def test_assignment_predating_cancellation_is_never_called_a_fill_in(session,clock,make_volunteer,make_shift,assign):
    shift=make_shift();old=assign(make_volunteer(),shift,status='cancelled');old.updated_at=clock.now()
    original=assign(make_volunteer('Earlier roster'),shift,status='confirmed');original.created_at=clock.now()-timedelta(days=1)
    assert event_admins.changes(session,shift.event)['filled']==[]


def test_recorded_fill_of_initial_gap_is_shown_without_inventing_a_cancellation(session,clock,make_volunteer,make_shift,assign):
    shift=make_shift();person=make_volunteer('Initial gap helper');booking=assign(person,shift,status='confirmed')
    booking.source='fill';booking.created_at=clock.now()-timedelta(minutes=10)
    session.add(m.FillRequest(shift_id=shift.id,state='filled',urgency='normal',
        created_at=clock.now()-timedelta(minutes=20),closed_at=booking.created_at));session.flush()
    facts=event_admins.changes(session,shift.event)
    assert facts['cancelled']==[] and facts['filled'][0]['name']=='Initial gap helper' and not facts['initial_roster']
    assert 'Canceled: none' in event_admins.changes_copy(facts)


@pytest.mark.parametrize('change',['cancelled_name','filled_name','role'])
def test_review_binds_names_and_roles_used_in_cancellation_list(session,clock,provider,make_volunteer,make_shift,assign,change):
    session.info['record_authorized']=True
    make_volunteer(coordinator=True);shift=make_shift('Greeter',starts=clock.now()+timedelta(hours=2))
    before=make_volunteer('Jordan');old=assign(before,shift,status='cancelled');old.updated_at=clock.now()-timedelta(hours=1)
    after=make_volunteer('Casey');new=assign(after,shift,status='confirmed');new.created_at=clock.now()-timedelta(minutes=30)
    session.info[confirmations.MODE_KEY]=True
    ctx=FillContext(session,clock,provider,SyntheticGloo());notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
    row=session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    review=session.get(m.Approval,row.detail['approval_id']);original=dict(review.payload)
    if change=='cancelled_name':before.name='Changed cancellation'
    elif change=='filled_name':after.name='Changed fill-in'
    else:shift.role.name='Changed role'
    session.flush()
    confirmations.decide(session,ctx.gate,review,approve=True,actor='fixture@example.test',expected=original['content_hash'],now=clock.now())
    assert not provider.sent and review.status=='expired' and review.payload==original


@pytest.mark.parametrize('boundary,exact',[('review',True),('pull',True),('verify',True),('pull',False),('verify',False)])
def test_changed_event_recipients_hold_exact_old_text_and_new_admin_gets_fresh_review(acceptance_app,boundary,exact):
    client,app,gloo,clock=acceptance_app
    app.state.settings=replace(app.state.settings,competition_confirmation_required=exact)
    person_id=seed_event(app);enroll(client)
    with app.state.session_factory() as s:
        s.info[confirmations.MODE_KEY]=exact
        ctx=FillContext(s,clock,app.state.provider,gloo)
        notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
        row=s.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
        review=s.get(m.Approval,row.detail['approval_id']) if exact else None
        review_id=review.id if review else None
        original=dict(review.payload) if review else {'body':s.get(m.Message,row.message_id).body}
        old_message_id=row.message_id
        event_id=row.event_id;s.commit()
    claim=None
    if boundary!='review':
        if exact:
            assert client.post(f'/api/proposals/{review_id}/approve',json={'content_hash':original['content_hash']}).status_code==200
            with app.state.session_factory() as s:old_message_id=s.get(m.Approval,review_id).payload['message_id']
        if boundary=='verify':claim=next(p for p in pull(client).json()['messages'] if p['id']==old_message_id)
    assert select_event(client,status(client),0,[person_id]).status_code==200
    if boundary=='review':
        assert client.post(f'/api/proposals/{review_id}/approve',json={'content_hash':original['content_hash']}).status_code==409
    elif boundary=='pull':assert old_message_id not in {p['id'] for p in pull(client).json()['messages']}
    else:
        assert client.post(f"/mac/outbound/{claim['id']}/verify",json={'token':claim['token'],'content_hash':original.get('content_hash')},
            headers={'Authorization':'Bearer '+BRIDGE_TOKEN}).status_code==409
    with app.state.session_factory() as s:
        if exact:
            old=s.get(m.Approval,review_id)
            assert old.payload['body']==original['body'] and old.payload['content_hash']==original['content_hash']
        else:assert s.get(m.Message,old_message_id).body==original['body']
        s.info[confirmations.MODE_KEY]=exact
        ctx=FillContext(s,clock,app.state.provider,gloo);notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
        rows=s.scalars(select(m.Notification).where(m.Notification.event_id==event_id,m.Notification.volunteer_id==person_id,
            m.Notification.key.startswith('pre-event:'))).all()
        assert len(rows)==1 and rows[0].state==('awaiting_approval' if exact else 'sent'), rows[0].detail.get('reason') if rows else None
        if exact:
            new=s.get(m.Approval,rows[0].detail['approval_id']);assert new.id!=review_id
            assert new.payload['volunteer_id']==person_id
        assert rows[0].detail['pre_event_source']['facts']['event_admin_route']['recipient_ids']==[person_id]


def test_event_routing_does_not_change_ministry_staffing_source(setup_client):
    client,app,_=setup_client;save(client,complete=True);enable(client);person_id=seed_event(app);enroll(client)
    with app.state.session_factory() as s:
        ctx=FillContext(s,app.state.clock,app.state.provider,SyntheticGloo())
        event=s.scalar(select(m.Event));notifications.queue_staffing(ctx,event)
        row=s.scalar(select(m.Notification).where(m.Notification.key.startswith('staffing:')))
        before=staffing_subscriptions.capture(s,row,app.state.clock.now());s.commit();key=row.key
    assert select_event(client,status(client),0,[person_id]).status_code==200
    with app.state.session_factory() as s:
        assert staffing_subscriptions.capture(s,s.get(m.Notification,key),app.state.clock.now())==before


@pytest.mark.parametrize('count,held',[(12,False),(35,True)])
def test_complete_long_pre_event_list_uses_code_limit_or_holds_without_truncation(session,clock,provider,make_volunteer,make_shift,assign,count,held):
    admin=make_volunteer(coordinator=True)
    shift=make_shift('Greeter',starts=clock.now()+timedelta(hours=2))
    names=[]
    for i in range(count):
        name=f'Cancelled Person{i:02} '+('A'*35);names.append(name)
        booking=assign(make_volunteer(name),shift,status='cancelled');booking.updated_at=clock.now()-timedelta(hours=1)
    replacement=assign(make_volunteer('Casey Cover'),shift,status='confirmed');replacement.created_at=clock.now()-timedelta(minutes=30)
    ctx=FillContext(session,clock,provider,SyntheticGloo())
    notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
    row=session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    if held:
        assert not provider.sent and not ctx.gloo.calls and row.state=='blocked_policy'
        assert '1,600' in row.detail['reason'] and 'Shifts' in row.detail['reason']
        assert len(event_admins.changes(session,shift.event)['cancelled'])==count
    else:
        body=provider.sent_to(admin.phone)[0].body
        assert 600<len(body)<=1600 and all(name in body for name in names) and 'Casey Cover' in body
        import json
        assert json.loads(ctx.gloo.calls[0]['input'])['max_chars']==1600


@pytest.mark.parametrize('length,allowed',[(100,True),(601,False)])
def test_ordinary_composer_keeps_original_600_character_limit(session,clock,make_volunteer,length,allowed):
    from app.core.signup_responder import compose_signup_reply
    from app.llm.gloo_client import GlooUnavailableError
    gloo=SyntheticGloo();person=make_volunteer();body='A'*length
    if allowed:assert compose_signup_reply(session,clock,gloo,body,(body,),volunteer=person,require_gloo=True)==body
    else:
        with pytest.raises(GlooUnavailableError):compose_signup_reply(session,clock,gloo,body,(body,),volunteer=person,require_gloo=True)
    import json
    assert 'max_chars' not in json.loads(gloo.calls[0]['input'])


def test_return_to_previous_unsent_recipient_recaptures_once_without_reusing_old_review(acceptance_app):
    client,app,gloo,clock=acceptance_app;app.state.settings=replace(app.state.settings,competition_confirmation_required=True)
    person_id=seed_event(app);enroll(client)
    with app.state.session_factory() as s:
        s.info[confirmations.MODE_KEY]=True;ctx=FillContext(s,clock,app.state.provider,gloo)
        notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
        row=s.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
        old_id=row.detail['approval_id'];old_payload=dict(s.get(m.Approval,old_id).payload);key=row.key;s.commit()
    assert select_event(client,status(client),0,[person_id]).status_code==200
    assert select_event(client,status(client),0,[],mode='inherit').status_code==200
    with app.state.session_factory() as s:
        s.info[confirmations.MODE_KEY]=True;ctx=FillContext(s,clock,app.state.provider,gloo)
        notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
        row=s.get(m.Notification,key)
        assert row.state=='awaiting_approval' and row.detail['approval_id']!=old_id
        assert s.get(m.Approval,old_id).payload==old_payload and s.get(m.Approval,old_id).status=='expired'
        calls=len(gloo.calls);notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx)
        assert len(gloo.calls)==calls


@pytest.mark.parametrize('change',['delete','remove_owner'])
def test_lost_primary_never_falls_back_to_other_contact(setup_client,change):
    client,app,_=setup_client;save(client,complete=True);enable(client)
    seed_event(app,existing_coordinator=True)
    with app.state.session_factory() as s:
        s.info['record_authorized']=True
        primary=s.scalar(select(m.Volunteer).where(m.Volunteer.phone=='+12025550199'))
        if change=='delete':s.delete(primary)
        else:primary.preferences={k:v for k,v in primary.preferences.items() if k!='admin_text_owner'}
        s.commit();assert event_admins.default_admins(s)==[]


@pytest.mark.parametrize('exact',[False,True])
def test_event_only_recipient_keeps_volunteer_routing_and_ranking(setup_client,exact):
    from app.core.inbound import handle_inbound
    from app.core.ranking import rank_candidates
    from app.llm.parser import ParsedMessage
    client,app,_=setup_client;save(client,complete=True);enable(client)
    person_id=seed_event(app);enroll(client)
    with app.state.session_factory() as s:
        person=s.get(m.Volunteer,person_id);s.info[confirmations.MODE_KEY]=exact
        parsed=[]
        def parser(body):
            parsed.append(body);return ParsedMessage(intent='unclear',confidence=0)
        result=handle_inbound(s,app.state.clock,app.state.provider,person.phone,'PLAN tomorrow',parser=parser)
        assert parsed==['PLAN tomorrow'] and result.routed_to not in {'admin_agent','human_review'}
        rejected={};rank_candidates(s,s.scalar(select(m.Shift)),app.state.clock.now(),rejected=rejected)
        assert rejected.get(person_id)!=['coordinator or pastor']


@pytest.mark.parametrize('purpose',['coordinator_notify','escalation_notify'])
def test_event_only_recipient_cannot_receive_unbound_admin_text(setup_client,purpose):
    from app.core.send_gate import SendGate,SendStatus
    client,app,_=setup_client;save(client,complete=True);enable(client)
    person_id=seed_event(app);enroll(client)
    with app.state.session_factory() as s:
        person=s.get(m.Volunteer,person_id)
        outcome=SendGate(s,app.state.clock,app.state.provider).send(volunteer=person,purpose=purpose,body='Unbound admin update.')
        assert outcome.status==SendStatus.BLOCKED_POLICY and not s.scalar(select(m.Message))


@pytest.mark.parametrize('change',['receipt_deleted','receipt_owner','receipt_phone','review_revoked','review_hash'])
def test_event_only_recipient_requires_intact_reviewed_consent(setup_client,change):
    client,app,_=setup_client;save(client,complete=True);enable(client)
    person_id=seed_event(app);enroll(client);select_event(client,status(client),0,[person_id])
    with app.state.session_factory() as s:
        person=s.get(m.Volunteer,person_id);receipt=s.get(m.Notification,person.preferences['admin_text_consent_key'])
        review=s.get(m.Notification,receipt.detail['review_id'])
        if change=='receipt_deleted':s.delete(receipt)
        elif change=='receipt_owner':receipt.detail={**receipt.detail,'owner_id':OWNER_B}
        elif change=='receipt_phone':receipt.detail={**receipt.detail,'phone':'+12025550100'}
        elif change=='review_revoked':review.state='revoked'
        else:review.detail={**review.detail,'record_hash':'changed'}
        s.commit();assert not event_admins.eligible(s,person)
        assert event_admins.recipients(s,s.scalar(select(m.Event).order_by(m.Event.id)))==[]


@pytest.mark.parametrize('target',['receipt','review'])
@pytest.mark.parametrize('separate_session',[False,True])
@pytest.mark.parametrize('existing_coordinator',[False,True])
def test_consent_revocation_is_fresh_and_preserves_same_session_changes(setup_client,target,separate_session,existing_coordinator):
    client,app,_=setup_client;save(client,complete=True);enable(client)
    person_id=seed_event(app,existing_coordinator=existing_coordinator);enroll(client);select_event(client,status(client),0,[person_id])
    with app.state.session_factory() as s:
        person=s.get(m.Volunteer,person_id)
        receipt=s.get(m.Notification,person.preferences['admin_text_consent_key'])
        review=s.get(m.Notification,receipt.detail['review_id'])
        event=s.scalar(select(m.Event).order_by(m.Event.id))
        assert [v.id for v in event_admins.recipients(s,event)]==[person_id]
        key=receipt.key if target=='receipt' else review.key
        if separate_session:
            with app.state.session_factory() as other:
                other.get(m.Notification,key).state='revoked';other.commit()
        else:s.get(m.Notification,key).state='revoked'
        assert event_admins.recipients(s,event)==[]
        notice=m.Notification(key=f'pre-event:{event.id}:{person_id}:probe',event_id=event.id,
            volunteer_id=person_id,purpose='coordinator_notify',state='pending',body='',created_at=app.state.clock.now(),
            due_at=app.state.clock.now(),detail={'event_start':event.starts_at.isoformat()})
        s.add(notice);s.flush()
        assert notifications.pre_event_delivery_problem(s,notice,app.state.clock.now())
        assert s.get(m.Notification,key).state=='revoked'


def test_inherited_primary_reload_holds_revoked_enrollment(setup_client):
    client,app,_=setup_client;save(client,complete=True);enable(client);seed_event(app,existing_coordinator=True)
    with app.state.session_factory() as s:
        primary=event_admins.default_admins(s)[0]
        receipt=s.get(m.Notification,primary.preferences['admin_text_consent_key'])
        with app.state.session_factory() as other:
            other.get(m.Notification,receipt.key).state='revoked';other.commit()
        assert event_admins.default_admins(s)==[]
