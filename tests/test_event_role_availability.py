"""Fictional normalized mixed-role replies; no Gloo, transport or live DB."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core import eligibility
from app.core.recurring_availability import (global_frequency_limit, merge_recurring_windows,
    merge_role_frequency_caps, normalize_recurring_windows, normalize_role_frequency_caps)
from app.db import models as m
from tests.conftest import NOW
from tests.test_recurring_availability import setup, window


def event_preferences(session, make_shift, make_volunteer, *, mapped=True):
    v, greeting, coffee, group = setup(session, make_shift, make_volunteer)
    windows = deepcopy(v.preferences['recurring_windows'])
    windows[1]['time_mode'] = 'event'
    if not mapped:
        windows[1]['event_context']['event_type_ids'] = []
    v.preferences = {**v.preferences, 'recurring_windows': windows,
        'role_frequency_caps': [{'role_id': greeting.role_id, 'role_name': 'Greeter', 'max_per_month': 2}]}
    v.preferences.pop('max_per_month')
    return v, greeting, coffee, group


def test_normalized_mixed_reply_shape_retains_sunday_window_unknown_group_and_december_blackout(
    session, make_shift, make_volunteer
):
    v, greeting, coffee, group = event_preferences(session, make_shift, make_volunteer, mapped=False)
    previous = {'recurring_windows': [window(greeting.role)], 'role_frequency_caps': []}
    blackout = [f'2026-12-{day:02}' for day in range(1, 32)]
    data = {'recurring_windows': v.preferences['recurring_windows'],
        'role_frequency_caps': v.preferences['role_frequency_caps'],
        'frequency_known': False, 'max_per_month': None, 'unavailable_dates': blackout}
    before = deepcopy(data)
    roles = [greeting.role, coffee.role]
    merged = merge_recurring_windows(data, previous, roles, [])
    caps = merge_role_frequency_caps(data, previous, roles)
    assert merged[0] == previous['recurring_windows'][0]
    assert merged[1]['time_mode'] == 'event' and not merged[1]['all_day']
    assert merged[1]['event_context']['event_type_ids'] == [] and merged[1]['start_time'] is None
    assert caps == [{'role_id': greeting.role_id, 'role_name': 'Greeter', 'max_per_month': 2}]
    assert global_frequency_limit({**data, 'role_frequency_caps': caps}) is None
    assert data == before and len(data['unavailable_dates']) == 31
    assert not eligibility.check(session, v, coffee)
    assert not v.qualifications and not v.is_coordinator and not v.assignments


def test_event_follow_matches_only_known_role_group_and_weekday(session, make_shift, make_volunteer):
    v, _, coffee, group = event_preferences(session, make_shift, make_volunteer)
    assert eligibility.check(session, v, coffee)
    # Unlike unknown clock hours, explicit event-follow can follow this actual
    # mapped event at any time, including evening; it is not a general day window.
    coffee.event.starts_at = NOW.replace(day=7, hour=20)
    coffee.event.ends_at = coffee.event.starts_at + timedelta(hours=2)
    assert eligibility.check(session, v, coffee)
    unrelated = make_shift('Coffee', starts=coffee.event.starts_at, title='Unrelated meeting')
    assert not eligibility.check(session, v, unrelated)
    wrong_role = make_shift('Greeter', starts=coffee.event.starts_at)
    wrong_role.event.event_type_id = group.id
    assert not eligibility.check(session, v, wrong_role)
    coffee.event.starts_at += timedelta(days=1)
    coffee.event.ends_at += timedelta(days=1)
    assert not eligibility.check(session, v, coffee)


def test_clock_hours_unknown_stay_held_even_for_mapped_group(session, make_shift, make_volunteer):
    v, _, coffee, _ = event_preferences(session, make_shift, make_volunteer)
    windows = deepcopy(v.preferences['recurring_windows'])
    windows[1].pop('time_mode')
    v.preferences = {**v.preferences, 'recurring_windows': windows}
    assert not eligibility.check(session, v, coffee)


def test_event_follow_actual_interval_can_cross_dst_without_widening_ordinary_clock_windows(
    session, make_shift, make_volunteer
):
    v, greeting, _, group = event_preferences(session, make_shift, make_volunteer)
    shift = make_shift('Greeter', starts=datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc), minutes=70)
    shift.event.event_type_id = group.id
    event_window = {**window(greeting.role, start=None, end=None,
        context={'label': "men's group", 'event_type_ids': [group.id]}), 'time_mode': 'event'}
    v.preferences = {**v.preferences, 'recurring_windows': [event_window]}
    assert eligibility.check(session, v, shift)
    event_window['event_context']['event_type_ids'] = []
    assert not eligibility.check(session, v, shift)


def test_december_blackout_overrides_both_event_follow_and_clock_window(session, make_shift, make_volunteer):
    v, _, _, group = event_preferences(session, make_shift, make_volunteer)
    session.add(m.Availability(volunteer_id=v.id, month='2026-12',
        unavailable_dates=[f'2026-12-{day:02}' for day in range(1, 32)]))
    session.flush()
    coffee = make_shift('Coffee', starts=NOW.replace(month=12, day=2, hour=20), minutes=60)
    coffee.event.event_type_id = group.id
    greeter = make_shift('Greeter', starts=NOW.replace(month=12, day=6, hour=8), minutes=120)
    assert 'said unavailable on 2026-12-02' in eligibility.check(session, v, coffee).reasons
    assert 'said unavailable on 2026-12-06' in eligibility.check(session, v, greeter).reasons


def test_coffee_bookings_do_not_consume_greeter_cap_and_third_greeting_is_held(
    session, make_shift, make_volunteer, assign
):
    v, greeting, coffee, group = event_preferences(session, make_shift, make_volunteer)
    for day in (7, 14, 21):
        shift = make_shift('Coffee', starts=NOW.replace(day=day, hour=20), minutes=60)
        shift.event.event_type_id = group.id
        assign(v, shift, status='completed')
    assert eligibility.check(session, v, greeting)
    first = assign(v, make_shift('Greeter', starts=NOW.replace(day=11, hour=8), minutes=60), status='completed')
    assert eligibility.check(session, v, greeting)
    second = assign(v, make_shift('Greeter', starts=NOW.replace(day=18, hour=8), minutes=60), status='approved')
    assert not eligibility.check(session, v, greeting)
    assert 'role-specific monthly maximum reached: Greeter' in eligibility.check(session, v, greeting).reasons
    assert eligibility.check(session, v, coffee)
    assert eligibility.check(session, v, second.shift, _exclude_assignment_id=second.id)
    assert not eligibility.check(session, v, second.shift)  # Existing assignment not excluded.
    second.status = 'cancelled'
    session.flush()
    assert eligibility.check(session, v, greeting)


def test_role_cap_uses_event_local_calendar_month_and_rolls_over_at_year_boundary(
    session, make_shift, make_volunteer, assign
):
    v, _, _, _ = event_preferences(session, make_shift, make_volunteer)
    for hour in (1, 3):
        # December 1 UTC, still November 30 Denver.
        assign(v, make_shift('Greeter', starts=datetime(2026, 12, 1, hour, tzinfo=timezone.utc), minutes=60), status='completed')
    dec = make_shift('Greeter', starts=NOW.replace(month=12, day=6, hour=8), minutes=60)
    assert eligibility.check(session, v, dec)
    for day in (13, 20):
        assign(v, make_shift('Greeter', starts=NOW.replace(month=12, day=day, hour=8), minutes=60), status='completed')
    assert not eligibility.check(session, v, dec)
    jan = make_shift('Greeter', starts=NOW.replace(year=2027, month=1, day=3, hour=8), minutes=60)
    assert eligibility.check(session, v, jan)


def test_frequency_and_unrelated_followups_preserve_event_mode_and_role_caps(session, make_shift, make_volunteer):
    v, greeting, coffee, group = event_preferences(session, make_shift, make_volunteer)
    previous = deepcopy(v.preferences)
    data = {'unavailable_dates': ['2026-12-02']}
    assert merge_recurring_windows(data, previous, [greeting.role, coffee.role], [group]) == previous['recurring_windows']
    assert merge_role_frequency_caps(data, previous, [greeting.role, coffee.role]) == previous['role_frequency_caps']
    assert previous == v.preferences


@pytest.mark.parametrize('bad', [
    {'time_mode': True}, {'time_mode': 'whenever'}, {'time_mode': 'event', 'all_day': True},
    {'time_mode': 'event', 'event_context': None}, {'time_mode': 'event', 'start_time': '08:00', 'end_time': '10:00'},
    {'time_mode': 'event', 'any_role': True, 'role_ids': [], 'role_label': None},
])
def test_invalid_event_mode_cannot_widen_availability(session, make_shift, make_volunteer, bad):
    v, greeting, coffee, group = event_preferences(session, make_shift, make_volunteer)
    malformed = [{**v.preferences['recurring_windows'][1], **bad}]
    with pytest.raises(ValueError):
        normalize_recurring_windows(malformed, [greeting.role, coffee.role], [group])
    v.preferences = {**v.preferences, 'recurring_windows': malformed}
    assert not eligibility.check(session, v, coffee)


@pytest.mark.parametrize('bad', [
    {'role_id': True}, {'role_id': 999}, {'role_name': 'Coffee'},
    {'max_per_month': True}, {'max_per_month': 0}, {'max_per_month': 9},
])
def test_invalid_role_frequency_mapping_or_cap_is_held(session, make_shift, make_volunteer, bad):
    v, greeting, coffee, _ = event_preferences(session, make_shift, make_volunteer)
    caps = [{**v.preferences['role_frequency_caps'][0], **bad}]
    with pytest.raises(ValueError):
        normalize_role_frequency_caps(caps, [greeting.role, coffee.role])
    v.preferences = {**v.preferences, 'role_frequency_caps': caps}
    assert not eligibility.check(session, v, greeting)


def test_duplicate_role_caps_are_invalid_and_old_global_limit_behavior_is_preserved(session, make_shift, make_volunteer):
    v, greeting, coffee, _ = event_preferences(session, make_shift, make_volunteer)
    with pytest.raises(ValueError):
        normalize_role_frequency_caps(v.preferences['role_frequency_caps'] * 2, [greeting.role, coffee.role])
    assert global_frequency_limit({}) == 3
    assert global_frequency_limit({'max_per_month': 2}) == 2
    assert global_frequency_limit({'role_frequency_caps': []}) is None
    assert global_frequency_limit({'role_frequency_caps': v.preferences['role_frequency_caps'], 'max_per_month': 4}) == 4
    assert session.scalar(select(m.Assignment)) is None
