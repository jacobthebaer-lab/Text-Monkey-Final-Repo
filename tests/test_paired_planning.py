"""Fictional paired planning, no network/model/native delivery."""
from datetime import timedelta
from copy import deepcopy

import pytest
from sqlalchemy import select
from app.agents.planning_agent import plan_month
from app.core import eligibility, paired_planning as pairs, scheduler, confirmations, ranking
from app.core.planning_patterns import stage_pattern_review
from app.db import models as m
from tests.conftest import NOW
from tests.test_recurring_availability import window
from tests.test_planning_composition import context, ConnectedDouble, reviewed, human_change
from tests.test_planning_workflows_api import planning_client


def case(session,clock,make_volunteer,make_shift,tmp_path,*,qualified=True,windows=True,global_limit=None):
    shifts = []
    for day in (4,11,18):
        shifts.append(make_shift('Greeter', starts=NOW.replace(day=day,hour=9), minutes=75))
        shifts.append(make_shift('Production', starts=NOW.replace(day=day,hour=11), minutes=75,
                                 required=('production_training',)))
    a,b = shifts[0].role, shifts[1].role
    prefs = {'onboarding_stage':'complete','interested_roles':[a.name,b.name], 'max_per_month':global_limit,
        'role_frequency_caps':[{'role_id':r.id,'role_name':r.name,'max_per_month':2} for r in (a,b)],
        'pending_constraints':[{'kind':'same_day','role_ids':[a.id,b.id],'description':'Fictional stated pairing'}]}
    if windows:
        prefs['recurring_windows']=[window(a,start='09:00',end='10:15'),window(b,start='11:00',end='12:15')]
    person = make_volunteer('Fictional paired volunteer',prefs=prefs,
        quals=(('production_training','verified',None),) if qualified else ())
    session.commit(); session.expire_all()
    ctx = context(session,clock,ConnectedDouble(),tmp_path)
    rule = pairs.stage_rules(session,clock.now(),person,pairs=[{'role_ids':[a.id,b.id]}],
        resolved_constraint_indexes=(0,))
    assert not eligibility.check(session,person,shifts[0])
    assert person.preferences.get('pending_constraints')
    reviewed(session,ctx,rule)
    assert not person.preferences['pending_constraints'] and pairs.rule_problem(session,person) is None
    return person,shifts,ctx,rule


def test_plan_pairs_first_and_second_service_same_dates_with_separate_role_caps(
    session,clock,make_volunteer,make_shift,tmp_path
):
    person,shifts,ctx,_=case(session,clock,make_volunteer,make_shift,tmp_path)
    report=plan_month(ctx,'2026-10')
    assert report['state']=='pending_exact_review' and not report['violations']
    assert len(report['proposals'])==4 and len(report['reviews'])==2
    assert not session.scalar(select(m.Assignment)) and not ctx.provider.sent
    assert plan_month(ctx,'2026-10')['reviews']==report['reviews']
    reviews=[session.get(m.Approval,i) for i in report['reviews']]
    assert all(r.payload['record']=='AssignmentPair' for r in reviews)
    for review in reviews:
        group=review.payload['after']['assignments']
        dates={session.get(m.Shift,c['shift_id']).event.starts_at.astimezone(NOW.tzinfo).date() for c in group}
        assert len(group)==2 and len(dates)==1
        assert review.payload['content_hash']==confirmations.digest(review.payload)
        reviewed(session,ctx,review)
        assert len(review.payload['applied_record_ids'])==2
    rows=session.scalars(select(m.Assignment)).all()
    assert len(rows)==4 and all(r.status=='approved' for r in rows)
    assert {r.shift.event.starts_at.astimezone(NOW.tzinfo).day for r in rows}=={4,11}
    assert scheduler.validate(session,'2026-10')['violations']==[]
    assert scheduler.preview_report(session,'2026-10',[],'America/Denver')['held_constraints']==[]
    assert not ctx.provider.sent


