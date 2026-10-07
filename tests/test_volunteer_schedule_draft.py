"""Volunteer discovery/drafting precedes coordinator review, entirely synthetic."""
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.core.inbound import handle_inbound
from app.core import confirmations, paired_planning
from app.agents.fill_agent import FillContext
from app.db import models as m
from tests.test_opportunities_reply import ExactGloo


class ChoicesGloo(ExactGloo):
    choices = [1]
    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        if 'reply' in facts:
            from app.llm.gloo_client import GlooUnavailableError
            self.calls.append(kwargs)
            if self.fail:
                raise GlooUnavailableError('Synthetic outage')
            return SimpleNamespace(output_text=json.dumps({'understood':True, 'choice_numbers':self.choices}))
        return super().create_response(**kwargs)


def incoming(session, clock, provider, person, gloo, body):
    return handle_inbound(session, clock, provider, person.phone, body,
        lambda _: pytest.fail('The grounded draft workflow must precede generic parsing'),
        ctx=FillContext(session, clock, provider, gloo))


def person_and_options(make_volunteer, make_shift, clock):
    person = make_volunteer(prefs={'onboarding_stage':'complete', 'interested_roles':['usher'], 'max_per_month':2})
    a = make_shift(); b = make_shift(starts=clock.now()+timedelta(days=10))
    return person, a, b


@pytest.mark.parametrize('question', [
    'I have some availability coming up, what can I sign up for within my presences?',
    'Can I sign up?', 'What can I sign up for within my preferences?'])
def test_signup_questions_use_saved_preferences_without_intake_or_early_review(session, clock, provider, make_volunteer, make_shift, question):
    person, a, b = person_and_options(make_volunteer, make_shift, clock)
    gloo = ChoicesGloo()
    assert incoming(session, clock, provider, person, gloo, question).routed_to == 'booking_status'
    assert '1:' in provider.sent[-1].body and '2:' in provider.sent[-1].body
    assert not session.scalar(select(m.Escalation)) and not session.scalar(select(m.Approval))
    assert 'serving_requests' not in person.preferences
    assert incoming(session, clock, provider, person, gloo, 'I already am').routed_to == 'booking_status'
    assert 'Open options' in provider.sent[-1].body


def test_selection_preview_yes_then_final_exact_review_even_without_global_review_mode(session, clock, provider, make_volunteer, make_shift):
    person, a, b = person_and_options(make_volunteer, make_shift, clock); gloo = ChoicesGloo()
    original = dict(person.preferences)
    incoming(session, clock, provider, person, gloo, 'Can I sign up?')
    incoming(session, clock, provider, person, gloo, '1')
    assert 'Proposed schedule' in provider.sent[-1].body and 'not booked yet' in provider.sent[-1].body
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Approval))
    incoming(session, clock, provider, person, gloo, 'YES')
    approval = session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record')).one()
    assert approval.status=='pending' and not session.scalar(select(m.Assignment))
    assert 'final coordinator approval' in provider.sent[-1].body and 'not booked yet' in provider.sent[-1].body
    assert person.preferences==original and not session.scalar(select(m.Qualification))
    incoming(session, clock, provider, person, gloo, 'YES')
    assert len(session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record')).all())==1
    ctx=FillContext(session, clock, provider, gloo)
    confirmations.decide(session, ctx.gate, approval, approve=True, actor='Synthetic coordinator', expected=approval.payload['content_hash'], now=clock.now(), ctx=ctx)
    row=session.scalars(select(m.Assignment)).one(); assert row.shift_id==a.id and row.status=='approved'


def test_natural_multiple_choices_are_drafted_together(session, clock, provider, make_volunteer, make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo();gloo.choices=[1,2]
    incoming(session,clock,provider,person,gloo,'Can I sign up?')
    incoming(session,clock,provider,person,gloo,'I would like both dates as an usher')
    assert 'Proposed schedule' in provider.sent[-1].body
    incoming(session,clock,provider,person,gloo,'YES')
    approvals=session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record')).all()
    assert {x.payload['after']['shift_id'] for x in approvals}=={a.id,b.id}
    assert not session.scalar(select(m.Assignment))


def test_bare_yes_to_discovery_asks_for_choice_without_review(session, clock, provider, make_volunteer, make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo()
    incoming(session,clock,provider,person,gloo,'Can I sign up?')
    incoming(session,clock,provider,person,gloo,'YES')
    assert 'Which option' in provider.sent[-1].body
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Approval))


@pytest.mark.parametrize('change',['occupied','consent','frequency','event'])
def test_final_review_revalidates_changed_facts(session, clock, provider, make_volunteer, make_shift, assign, change):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo()
    for body in ('Can I sign up?','1','YES'):incoming(session,clock,provider,person,gloo,body)
    approval=session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record')).one()
    if change=='occupied':assign(make_volunteer(),a)
    if change=='consent':person.sms_opt_in=False
    if change=='frequency':person.preferences={**person.preferences,'max_per_month':0}
    if change=='event':a.event.status='cancelled'
    ctx=FillContext(session,clock,provider,gloo)
    with pytest.raises(ValueError):confirmations.decide(session,ctx.gate,approval,approve=True,actor='Synthetic coordinator',expected=approval.payload['content_hash'],now=clock.now(),ctx=ctx)
    assert not session.scalar(select(m.Assignment).where(m.Assignment.volunteer_id==person.id))


def test_combined_choices_cannot_exceed_monthly_cap(session,clock,provider,make_volunteer,make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);person.preferences={**person.preferences,'max_per_month':1};gloo=ChoicesGloo();gloo.choices=[1,2]
    incoming(session,clock,provider,person,gloo,'Can I sign up?')
    incoming(session,clock,provider,person,gloo,'both')
    assert 'no longer fit' in provider.sent[-1].body
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Approval))


