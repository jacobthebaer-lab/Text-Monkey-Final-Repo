"""Fictional role-scoped calendar restrictions through real eligibility consumers."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.core import eligibility, ranking, scheduler
from app.core.recurring_availability import merge_recurring_windows, normalize_recurring_windows
from app.db import models as m
from tests.conftest import DENVER
from tests.test_recurring_availability import window


def local(year, month, day, hour=18):
    return datetime(year, month, day, hour, tzinfo=DENVER)


def childcare_setup(session, make_shift, make_volunteer, **scope):
    shift = make_shift('Fictional Childcare', starts=local(2026, 12, 9), minutes=120,
                       required=['fictional_child_safety'], title="Fictional Women's Group")
    group = m.EventType(name="Fictional Women's Group", title_patterns=[])
    session.add(group)
    session.flush()
    shift.event.event_type_id = group.id
    restriction = {**window(shift.role, 2, '18:00', '20:00',
        {'label': "women's group", 'event_type_ids': [group.id]}), 'month_ordinals': [2], **scope}
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete',
        'recurring_windows': [restriction]}, quals=[('fictional_child_safety', 'verified', None)])
    return volunteer, shift, group


@pytest.mark.parametrize('day,allowed', [(2, False), (9, True), (16, False), (23, False), (30, False)])
def test_second_wednesday_is_executed_not_every_wednesday(session, make_shift, make_volunteer, day, allowed):
    volunteer, original, group = childcare_setup(session, make_shift, make_volunteer)
    shift = make_shift(original.role.name, starts=local(2026, 12, day), minutes=120)
    shift.event.event_type_id = group.id
    assert eligibility.check(session, volunteer, shift).eligible is allowed


def test_ordinal_does_not_replace_role_group_hours_or_admin_clearance(session, make_shift, make_volunteer):
    volunteer, shift, group = childcare_setup(session, make_shift, make_volunteer)
    assert eligibility.check(session, volunteer, shift)
    unrelated = make_shift(shift.role.name, starts=shift.event.starts_at, minutes=120)
    assert not eligibility.check(session, volunteer, unrelated)
    wrong_role = make_shift('Fictional Hospitality', starts=shift.event.starts_at, minutes=120)
    wrong_role.event.event_type_id = group.id
    assert not eligibility.check(session, volunteer, wrong_role)
    too_long = make_shift(shift.role.name, starts=shift.event.starts_at, minutes=121)
    too_long.event.event_type_id = group.id
    assert not eligibility.check(session, volunteer, too_long)
    unqualified = make_volunteer(prefs=deepcopy(volunteer.preferences))
    assert 'missing qualification: fictional_child_safety' in eligibility.check(session, unqualified, shift).reasons
    assert not unqualified.qualifications and not unqualified.assignments and not unqualified.is_coordinator


def test_greeter_and_production_keep_all_sundays_and_independent_two_per_month_caps(
    session, make_shift, make_volunteer, assign
):
    volunteer, childcare, _ = childcare_setup(session, make_shift, make_volunteer)
    greeter = make_shift('Fictional Greeter', starts=local(2026, 12, 6, 9))
    production = make_shift('Fictional Production', starts=local(2026, 12, 6, 11))
    windows = deepcopy(volunteer.preferences['recurring_windows'])
    for role in (greeter.role, production.role):
        windows.extend([window(role, start='09:00', end='10:15'),
                        window(role, start='11:00', end='12:15')])
    volunteer.preferences = {**volunteer.preferences, 'recurring_windows': windows,
        'role_frequency_caps': [{'role_id': role.id, 'role_name': role.name, 'max_per_month': 2}
                                for role in (greeter.role, production.role)]}
    for day in (6, 13, 20, 27):
        for role_name, hour in ((greeter.role.name, 9), (production.role.name, 11)):
            assert eligibility.check(session, volunteer, make_shift(role_name, starts=local(2026, 12, day, hour)))
    for day in (6, 13):
        assign(volunteer, make_shift(greeter.role.name, starts=local(2026, 12, day, 9)), status='completed')
    assert not eligibility.check(session, volunteer, greeter)
    assert eligibility.check(session, volunteer, production)
    assert eligibility.check(session, volunteer, childcare)


def test_candidate_selection_and_assignment_recheck_hold_nonsecond_occurrence(
    session, clock, make_shift, make_volunteer
):
    volunteer, valid, group = childcare_setup(session, make_shift, make_volunteer)
    invalid = make_shift(valid.role.name, starts=local(2026, 12, 16), minutes=120)
    invalid.event.event_type_id = group.id
    assert [candidate.volunteer.id for candidate in ranking.rank_candidates(session, valid, clock.now())] == [volunteer.id]
    assert ranking.rank_candidates(session, invalid, clock.now()) == []
    assert scheduler.propose(session, clock, invalid, volunteer, 'America/Denver')['error'] == 'ineligible'
    assert not volunteer.assignments


def test_fifth_weekday_has_no_fourth_week_fallback(session, make_shift, make_volunteer):
    volunteer, _, group = childcare_setup(session, make_shift, make_volunteer, month_ordinals=[5])
    dec_fifth = make_shift('Fictional Childcare', starts=local(2026, 12, 30), minutes=120)
    dec_fifth.event.event_type_id = group.id
    assert eligibility.check(session, volunteer, dec_fifth)
    for day in (3, 10, 17, 24):
        feb = make_shift('Fictional Childcare', starts=local(2027, 2, day), minutes=120)
        feb.event.event_type_id = group.id
        assert not eligibility.check(session, volunteer, feb)


def test_ordinal_and_dated_month_use_church_local_start_not_utc(session, make_shift, make_volunteer):
    volunteer, shift, _ = childcare_setup(session, make_shift, make_volunteer, months=['2026-12'])
    # Thursday UTC is the second Wednesday evening in the configured church zone.
    shift.event.starts_at = datetime(2026, 12, 10, 1, tzinfo=timezone.utc)
    shift.event.ends_at = datetime(2026, 12, 10, 3, tzinfo=timezone.utc)
    assert eligibility.check(session, volunteer, shift, tz='America/Denver')
    assert not eligibility.check(session, volunteer, shift, tz='UTC')


@pytest.mark.parametrize('year,month,day,allowed', [(2026, 12, 9, True), (2027, 1, 13, False), (2027, 12, 8, False)])
def test_positive_dated_month_scope_does_not_recur_annually(
    session, make_shift, make_volunteer, year, month, day, allowed
):
    volunteer, _, group = childcare_setup(session, make_shift, make_volunteer, months=['2026-12'])
    shift = make_shift('Fictional Childcare', starts=local(year, month, day), minutes=120)
    shift.event.event_type_id = group.id
    assert eligibility.check(session, volunteer, shift).eligible is allowed


def test_december_2026_off_is_dated_exclusion_not_positive_or_annual_month_scope(
    session, make_shift, make_volunteer
):
    volunteer, dec2026, group = childcare_setup(session, make_shift, make_volunteer)
    session.add(m.Availability(volunteer_id=volunteer.id, month='2026-12',
        unavailable_dates=[f'2026-12-{day:02}' for day in range(1, 32)]))
    session.flush()
    assert 'said unavailable on 2026-12-09' in eligibility.check(session, volunteer, dec2026).reasons
    assert 'months' not in volunteer.preferences['recurring_windows'][0]
    dec2027 = make_shift('Fictional Childcare', starts=local(2027, 12, 8), minutes=120)
    dec2027.event.event_type_id = group.id
    assert eligibility.check(session, volunteer, dec2027)


def test_named_group_event_follow_still_respects_ordinal_and_month_scope(session, make_shift, make_volunteer):
    volunteer, _, group = childcare_setup(session, make_shift, make_volunteer, months=['2026-12'])
    restriction = deepcopy(volunteer.preferences['recurring_windows'][0])
    restriction.update(time_mode='event', start_time=None, end_time=None)
    volunteer.preferences = {**volunteer.preferences, 'recurring_windows': [restriction]}
    for month, day, allowed in ((12, 9, True), (12, 16, False), (11, 11, False)):
        shift = make_shift('Fictional Childcare', starts=local(2026, month, day, 10), minutes=90)
        shift.event.event_type_id = group.id
        assert eligibility.check(session, volunteer, shift).eligible is allowed


def test_frequency_and_clock_corrections_preserve_omitted_optional_constraints(
    session, make_shift, make_volunteer
):
    volunteer, shift, group = childcare_setup(session, make_shift, make_volunteer, months=['2026-12'])
    previous = deepcopy(volunteer.preferences)
    roles = [shift.role]
    assert merge_recurring_windows({'max_per_month': 2}, previous, roles, [group]) == previous['recurring_windows']
    reemitted = deepcopy(previous['recurring_windows'])
    reemitted[0].pop('month_ordinals')
    reemitted[0].pop('months')
    reemitted[0].update(start_time='18:30', end_time='19:30')
    reemitted[0]['event_context']['label'] = "Fictional Women's Group"
    merged = merge_recurring_windows({'recurring_windows': reemitted}, previous, roles, [group])
    assert merged[0]['month_ordinals'] == [2] and merged[0]['months'] == ['2026-12']
    assert merged[0]['start_time'] == '18:30'
    assert previous == volunteer.preferences and 'month_ordinals' not in reemitted[0]
    explicit = {**merged[0], 'month_ordinals': [1, 2, 3, 4, 5]}
    assert merge_recurring_windows({'recurring_windows': [explicit]}, previous, roles, [group])[0]['month_ordinals'] == [1, 2, 3, 4, 5]
    assert merge_recurring_windows({'recurring_windows': []}, previous, roles, [group]) == []


def test_ambiguous_scope_cannot_be_dropped_when_windows_are_collapsed(session, make_shift, make_volunteer):
    volunteer, shift, group = childcare_setup(session, make_shift, make_volunteer)
    second = volunteer.preferences['recurring_windows'][0]
    previous = {'recurring_windows': [second, {**second, 'month_ordinals': [4]}]}
    reemitted = deepcopy(second)
    reemitted.pop('month_ordinals')
    with pytest.raises(ValueError, match='unambiguous source window'):
        merge_recurring_windows({'recurring_windows': [reemitted]}, previous, [shift.role], [group])


@pytest.mark.parametrize('bad', [
    {'month_ordinals': []}, {'month_ordinals': [True]}, {'month_ordinals': [0]},
    {'month_ordinals': [6]}, {'month_ordinals': ['2']}, {'month_ordinals': 2},
    {'month_ordinals': [1, 2, 3, 4, 5, 5]}, {'months': []}, {'months': '2026-12'},
    {'months': ['2026-2']}, {'months': ['0000-12']}, {'months': ['2026-13']},
    {'months': [True]}, {'months': ['2026-12'] * 13}, {'monthly_ordinal': 2},
])
def test_invalid_calendar_scope_fails_closed(session, make_shift, make_volunteer, bad):
    volunteer, shift, group = childcare_setup(session, make_shift, make_volunteer)
    malformed = [{**volunteer.preferences['recurring_windows'][0], **bad}]
    with pytest.raises(ValueError):
        normalize_recurring_windows(malformed, [shift.role], [group])
    volunteer.preferences = {**volunteer.preferences, 'recurring_windows': malformed}
    assert not eligibility.check(session, volunteer, shift)


def test_unrestricted_historical_windows_and_profile_serialization_keep_schema_compatibility(
    session, make_shift, make_volunteer
):
    from app.core.profile_sync import window_snapshot
    volunteer, shift, group = childcare_setup(session, make_shift, make_volunteer,
                                             month_ordinals=[3, 2, 2], months=['2026-12', '2026-11'])
    snapshot = window_snapshot(session, volunteer.preferences['recurring_windows'])
    assert snapshot[0]['month_ordinals'] == [2, 3] and snapshot[0]['months'] == ['2026-11', '2026-12']
    assert snapshot[0]['role_names'] == [shift.role.name]
    assert snapshot[0]['event_context']['event_type_names'] == [group.name]
    historical = window(shift.role, 2, '18:00', '20:00',
                        {'label': "women's group", 'event_type_ids': [group.id]})
    assert normalize_recurring_windows([historical], [shift.role], [group]) == [historical]
    volunteer.preferences = {**volunteer.preferences, 'recurring_windows': [historical]}
    third = make_shift(shift.role.name, starts=local(2026, 12, 16), minutes=120)
    third.event.event_type_id = group.id
    assert eligibility.check(session, volunteer, third)