def test_unqualified_partner_yields_clear_hold_and_no_greeter_only_proposal(
    session,clock,make_volunteer,make_shift,tmp_path
):
    person,_,ctx,_=case(session,clock,make_volunteer,make_shift,tmp_path,qualified=False)
    report=plan_month(ctx,'2026-10')
    assert report['proposals']==report['reviews']==[] and report['state']=='held_for_review'
    assert any(h['volunteer_id']==person.id and 'same-date pair' in h['reason'] for h in report['held_constraints'])
    assert not session.scalar(select(m.Assignment)) and not ctx.provider.sent


@pytest.mark.parametrize('change',['consent','qualification','availability','role_requirement','event','rule','timezone','occupied','care'])
def test_changed_source_or_hard_guard_prevents_both_assignments(
    session,clock,make_volunteer,make_shift,tmp_path,assign,change
):
    person,shifts,ctx,_=case(session,clock,make_volunteer,make_shift,tmp_path)
    review=session.get(m.Approval,plan_month(ctx,'2026-10')['reviews'][0])
    def mutate():
        if change=='consent':person.sms_opt_in=False
        if change=='qualification':person.qualifications[0].status='pending'
        if change=='availability':person.preferences={**person.preferences,'recurring_windows':[],'availability_weekdays':[2]}
        if change=='role_requirement':shifts[1].role.required_qualifications=['additional_training']
        if change=='event':shifts[1].event.starts_at+=timedelta(days=1); shifts[1].event.ends_at+=timedelta(days=1)
        if change=='rule':person.preferences={**person.preferences,'same_day_role_pairs':[]}
        if change=='timezone':session.add(m.Policy(key='church_timezone',value={'value':'America/New_York'}))
        if change=='occupied':assign(make_volunteer(),shifts[1])
        if change=='care':session.add(m.Escalation(category='sensitive',severity='normal',summary='Fictional care hold',
            related_ids={'volunteer_id':person.id},status='open',created_at=clock.now()))
    human_change(session,mutate)
    with pytest.raises(ValueError): reviewed(session,ctx,review)
    assert not session.scalar(select(m.Assignment.id).where(m.Assignment.volunteer_id==person.id))
    assert not ctx.provider.sent


def test_pair_is_blocked_by_overlap_and_missing_counterpart_event(
    session,clock,make_volunteer,make_shift,tmp_path
):
    person,shifts,ctx,_=case(session,clock,make_volunteer,make_shift,tmp_path,windows=False)
    def mutate():
        for s in shifts[1::2]:
            s.event.starts_at=s.event.starts_at.astimezone(NOW.tzinfo).replace(hour=9,minute=30)
            s.event.ends_at=s.event.starts_at+timedelta(hours=1)
    human_change(session,mutate)
    assert scheduler.preview_draft(session,clock,'2026-10','America/Denver')==[]
    human_change(session,lambda:[setattr(s.event,'status','cancelled') for s in shifts[1::2]])
    assert scheduler.preview_draft(session,clock,'2026-10','America/Denver')==[]


def test_global_limit_counts_both_pair_placements_without_overriding_role_caps(
    session,clock,make_volunteer,make_shift,tmp_path
):
    _,_,_,_=case(session,clock,make_volunteer,make_shift,tmp_path,global_limit=3)
    choices=scheduler.preview_draft(session,clock,'2026-10','America/Denver')
    assert len(choices)==2 and not scheduler.preview_report(session,'2026-10',choices,'America/Denver')['violations']


def test_greeter_only_role_cap_does_not_become_global_and_still_limits_paired_dates(
    session,clock,make_volunteer,make_shift,tmp_path
):
    person,shifts,ctx,_=case(session,clock,make_volunteer,make_shift,tmp_path)
    def change():
        p={**person.preferences,'role_frequency_caps':[person.preferences['role_frequency_caps'][0]]}
        p.pop('max_per_month')
        person.preferences=p
    human_change(session,change)
    report=plan_month(ctx,'2026-10')
    assert len(report['proposals'])==4 and len(report['reviews'])==2
    assert {session.get(m.Shift,c['shift_id']).event.starts_at.astimezone(NOW.tzinfo).day for c in report['proposals']}=={4,11}