def test_choices_in_different_months_keep_separate_frequency_counts(session,clock,provider,make_volunteer,make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock)
    b.event.starts_at=clock.now()+timedelta(days=38);b.event.ends_at=b.event.starts_at+timedelta(hours=1)
    person.preferences={**person.preferences,'max_per_month':1};gloo=ChoicesGloo();gloo.choices=[1,2]
    incoming(session,clock,provider,person,gloo,'Can I sign up?');incoming(session,clock,provider,person,gloo,'both dates')
    assert 'Proposed schedule' in provider.sent[-1].body
    incoming(session,clock,provider,person,gloo,'YES')
    assert len(session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record')).all())==2


def test_overlapping_selected_options_do_not_create_review(session,clock,provider,make_volunteer,make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock)
    b.event.starts_at=a.starts_at;b.event.ends_at=a.ends_at
    gloo=ChoicesGloo();gloo.choices=[1,2]
    incoming(session,clock,provider,person,gloo,'Can I sign up?');incoming(session,clock,provider,person,gloo,'both dates')
    assert 'no longer fit' in provider.sent[-1].body and not session.scalar(select(m.Approval))


def test_changed_preview_cannot_authorize_submission(session,clock,provider,make_volunteer,make_shift,assign):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo()
    incoming(session,clock,provider,person,gloo,'Can I sign up?');incoming(session,clock,provider,person,gloo,'1')
    assign(make_volunteer(),a);incoming(session,clock,provider,person,gloo,'YES')
    assert 'no longer fit' in provider.sent[-1].body and not session.scalar(select(m.Approval))


def test_undelivered_preview_and_model_outage_cannot_submit(session,clock,provider,make_volunteer,make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo()
    incoming(session,clock,provider,person,gloo,'Can I sign up?')
    gloo.fail=True;incoming(session,clock,provider,person,gloo,'1')
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Approval))
    gloo.fail=False;incoming(session,clock,provider,person,gloo,'1')
    last=session.scalar(select(m.Message).where(m.Message.direction=='out').order_by(m.Message.id.desc()))
    last.status='queued'
    incoming(session,clock,provider,person,gloo,'YES')
    assert not session.scalar(select(m.Approval)) and not session.scalar(select(m.Assignment))


