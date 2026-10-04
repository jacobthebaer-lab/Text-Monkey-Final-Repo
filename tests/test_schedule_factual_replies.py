"""Fictional sender records and literal model doubles, never native delivery."""
from datetime import timedelta
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select, update, delete
from sqlalchemy.orm import Session
from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core import booking_status, confirmations, notifications, reminders, offer_windows
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import ParsedMessage
from tests.test_planning_composition import reviewed, human_change

class LiteralGloo:
    settings = Settings(gloo_signup_replies=False)
    def __init__(self, *, unavailable=False, suffix='', during=None):
        self.calls=[];self.unavailable=unavailable;self.suffix=suffix;self.during=during
    def create_response(self,**kwargs):
        facts=json.loads(kwargs['input']);self.calls.append(facts)
        if self.during:
            callback=self.during;self.during=None;callback()
        if self.unavailable:raise GlooUnavailableError('Synthetic outage')
        return SimpleNamespace(output_text=facts['approved_message']+self.suffix,usage=None)

def question(session,clock,provider,person,gloo,body='Am I booked for anything?',exact=False):
    if exact:session.info[confirmations.MODE_KEY]=True
    ctx=FillContext(session,clock,provider,gloo)
    result=handle_inbound(session,clock,provider,person.phone,body,
        lambda text:ParsedMessage(intent='question',confidence=1),ctx=ctx)
    assert result.routed_to=='booking_status'
    row=session.scalar(select(m.Notification).where(m.Notification.purpose=='booking_status').order_by(m.Notification.created_at.desc(),m.Notification.key.desc()))
    return ctx,row

@pytest.mark.parametrize('missing',[False,True])
def test_disabled_signup_setting_or_missing_client_never_allows_booking_fallback(session,clock,provider,make_volunteer,missing):
    person=make_volunteer('Alpha Synthetic');gloo=None if missing else LiteralGloo(unavailable=True)
    ctx,row=question(session,clock,provider,person,gloo)
    assert row.state=='pending' and row.detail['gloo_attempts']==1 and not provider.sent
    assert row.due_at==clock.now()+timedelta(minutes=2)
    assert row.expires_at==clock.now()+timedelta(minutes=10)
    notifications.flush_due(ctx);assert row.detail['gloo_attempts']==1
    clock.advance(timedelta(minutes=2));notifications.flush_due(ctx)
    assert row.detail['gloo_attempts']==2 and not provider.sent
    ctx.gloo=LiteralGloo();clock.advance(timedelta(minutes=2));notifications.flush_due(ctx)
    assert len(ctx.gloo.calls)==1 and row.state=='sent' and len(provider.sent)==1
    booking_status.reply(session,clock,ctx.gate,person,ctx.gloo)
    notifications.flush_due(ctx)
    assert len(ctx.gloo.calls)==1 and len(provider.sent)==1


def test_three_failures_are_visible_internal_hold_then_question_expires(session,clock,provider,make_volunteer):
    ctx,row=question(session,clock,provider,make_volunteer(),LiteralGloo(unavailable=True))
    for _ in range(2):
        clock.advance(timedelta(minutes=2));notifications.flush_due(ctx)
    assert row.state=='blocked' and row.detail['gloo_attempts']==3
    assert session.scalar(select(m.Escalation).where(m.Escalation.related_ids['notification_key'].as_string()==row.key))
    assert not provider.sent


def test_retry_uses_changed_schedule_and_question_context(session,clock,provider,make_volunteer,make_shift,assign):
    a=make_volunteer('Alpha Synthetic');b=make_volunteer('Beta Synthetic')
    assign(b,make_shift('Other Role',title='Foreign Event'))
    ctx,row=question(session,clock,provider,a,LiteralGloo(unavailable=True))
    assign(a,make_shift('Greeter',title='Saved Morning Event'),status='confirmed')
    ctx.gloo=LiteralGloo();clock.advance(timedelta(minutes=2));notifications.flush_due(ctx)
    text=provider.sent_to(a.phone)[0].body
    assert 'Alpha' in text and 'Saved Morning Event' in text and 'Greeter' in text
    assert 'Foreign Event' not in text and 'Beta' not in text
    facts=ctx.gloo.calls[0]
    assert facts['schedule_context']['question']=='Am I booked for anything?'
    assert facts['schedule_context']['schedule']['assignments'][0]['event_title']=='Saved Morning Event'