def test_reviewed_calendar_pattern_and_annual_absence_restrict_both_paired_roles(
    session,clock,make_volunteer,make_shift,tmp_path
):
    person,shifts,ctx,_=case(session,clock,make_volunteer,make_shift,tmp_path)
    pattern={'weekday_ordinals':[{'weekday':6,'ordinals':[2,3]}],'annual_unavailable_months':[12]}
    review=stage_pattern_review(session,person,pattern,clock.now())
    reviewed(session,ctx,review)
    choices=scheduler.preview_draft(session,clock,'2026-10','America/Denver')
    assert len(choices)==4
    assert {session.get(m.Shift,c['shift_id']).event.starts_at.astimezone(NOW.tzinfo).day for c in choices}=={11,18}
    assert pairs.rule_problem(session,person) is None
    def move():
        for s in shifts:
            s.event.starts_at=s.event.starts_at.replace(month=12)
            s.event.ends_at=s.event.ends_at.replace(month=12)
    human_change(session,move)
    assert scheduler.preview_draft(session,clock,'2026-12','America/Denver')==[]
    assert 'unavailable annual month' in ' '.join(eligibility.check(session,person,shifts[0]).reasons)


def test_malformed_calendar_pattern_holds_the_whole_pair(session,clock,make_volunteer,make_shift,tmp_path):
    person,_,_,_=case(session,clock,make_volunteer,make_shift,tmp_path)
    human_change(session,lambda:setattr(person,'preferences',{**person.preferences,
        'calendar_patterns':{'annual_unavailable_months':[12]}}))
    assert scheduler.preview_draft(session,clock,'2026-10','America/Denver')==[]


def test_pending_description_and_unreviewed_structured_rule_cannot_establish_eligibility(
    session,clock,make_volunteer,make_shift,tmp_path
):
    a,b=make_shift('Greeter'),make_shift('Production')
    person=make_volunteer(prefs={'pending_constraints':[{'kind':'same_day','role_ids':[a.role_id,b.role_id],
        'description':'Same dates, fictional fixture'}]})
    assert 'coordinator review' in pairs.rule_problem(session,person)
    person.preferences={'same_day_role_pairs':[{'role_ids':[a.role_id,b.role_id]}]}
    assert 'no current source-bound' in pairs.rule_problem(session,person)
    assert not ranking.rank_candidates(session,a,clock.now())


def test_replacement_pool_requires_eligible_existing_same_date_partner(
    session,clock,make_volunteer,make_shift,tmp_path,assign
):
    person,shifts,ctx,_=case(session,clock,make_volunteer,make_shift,tmp_path)
    assert not eligibility.check(session,person,shifts[0])
    assert not ranking.rank_candidates(session,shifts[0],clock.now())
    human_change(session,lambda:assign(person,shifts[1],status='confirmed'))
    assert eligibility.check(session,person,shifts[0])
    assert [c.volunteer.id for c in ranking.rank_candidates(session,shifts[0],clock.now())]==[person.id]
    human_change(session,lambda:setattr(person.qualifications[0],'status','expired'))
    assert not eligibility.check(session,person,shifts[0])
    assert not ranking.rank_candidates(session,shifts[0],clock.now())


def test_gloo_swap_cannot_leave_a_partial_paired_virtual_plan(
    session,clock,make_volunteer,make_shift,tmp_path
):
    person,_,_,_=case(session,clock,make_volunteer,make_shift,tmp_path)
    choices=scheduler.preview_draft(session,clock,'2026-10','America/Denver')
    partial=[choices[0]]
    assert scheduler.preview_report(session,'2026-10',partial,'America/Denver')['violations']


@pytest.mark.parametrize('bad',[[],[1,1],[1,'two'],[True,2],[1,999]])
def test_pair_rule_rejects_ambiguous_or_missing_roles(session,make_shift,bad):
    make_shift('Greeter');make_shift('Production')
    with pytest.raises(ValueError):pairs.normalize(session,[{'role_ids':bad}])