def test_required_pair_is_one_option_and_one_atomic_final_review(session,clock,provider,make_volunteer,make_shift):
    a=make_shift(role_name='usher');b=make_shift(role_name='Coffee',starts=a.ends_at,minutes=30)
    person=make_volunteer(prefs={'onboarding_stage':'complete','interested_roles':['usher','Coffee'],'max_per_month':2})
    rule=paired_planning.stage_rules(session,clock.now(),person,pairs=[{'role_ids':[a.role_id,b.role_id]}])
    confirmations.decide(session,FillContext(session,clock,provider,None).gate,rule,approve=True,actor='Synthetic coordinator',expected=rule.payload['content_hash'],now=clock.now())
    gloo=ChoicesGloo()
    incoming(session,clock,provider,person,gloo,'Can I sign up?')
    assert 'and Coffee' in provider.sent[-1].body
    incoming(session,clock,provider,person,gloo,'1');incoming(session,clock,provider,person,gloo,'YES')
    review=session.scalars(select(m.Approval).where(m.Approval.status=='pending')).one()
    assert review.payload['record']=='AssignmentPair' and not session.scalar(select(m.Assignment))
    ctx=FillContext(session,clock,provider,gloo);confirmations.decide(session,ctx.gate,review,approve=True,actor='Synthetic coordinator',expected=review.payload['content_hash'],now=clock.now(),ctx=ctx)
    assert {r.shift_id for r in session.scalars(select(m.Assignment))}=={a.id,b.id}


def test_help_and_completed_draft_acknowledgments_keep_responding(session,clock,provider,make_volunteer,make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo()
    incoming(session,clock,provider,person,gloo,'Can I sign up?')
    assert incoming(session,clock,provider,person,gloo,'HELP').routed_to=='help'
    assert 'Text Monkey helps' in provider.sent[-1].body
    incoming(session,clock,provider,person,gloo,'Can I sign up?')
    incoming(session,clock,provider,person,gloo,'1');incoming(session,clock,provider,person,gloo,'YES')
    review=session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record')).one()
    ctx=FillContext(session,clock,provider,gloo)
    confirmations.decide(session,ctx.gate,review,approve=True,actor='Synthetic coordinator',expected=review.payload['content_hash'],now=clock.now(),ctx=ctx)
    incoming(session,clock,provider,person,gloo,'Thanks')
    assert "You're welcome" in provider.sent[-1].body


def test_expired_preview_returns_fresh_options_without_submission(session,clock,provider,make_volunteer,make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo()
    incoming(session,clock,provider,person,gloo,'Can I sign up?');incoming(session,clock,provider,person,gloo,'1')
    clock.set_time(clock.now()+timedelta(hours=3));incoming(session,clock,provider,person,gloo,'YES')
    assert 'Current options' in provider.sent[-1].body
    assert not session.scalar(select(m.Approval)) and not session.scalar(select(m.Assignment))


def test_choice_outage_retries_original_job_without_new_inbound(session,clock,provider,make_volunteer,make_shift):
    from app.core.notifications import flush_due
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo()
    incoming(session,clock,provider,person,gloo,'Can I sign up?');gloo.fail=True
    incoming(session,clock,provider,person,gloo,'1')
    count=len(session.scalars(select(m.Message).where(m.Message.direction=='in')).all())
    gloo.fail=False;clock.set_time(clock.now()+timedelta(minutes=5))
    flush_due(FillContext(session,clock,provider,gloo))
    assert 'Proposed schedule' in provider.sent[-1].body
    assert len(session.scalars(select(m.Message).where(m.Message.direction=='in')).all())==count
    assert session.scalar(select(m.Notification).where(m.Notification.purpose=='volunteer_choice_work')).state=='completed'
    assert not session.scalar(select(m.Approval))


def test_withdrawal_rejects_pending_draft_only(session,clock,provider,make_volunteer,make_shift):
    person,a,b=person_and_options(make_volunteer,make_shift,clock);gloo=ChoicesGloo()
    for body in ('Can I sign up?','1','YES'):incoming(session,clock,provider,person,gloo,body)
    review=session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record')).one()
    incoming(session,clock,provider,person,gloo,"No, I changed my mind. Don't book either shift.")
    assert review.status=='rejected' and 'withdrawn' in provider.sent[-1].body
    ctx=FillContext(session,clock,provider,gloo)
    with pytest.raises(ValueError):confirmations.decide(session,ctx.gate,review,approve=True,actor='Synthetic coordinator',expected=review.payload['content_hash'],now=clock.now(),ctx=ctx)
    assert not session.scalar(select(m.Assignment))
