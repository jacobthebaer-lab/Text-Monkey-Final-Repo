"""Capacity evidence cannot turn one role's frequency into a global ceiling."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.agents.capacity_agent import scan
from tests.conftest import NOW


@pytest.mark.parametrize('global_limit', ['omitted', None, 3])
def test_role_cap_only_limits_its_role_without_false_coffee_workload_flag(
    session, clock, make_volunteer, make_shift, assign, tmp_path, global_limit
):
    greeting = make_shift('Greeter', starts=NOW+timedelta(days=2))
    coffee = make_shift('Coffee', starts=NOW+timedelta(days=3))
    prefs = {'interested_roles': ['Greeter', 'Coffee'], 'role_frequency_caps': [
        {'role_id': greeting.role_id, 'role_name': 'Greeter', 'max_per_month': 2}]}
    if global_limit != 'omitted':
        prefs['max_per_month'] = global_limit
    volunteer = make_volunteer(prefs=prefs)
    for day in (2, 4, 6, 8):
        assign(volunteer, make_shift('Coffee', starts=NOW-timedelta(days=day)), status='completed')
    for day in (9, 16, 23, 30):
        make_shift('Greeter', starts=NOW+timedelta(days=day))
    for day in (10, 17, 24, 31, 38, 45):
        make_shift('Coffee', starts=NOW+timedelta(days=day))
    ctx = SimpleNamespace(session=session, clock=clock, log_dir=tmp_path)
    flags = scan(ctx)
    burnout = [f for f in flags if f.type == 'burnout' and f.evidence['volunteer_id'] == volunteer.id]
    assert bool(burnout) is (global_limit == 3)
    growing = {f.evidence['role_id']: f.evidence['capacity'] for f in flags if f.type == 'growing_need'}
    assert growing[greeting.role_id] == 4
    if global_limit == 3:
        assert growing[coffee.role_id] == 6
    else:
        assert coffee.role_id not in growing


def test_unknown_coffee_capacity_does_not_claim_shortage_for_mixed_pool(
    session, clock, make_volunteer, make_shift, tmp_path
):
    greeting = make_shift('Greeter')
    for day in range(1, 10):
        make_shift('Coffee', starts=NOW+timedelta(days=day))
    make_volunteer(prefs={'interested_roles': ['Coffee'], 'max_per_month': 1})
    make_volunteer(prefs={'interested_roles': ['Coffee'], 'max_per_month': None,
        'role_frequency_caps': [{'role_id': greeting.role_id, 'role_name': 'Greeter', 'max_per_month': 2}]})
    flags = scan(SimpleNamespace(session=session, clock=clock, log_dir=tmp_path))
    assert not any(f.type == 'growing_need' and f.summary.startswith('Eight-week Coffee') for f in flags)