def test_rule_review_detects_changed_profile_without_changing_other_facts(
    session,clock,make_volunteer,make_shift,tmp_path
):
    a,b=make_shift('Greeter'),make_shift('Production')
    person=make_volunteer(prefs={'max_per_month':2,'pending_constraints':[{'kind':'same_day','role_ids':[a.role_id,b.role_id]}]})
    ctx=context(session,clock,ConnectedDouble(),tmp_path)
    before=deepcopy(confirmations.values(person))
    review=pairs.stage_rules(session,clock.now(),person,pairs=[{'role_ids':[a.role_id,b.role_id]}],resolved_constraint_indexes=(0,))
    assert confirmations.values(person)==before
    human_change(session,lambda:setattr(person,'name','Changed fictional name'))
    with pytest.raises(ValueError):reviewed(session,ctx,review)
    assert 'same_day_role_pairs' not in person.preferences


def test_pair_review_never_resolves_a_different_pending_constraint(session,clock,make_volunteer,make_shift):
    a,b=make_shift('Greeter'),make_shift('Production')
    person=make_volunteer(prefs={'pending_constraints':[{'kind':'excluded_months','months':[12]}]})
    with pytest.raises(ValueError,match='only resolve'):
        pairs.stage_rules(session,clock.now(),person,pairs=[{'role_ids':[a.role_id,b.role_id]}],resolved_constraint_indexes=(0,))
    assert person.preferences['pending_constraints'] and not session.scalar(select(m.Approval))


@pytest.mark.parametrize('changed',[False,True])
def test_existing_authenticated_review_api_publishes_the_pair_or_rolls_back_both(planning_client,changed):
    client,app,_=planning_client
    with app.state.session_factory() as session:
        session.info['record_authorized']=True
        person=session.scalar(select(m.Volunteer).order_by(m.Volunteer.id))
        a=m.Role(name='Greeter',ministry='Fictional',required_qualifications=[],criticality='standard',fill_policy='auto')
        b=m.Role(name='Production',ministry='Fictional',required_qualifications=[],criticality='standard',fill_policy='auto')
        session.add_all([a,b]);session.flush()
        for role,hour in ((a,9),(b,11)):
            event=m.Event(title='Demo: fictional pair',status='scheduled',starts_at=NOW.replace(day=4,hour=hour),
                          ends_at=NOW.replace(day=4,hour=hour)+timedelta(minutes=75))
            session.add(event);session.flush()
            session.add(m.Shift(event_id=event.id,role_id=role.id,slot_index=0))
        person.preferences={'onboarding_stage':'complete','max_per_month':None,
            'role_frequency_caps':[{'role_id':r.id,'role_name':r.name,'max_per_month':2} for r in (a,b)]}
        session.flush()
        review=pairs.stage_rules(session,app.state.clock.now(),person,pairs=[{'role_ids':[a.id,b.id]}])
        rule_id,rule_hash=review.id,review.payload['content_hash']
        session.commit()
    approved=client.post(f'/api/proposals/{rule_id}/approve',json={'content_hash':rule_hash})
    assert approved.status_code==200,approved.text
    with app.state.session_factory() as session:
        from app.agents.fill_agent import FillContext
        ctx=FillContext(session,app.state.clock,app.state.provider,app.state.gloo)
        report=plan_month(ctx,'2026-10')
        assert len(report['reviews'])==1
        review=session.get(m.Approval,report['reviews'][0])
        ident,content_hash=review.id,review.payload['content_hash']
        if changed:
            session.info['record_authorized']=True
            session.scalar(select(m.Volunteer).order_by(m.Volunteer.id)).sms_opt_in=False
        session.commit()
    response=client.post(f'/api/proposals/{ident}/approve',json={'content_hash':content_hash})
    assert response.status_code==(409 if changed else 200),response.text
    with app.state.session_factory() as session:
        rows=session.scalars(select(m.Assignment)).all()
        assert len(rows)==(0 if changed else 2)
        assert session.get(m.Approval,ident).status==('pending' if changed else 'approved')
        assert session.scalar(select(m.Message)) is None