@pytest.mark.parametrize('suffix',[' Your shift is at Main Hall, arrive at 8:15am.',' You are also booked tomorrow.','\n',' — Thanks!'])
def test_unsupported_schedule_suffix_is_held_for_both_confirmation_and_status(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,suffix):
    person=make_volunteer('Alpha Synthetic');row=assign(person,make_shift('Greeter',title='Saved Event',starts=clock.now()+timedelta(days=2)))
    session.info[confirmations.MODE_KEY]=True
    gloo=LiteralGloo(suffix=suffix);ctx=FillContext(session,clock,provider,gloo,log_dir=tmp_path)
    reminders.process(ctx)
    assert session.get(m.Policy,f'job:assignment:{row.id}').value['state']=='gloo_unavailable'
    _,pending=question(session,clock,provider,person,gloo,exact=True)
    assert pending.state=='pending' and pending.detail['gloo_attempts']==1
    assert not session.scalar(select(m.Approval).where(m.Approval.kind=='confirm_text')) and not provider.sent


def test_same_role_time_different_events_have_distinct_saved_titles(session,clock,provider,make_volunteer,make_shift,assign):
    person=make_volunteer('Alpha Synthetic')
    starts=clock.now()+timedelta(days=2)
    for title in ('Saved Morning Event','Saved Community Event'):
        assign(person,make_shift('Greeter',title=title,starts=starts),status='confirmed')
    model=LiteralGloo();question(session,clock,provider,person,model)
    assert all(title in provider.sent[0].body for title in ('Saved Morning Event','Saved Community Event'))
    assert len(model.calls[0]['schedule_context']['schedule']['assignments'])==2

@pytest.mark.parametrize('changed',['title','role','question','phone','name','opt_out','delete_assignment'])
def test_mid_composition_changes_never_enqueue_old_booking_answer(session,clock,provider,make_volunteer,make_shift,assign,changed):
    person=make_volunteer('Alpha Synthetic');assignment=assign(person,make_shift('Greeter',title='Saved Event'))
    def change():
        # A separate identity map changes rows while the writer retains cached objects.
        with Session(session.get_bind()) as other:
            if changed=='title':other.execute(update(m.Event).where(m.Event.id==assignment.shift.event_id).values(title='Changed Event'))
            elif changed=='role':other.execute(update(m.Role).where(m.Role.id==assignment.shift.role_id).values(name='Changed Role'))
            elif changed=='question':other.execute(update(m.Message).where(m.Message.direction=='in',m.Message.phone==person.phone).values(body='Am I scheduled for shifts now?'))
            elif changed=='phone':other.execute(update(m.Volunteer).where(m.Volunteer.id==person.id).values(phone='+12025550199'))
            elif changed=='name':other.execute(update(m.Volunteer).where(m.Volunteer.id==person.id).values(name='Changed Synthetic'))
            elif changed=='opt_out':other.execute(update(m.Volunteer).where(m.Volunteer.id==person.id).values(sms_opt_in=False))
            else:other.execute(delete(m.Assignment).where(m.Assignment.id==assignment.id))
            other.commit()
    ctx,row=question(session,clock,provider,person,LiteralGloo(during=change),exact=True)
    assert row.state in {'pending','blocked_policy'} and not provider.sent
    assert not session.scalar(select(m.Approval).where(m.Approval.kind=='confirm_text'))

@pytest.mark.parametrize('change_kind',['phone','delete_assignment','delete_shift'])
def test_fill_confirmation_mutation_after_gloo_is_held_cleanly(session,clock,provider,make_volunteer,make_shift,assign,change_kind):
    person=make_volunteer('Alpha Synthetic');assignment=assign(person,make_shift('Greeter',title='Saved Event'),status='confirmed')
    def change():
        with Session(session.get_bind()) as other:
            if change_kind=='phone':other.execute(update(m.Volunteer).where(m.Volunteer.id==person.id).values(phone='+12025550199'))
            elif change_kind=='delete_assignment':other.execute(delete(m.Assignment).where(m.Assignment.id==assignment.id))
            else:other.execute(delete(m.Shift).where(m.Shift.id==assignment.shift_id))
            other.commit()
    model=LiteralGloo(during=change);ctx=FillContext(session,clock,provider,model)
    row=notifications.deliver(ctx,key='synthetic-assignment',body='A saved placement.',purpose='confirmation',volunteer=person,
        conversation={'assignment_id':assignment.id,'notice':'scheduled'})
    assert row.state=='blocked_policy' and not provider.sent

