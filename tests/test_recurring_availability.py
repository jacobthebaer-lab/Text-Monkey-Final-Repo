"""Fictional role windows: state validation and actual candidate eligibility."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.core import eligibility
from app.core.recurring_availability import merge_recurring_windows, normalize_recurring_windows
from app.db import models as m
from tests.conftest import NOW


def window(role, weekday=6, start='08:00', end='10:00', context=None):
    return {'weekday': weekday, 'role_ids': [role.id], 'role_label': role.name,
            'any_role': False, 'start_time': start, 'end_time': end,
            'all_day': False, 'event_context': context}


def setup(session, make_shift, make_volunteer):
    sunday = NOW.replace(day=4, hour=8)
    greeting = make_shift('Greeter', starts=sunday, minutes=120)
    coffee = make_shift('Coffee', starts=NOW.replace(day=7, hour=9), minutes=60,
                        title="Fictional Men's Group")
    group = m.EventType(name="Fictional Men's Group", title_patterns=[])
    session.add(group)
    session.flush()
    coffee.event.event_type_id = group.id
    windows = [window(greeting.role), window(coffee.role, 2, None, None,
               {'label': "men's group", 'event_type_ids': [group.id]})]
    prefs = {'onboarding_stage': 'complete', 'interested_roles': ['Greeter', 'Coffee'],
             'recurring_windows': windows, 'max_per_month': 2}
    return make_volunteer('Fictional Window Volunteer', prefs=prefs), greeting, coffee, group


def test_mixed_role_window_snapshot_retains_unspecified_group_hours_and_missing_frequency(session, make_shift, make_volunteer):
    v, greeting, coffee, group = setup(session, make_shift, make_volunteer)
    original = deepcopy(v.preferences['recurring_windows'])
    result = normalize_recurring_windows(original, [greeting.role, coffee.role], [group])
    assert result == original
    assert result[1]['start_time'] is None and not result[1]['all_day']
    assert result[1]['event_context']['event_type_ids'] == [group.id]
    assert all('max_per_month' not in w for w in result)
    assert not v.qualifications and not v.is_coordinator


def test_frequency_followup_preserves_windows_and_explicit_correction_replaces_snapshot(session, make_shift, make_volunteer):
    v, greeting, coffee, group = setup(session, make_shift, make_volunteer)
    previous = {'recurring_windows': v.preferences['recurring_windows']}
    roles = [greeting.role, coffee.role]
    retained = merge_recurring_windows({'max_per_month': 3}, previous, roles, [group])
    assert retained == previous['recurring_windows'] and retained is not previous['recurring_windows']
    retained[0]['weekday'] = 5
    corrected = merge_recurring_windows({'recurring_windows': retained}, previous, roles, [group])
    assert corrected[0]['weekday'] == 5 and corrected[1] == previous['recurring_windows'][1]
    assert previous['recurring_windows'][0]['weekday'] == 6
    assert merge_recurring_windows({'recurring_windows': []}, previous, roles, [group]) == []


@pytest.mark.parametrize('hour,minute,duration,allowed', [
    (8, 0, 120, True), (9, 0, 60, True), (10, 0, 60, False),
    (9, 30, 60, False), (7, 59, 1, False), (8, 0, 121, False),
])
def test_entire_sunday_interval_must_fit_without_inventing_ten_am_availability(
    session, make_shift, make_volunteer, hour, minute, duration, allowed
):
    v, _, _, _ = setup(session, make_shift, make_volunteer)
    shift = make_shift('Greeter', starts=NOW.replace(day=4, hour=hour, minute=minute), minutes=duration)
    assert eligibility.check(session, v, shift).eligible is allowed


def test_group_and_role_context_never_becomes_every_wednesday_or_every_role(session, make_shift, make_volunteer):
    v, greeting, coffee, group = setup(session, make_shift, make_volunteer)
    windows = deepcopy(v.preferences['recurring_windows'])
    windows[1].update(start_time='09:00', end_time='10:00')
    v.preferences = {**v.preferences, 'recurring_windows': windows}
    assert eligibility.check(session, v, greeting)
    assert eligibility.check(session, v, coffee)
    wrong_group = make_shift('Coffee', starts=coffee.event.starts_at, minutes=60, title='Unrelated Bible Study')
    assert not eligibility.check(session, v, wrong_group)
    wrong_role = make_shift('Greeter', starts=coffee.event.starts_at, minutes=60)
    wrong_role.event.event_type_id = group.id
    assert not eligibility.check(session, v, wrong_role)
    wrong_day = make_shift('Coffee', starts=coffee.event.starts_at+timedelta(days=1), minutes=60)
    wrong_day.event.event_type_id = group.id
    assert not eligibility.check(session, v, wrong_day)


def test_candidate_selection_and_assignment_write_reject_ten_to_eleven_event(session, clock, make_shift, make_volunteer):
    from app.core import ranking, scheduler
    v, greeting, _, _ = setup(session, make_shift, make_volunteer)
    late = make_shift('Greeter', starts=NOW.replace(day=4, hour=10), minutes=60)
    assert [c.volunteer.id for c in ranking.rank_candidates(session, greeting, clock.now())] == [v.id]
    assert ranking.rank_candidates(session, late, clock.now()) == []
    result = scheduler.propose(session, clock, late, v, 'America/Denver')
    assert result['error'] == 'ineligible' and not v.assignments


def test_unresolved_context_and_unknown_role_labels_are_preserved_but_hold_eligibility(session, make_shift, make_volunteer):
    v, greeting, coffee, group = setup(session, make_shift, make_volunteer)
    windows = deepcopy(v.preferences['recurring_windows'])
    windows[1].update(start_time='09:00', end_time='10:00')
    windows[1]['event_context']['event_type_ids'] = []
    v.preferences = {**v.preferences, 'recurring_windows': normalize_recurring_windows(
        windows, [greeting.role, coffee.role], [group])}
    assert not eligibility.check(session, v, coffee)
    assert eligibility.check(session, v, greeting)
    windows[0]['role_ids'] = []
    windows[0]['role_label'] = 'An unmapped welcoming role'
    v.preferences = {**v.preferences, 'recurring_windows': normalize_recurring_windows(
        windows, [greeting.role, coffee.role], [group])}
    assert not eligibility.check(session, v, greeting)


def test_local_timezone_and_subminute_end_boundary_are_enforced(session, make_shift, make_volunteer):
    v, greeting, _, _ = setup(session, make_shift, make_volunteer)
    greeting.event.starts_at = greeting.event.starts_at.astimezone(timezone.utc)
    greeting.event.ends_at = greeting.event.ends_at.astimezone(timezone.utc)
    assert eligibility.check(session, v, greeting, tz='America/Denver')
    greeting.event.ends_at += timedelta(seconds=1)
    assert not eligibility.check(session, v, greeting, tz='America/Denver')


@pytest.mark.parametrize('hour,duration', [(2, 60), (20, 180), (9, 60)])
def test_unspecified_group_hours_remain_held_after_frequency_followup(
    session, clock, make_shift, make_volunteer, hour, duration
):
    from app.core import ranking, scheduler
    v, greeting, coffee, group = setup(session, make_shift, make_volunteer)
    merged = merge_recurring_windows({'max_per_month': 3}, v.preferences,
                                     [greeting.role, coffee.role], [group])
    v.preferences = {**v.preferences, 'max_per_month': 3, 'recurring_windows': merged}
    coffee.event.starts_at = NOW.replace(day=7, hour=hour)
    coffee.event.ends_at = coffee.event.starts_at + timedelta(minutes=duration)
    assert merged[1]['start_time'] is None and not merged[1]['all_day']
    assert not eligibility.check(session, v, coffee)
    assert ranking.rank_candidates(session, coffee, clock.now()) == []
    assert scheduler.propose(session, clock, coffee, v, 'America/Denver')['error'] == 'ineligible'
    assert not v.assignments
    explicit = deepcopy(merged)
    explicit[1]['all_day'] = True
    v.preferences = {**v.preferences, 'recurring_windows': explicit}
    assert eligibility.check(session, v, coffee)


@pytest.mark.parametrize('all_day', [False, True])
def test_fall_back_interval_is_held_for_timed_windows_but_explicit_all_day_is_safe(
    session, make_shift, make_volunteer, all_day
):
    # 01:30 MDT -> 01:40 MST passes endpoint-only checks despite a 70-minute span.
    v, greeting, _, _ = setup(session, make_shift, make_volunteer)
    start = datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)
    shift = make_shift('Greeter', starts=start, minutes=70)
    restriction = window(greeting.role, start='01:30', end='01:45')
    if all_day:
        restriction.update(start_time=None, end_time=None, all_day=True)
    v.preferences = {**v.preferences, 'recurring_windows': [restriction]}
    assert eligibility.check(session, v, shift).eligible is all_day


@pytest.mark.parametrize('hour', [7, 8])
def test_single_offset_fall_back_intervals_still_fit_explicit_hours(session, make_shift, make_volunteer, hour):
    v, greeting, _, _ = setup(session, make_shift, make_volunteer)
    shift = make_shift('Greeter', starts=datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc), minutes=10)
    v.preferences = {**v.preferences, 'recurring_windows': [window(greeting.role, start='01:30', end='01:45')]}
    assert eligibility.check(session, v, shift)


def test_all_day_uses_actual_elapsed_order_when_fall_back_local_end_precedes_start(session, make_shift, make_volunteer):
    v, greeting, _, _ = setup(session, make_shift, make_volunteer)
    shift = make_shift('Greeter', starts=datetime(2026, 11, 1, 7, 50, tzinfo=timezone.utc), minutes=20)
    restriction = {**window(greeting.role, start=None, end=None), 'all_day': True}
    v.preferences = {**v.preferences, 'recurring_windows': [restriction]}
    assert eligibility.check(session, v, shift)


def test_windows_do_not_bypass_pending_signup_qualifications_or_explicit_date_exclusions(session, make_shift, make_volunteer):
    v, greeting, _, _ = setup(session, make_shift, make_volunteer)
    greeting.role.required_qualifications = ['background_check']
    v.preferences = {**v.preferences, 'onboarding_stage': 'availability'}
    session.add(m.Availability(volunteer_id=v.id, month='2026-10', unavailable_dates=['2026-10-04']))
    session.flush()
    result = eligibility.check(session, v, greeting)
    assert 'text signup is not finished' in result.reasons
    assert 'missing qualification: background_check' in result.reasons
    assert 'said unavailable on 2026-10-04' in result.reasons
    assert not v.qualifications and not v.is_coordinator and not v.assignments


@pytest.mark.parametrize('bad', [
    {'weekday': True}, {'weekday': 7}, {'role_ids': [True]}, {'role_ids': [999]},
    {'any_role': True}, {'all_day': True}, {'start_time': '8am'},
    {'start_time': '10:00', 'end_time': '08:00'}, {'end_time': None},
    {'end_time': '25:00'}, {'event_context': {'label': 'Group', 'event_type_ids': [999]}},
    {'event_context': {'label': '', 'event_type_ids': []}},
])
def test_malformed_or_invented_window_never_becomes_eligible(session, make_shift, make_volunteer, bad):
    v, greeting, coffee, group = setup(session, make_shift, make_volunteer)
    malformed = [{**window(greeting.role), **bad}]
    with pytest.raises(ValueError):
        normalize_recurring_windows(malformed, [greeting.role, coffee.role], [group])
    v.preferences = {**v.preferences, 'recurring_windows': malformed}
    assert not eligibility.check(session, v, greeting)


def test_windows_are_authoritative_over_legacy_service_enum_and_clear_returns_to_legacy_rules(session, make_shift, make_volunteer):
    v, greeting, _, _ = setup(session, make_shift, make_volunteer)
    v.preferences = {**v.preferences, 'availability_weekdays': [2], 'preferred_services': ['sun_10'],
                     'interested_roles': ['Coffee']}
    assert eligibility.check(session, v, greeting)
    v.preferences = {**v.preferences, 'recurring_windows': []}
    assert not eligibility.check(session, v, greeting)


def test_full_day_midnight_boundary_does_not_cover_overnight_without_explicit_window(session, make_shift, make_volunteer):
    v, greeting, coffee, group = setup(session, make_shift, make_volunteer)
    all_day = {**window(greeting.role, start=None, end=None), 'all_day': True}
    v.preferences = {**v.preferences, 'recurring_windows': normalize_recurring_windows(
        [all_day], [greeting.role, coffee.role], [group])}
    until_midnight = make_shift('Greeter', starts=NOW.replace(day=4, hour=23), minutes=60)
    assert eligibility.check(session, v, until_midnight)
    until_midnight.event.ends_at += timedelta(minutes=1)
    assert not eligibility.check(session, v, until_midnight)


@pytest.mark.parametrize('bad', [None, 'sun_8-10', {}, [None]])
def test_corrupt_saved_window_data_fails_closed(session, make_shift, make_volunteer, bad):
    v = make_volunteer(prefs={'recurring_windows': bad})
    assert not eligibility.check(session, v, make_shift('Greeter'))
