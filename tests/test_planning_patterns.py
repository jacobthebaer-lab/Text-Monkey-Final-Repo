"""Real calendar and recorded-event contracts; no model or native calls."""
from copy import deepcopy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import select

from app.core import confirmations
from app.core.planning_patterns import (KEY,normalize_patterns,calendar_dates,calendar_reasons,
    learned_patterns,stage_pattern_review,seasonal_staffing_report)
from app.db import models as m

ZONE=ZoneInfo('America/Denver')
PATTERN={'weekday_ordinals':[{'weekday':6,'ordinals':[2,4]}],'annual_unavailable_months':[12]}


def event(session,when,*,status='completed',count=1,event_type=None,role=None):
    if role is None:
        role=session.scalar(select(m.Role).where(m.Role.name=='Greeter'))
        if role is None:
            role=m.Role(name='Greeter',ministry='Welcome',criticality='standard',fill_policy='auto',required_qualifications=[])
            session.add(role);session.flush()
    row=m.Event(title='Synthetic service',event_type_id=event_type.id if event_type else None,
        starts_at=when,ends_at=when+timedelta(hours=1),status=status)
    session.add(row);session.flush()
    for index in range(count):
        session.add(m.Shift(event_id=row.id,role_id=role.id,slot_index=index))
    session.flush();return row


def test_second_fourth_sundays_and_annual_absence_apply_to_future_years():
    assert calendar_dates(PATTERN,'2026-11')==['2026-11-08','2026-11-22']
    assert calendar_dates(PATTERN,'2026-12')==[]
    assert calendar_dates(PATTERN,'2027-12')==[]
    assert calendar_dates(PATTERN,'2027-01')==['2027-01-10','2027-01-24']
    assert calendar_reasons({KEY:PATTERN},datetime(2027,1,3,9,tzinfo=ZONE))


def test_fifth_weekday_and_leap_calendar_have_no_invented_dates():
    pattern={'weekday_ordinals':[{'weekday':1,'ordinals':[5]}],'annual_unavailable_months':[]}
    assert calendar_dates(pattern,'2028-02')==['2028-02-29']
    assert calendar_dates(pattern,'2027-02')==[]


def test_event_local_day_owns_pattern_at_utc_boundary():
    pattern={'weekday_ordinals':[{'weekday':6,'ordinals':[2]}],'annual_unavailable_months':[]}
    # UTC Monday is still the second Sunday in Denver.
    assert calendar_reasons({KEY:pattern},datetime(2026,11,9,1,tzinfo=ZoneInfo('UTC')))==[]
    assert calendar_reasons({KEY:pattern},datetime(2026,11,9,9,tzinfo=ZoneInfo('UTC')))


def test_annual_absence_alone_does_not_declare_positive_availability():
    pattern={'weekday_ordinals':[],'annual_unavailable_months':[12]}
    assert calendar_dates(pattern,'2026-12')==[]
    with pytest.raises(ValueError,match='does not establish positive'):calendar_dates(pattern,'2027-01')
    assert calendar_reasons({KEY:pattern},datetime(2027,1,10,9,tzinfo=ZONE))==[]


@pytest.mark.parametrize('bad',[{},None,{'weekday_ordinals':[{'weekday':True,'ordinals':[2]}],'annual_unavailable_months':[]},
    {'weekday_ordinals':[{'weekday':6,'ordinals':[0]}],'annual_unavailable_months':[]},
    {'weekday_ordinals':[{'weekday':6,'ordinals':[2,2]}],'annual_unavailable_months':[]},
    {'weekday_ordinals':[],'annual_unavailable_months':[True]},
    {'weekday_ordinals':[],'annual_unavailable_months':[13]}])
def test_malformed_calendar_preferences_fail_closed(bad):
    with pytest.raises(ValueError):normalize_patterns(bad)
    assert calendar_reasons({KEY:bad},datetime(2026,11,8,9,tzinfo=ZONE))


def test_history_proposes_review_and_never_grants_consent_or_autobooks(session,clock,make_volunteer):
    person=make_volunteer(opt_in=False,status='inactive',prefs={KEY:deepcopy(PATTERN),'notes':'Keep this'})
    before=deepcopy(person.preferences)
    for when in [datetime(2026,7,12,9,tzinfo=ZONE),datetime(2026,8,9,9,tzinfo=ZONE)]:
        row=event(session,when,count=2)
        for shift in row.shifts:
            session.add(m.Assignment(shift_id=shift.id,volunteer_id=person.id,status='completed',source='admin',
                created_at=when,updated_at=when))
    session.flush()
    report=learned_patterns(session,person,clock.now())
    assert report['requires_review'] and not report['applied']
    assert report['proposal']['weekday_ordinals']==[{'weekday':6,'ordinals':[2]}]
    assert report['proposal']['annual_unavailable_months']==[12]
    assert not report['unavailable_months_inferred']
    assert person.preferences==before and not person.sms_opt_in and person.status=='inactive'
    assert not session.scalar(select(m.Message)) and not session.scalar(select(m.Approval))