@pytest.mark.parametrize('changed',['title','offer_closed','timezone','question'])
def test_changed_booking_source_invalidates_already_staged_review(session,clock,provider,make_volunteer,make_shift,assign,changed):
    person=make_volunteer('Alpha Synthetic');assignment=assign(person,make_shift('Greeter',title='Saved Event'))
    if changed=='offer_closed':
        offer,window=historical_offer(session,clock,person,make_shift('Usher',title='Saved Offer Event'))
    ctx,row=question(session,clock,provider,person,LiteralGloo(),exact=True)
    approval=session.get(m.Approval,row.detail['approval_id']);assert row.state=='awaiting_approval'
    if changed=='title':human_change(session,lambda:setattr(assignment.shift.event,'title','Changed Event'))
    elif changed=='offer_closed':offer.response='expired'
    elif changed=='timezone':session.add(m.Policy(key='church_timezone',value={'value':'America/New_York'}))
    else:session.get(m.Message,row.detail['reply_id']).body='Am I assigned for shifts now?'
    session.flush();reviewed(session,ctx,approval)
    assert approval.status=='expired' and not provider.sent


def historical_offer(session,clock,person,shift):
    fill=m.FillRequest(shift_id=shift.id,state='in_progress',urgency='normal',created_at=clock.now());session.add(fill)
    message=m.Message(phone=person.phone,volunteer_id=person.id,body='Synthetic earlier offer',direction='out',kind='ai',purpose='outreach',status='sent',created_at=clock.now());session.add(message);session.flush()
    offer=m.Outreach(fill_request_id=fill.id,volunteer_id=person.id,tranche=1,message_id=message.id,response='none');session.add(offer);session.flush()
    window=offer_windows.prepare(session,offer,message.body,clock.now());window.state='offer_active';session.flush()
    return offer,window

@pytest.mark.parametrize('invalid',['expired','changed_title','uncertain','unproven'])
def test_invalid_saved_offers_never_get_rsvp_in_status(session,clock,provider,make_volunteer,make_shift,invalid):
    person=make_volunteer('Alpha Synthetic');shift=make_shift('Greeter',title='Saved Offer Event')
    offer,window=historical_offer(session,clock,person,shift)
    if invalid=='expired':window.expires_at=clock.now()-timedelta(seconds=1)
    elif invalid=='changed_title':shift.event.title='Changed Event'
    elif invalid=='uncertain':window.state='offer_uncertain'
    else:session.delete(window)
    session.flush();question(session,clock,provider,person,LiteralGloo())
    assert 'YES' not in provider.sent[0].body and 'pending offer' not in provider.sent[0].body


def test_quiet_hours_expiry_and_optout_prechecks_spend_no_model_calls(session,clock,provider,make_volunteer):
    person=make_volunteer('Alpha Synthetic');clock.set_time(clock.now().replace(hour=23))
    model=LiteralGloo();ctx,row=question(session,clock,provider,person,model)
    assert not model.calls and not provider.sent and row.state=='pending'
    clock.advance(timedelta(hours=10));notifications.flush_due(ctx)
    assert row.state=='expired' and not model.calls


def test_repeat_dispatch_does_not_recompose_or_stage_duplicate_review(session,clock,provider,make_volunteer):
    person=make_volunteer('Alpha Synthetic');model=LiteralGloo();ctx,row=question(session,clock,provider,person,model,exact=True)
    assert row.state=='awaiting_approval' and len(model.calls)==1
    booking_status.reply(session,clock,ctx.gate,person,model);notifications.flush_due(ctx)
    assert len(model.calls)==1 and len(session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text')).all())==1