def test_one_month_duplicate_services_and_noncompleted_history_do_not_establish_rhythm(session,clock,make_volunteer):
    person=make_volunteer()
    for status,event_status,when in [('completed','completed',datetime(2026,9,13,9,tzinfo=ZONE)),
        ('confirmed','completed',datetime(2026,8,9,9,tzinfo=ZONE)),
        ('completed','cancelled',datetime(2026,7,12,9,tzinfo=ZONE))]:
        row=event(session,when,count=3,status=event_status)
        for shift in row.shifts:
            session.add(m.Assignment(shift_id=shift.id,volunteer_id=person.id,status=status,source='admin',created_at=when,updated_at=when))
    session.flush()
    assert learned_patterns(session,person,clock.now())['proposal'] is None


def test_existing_exact_record_review_is_idempotent_and_preserves_privileges_and_unrelated_preferences(session,clock,make_volunteer):
    person=make_volunteer(opt_in=False,status='inactive',prefs={'notes':'Keep','any_role':False},coordinator=True)
    before=confirmations.values(person)
    approval=stage_pattern_review(session,person,PATTERN,clock.now())
    assert approval.status=='pending' and confirmations.valid(approval,clock.now())
    assert confirmations.values(person)==before
    assert stage_pattern_review(session,person,PATTERN,clock.now()).id==approval.id
    approval.status='approved'
    confirmations.apply_record(session,approval,clock.now())
    assert person.preferences[KEY]==PATTERN and person.preferences['notes']=='Keep'
    assert person.is_coordinator and not person.sms_opt_in and person.status=='inactive'
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Qualification))


def test_review_stale_profile_cannot_overwrite_a_newer_sender_preference(session,clock,make_volunteer):
    person=make_volunteer(prefs={'notes':'Original'})
    approval=stage_pattern_review(session,person,PATTERN,clock.now())
    person.preferences={'notes':'New actual preference'};session.flush();approval.status='approved'
    with pytest.raises(ValueError,match='changed since review'):confirmations.apply_record(session,approval,clock.now())


def test_seasonal_recipe_recommendation_uses_actual_same_type_role_slots_from_two_years(session,clock):
    kind=m.EventType(name='Holiday service',title_patterns=[]);session.add(kind);session.flush()
    past=[event(session,datetime(year,12,15,9,tzinfo=ZONE),count=count,event_type=kind)
        for year,count in [(2024,2),(2025,4)]]
    upcoming=event(session,datetime(2026,12,15,9,tzinfo=ZONE),count=1,status='scheduled',event_type=kind)
    original=len(session.scalars(select(m.Shift)).all())
    report=seasonal_staffing_report(session,'2026-12',clock.now())
    proposal=report['recommendations'][0]
    assert report['requires_review'] and not report['staffing_changed']
    assert proposal['event_id']==upcoming.id and proposal['current_slots']==1
    assert proposal['observed_median_slots']==3 and proposal['review_difference']==2
    assert {row['event_id'] for row in proposal['evidence']}=={row.id for row in past}
    assert len(session.scalars(select(m.Shift)).all())==original
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Message))


def test_unknown_cancelled_other_type_and_single_year_data_never_fabricate_staffing(session,clock):
    kind=m.EventType(name='Holiday',title_patterns=[]);other=m.EventType(name='Other',title_patterns=[])
    session.add_all([kind,other]);session.flush()
    event(session,datetime(2025,12,15,9,tzinfo=ZONE),count=4,event_type=kind)
    event(session,datetime(2024,12,15,9,tzinfo=ZONE),count=10,event_type=kind,status='cancelled')
    event(session,datetime(2024,12,20,9,tzinfo=ZONE),count=10,event_type=other)
    future=event(session,datetime(2026,12,15,9,tzinfo=ZONE),count=1,event_type=kind,status='scheduled')
    unknown=event(session,datetime(2026,12,16,9,tzinfo=ZONE),count=1,status='scheduled')
    report=seasonal_staffing_report(session,'2026-12',clock.now())
    assert report['recommendations']==[] and report['insufficient_history_event_ids']==[future.id]
    assert report['unknown_event_ids']==[unknown.id]


def test_generic_existing_monthly_engine_already_expands_ordinal_sundays(session,clock,provider,make_volunteer):
    from app.agents.planning_agent import record_availability
    person=make_volunteer()
    ctx=type('Context',(),{'session':session,'clock':clock})()
    parsed=type('Parsed',(),{'dates':[]})()
    report=record_availability(ctx,person,parsed,'second and fourth Sundays',month='2026-11')
    assert report['available']==calendar_dates({**PATTERN,'annual_unavailable_months':[]},'2026-11')
