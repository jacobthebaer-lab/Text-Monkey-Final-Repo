"""Opt-in fictional recipes use the existing mapper and clearance engine."""
import json
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import eligibility
from app.db import models as m
from app.db.seed import DATA_DIR, Seeder
from app.integrations.gcal import sync

CATALOG = json.loads((DATA_DIR / 'optional_ministry_recipes.json').read_text())
VARIANTS = CATALOG['event_types']
GUARDED_ROLES = [role for role in CATALOG['roles'] if role['required_qualifications']]


def install_reviewed_examples(session):
    """Synthetic fixture only: model the coordinator's explicit stored configuration."""
    seeder = Seeder(session)
    seeder.load_roles()
    roles = dict(seeder.roles)
    for row in CATALOG['roles']:
        role = m.Role(**{key: row[key] for key in ('name', 'ministry', 'required_qualifications', 'criticality', 'fill_policy')})
        session.add(role)
        roles[role.name] = role
    session.flush()
    event_types = {}
    for row in VARIANTS:
        event_type = m.EventType(name=row['name'], title_patterns=row['title_patterns'])
        session.add(event_type)
        session.flush()
        for item in row['recipe']:
            session.add(m.RoleRecipe(event_type_id=event_type.id, role_id=roles[item['role']].id, count=item['count']))
        event_types[row['name']] = event_type
    session.flush()
    return roles, event_types


def calendar_item(row, clock, index=0):
    title = row['title_patterns'][0].removeprefix('^').removesuffix('$')
    start = clock.now() + timedelta(days=3, hours=index)
    return {'id': 'synthetic-optional-' + row['name'], 'summary': title,
        'start': {'dateTime': start.isoformat()}, 'end': {'dateTime': (start + timedelta(hours=1)).isoformat()}}


class Calendar:
    """Only the read method exists; a write or notification call fails the test."""
    def __init__(self, items): self.items = items
    def events(self): return self
    def list(self, **kwargs): return self
    def execute(self): return {'items': self.items}


def import_calendar(session, clock, items):
    return sync(SimpleNamespace(session=session, clock=clock), service=Calendar(items),
        settings=Settings(google_calendar_id='synthetic-optional-recipes', sms_provider='mock'))


def test_optional_catalog_does_not_change_default_seed_or_match_real_titles(session):
    seeder = Seeder(session)
    seeder.load_roles()
    defaults = seeder.load_event_types()
    assert set(defaults) == {'sunday_service', 'kids_night', 'food_drive', 'christmas_eve'}
    assert len(session.scalars(select(m.Role)).all()) == 10
    assert CATALOG['activation'] == 'optional_manual_review'
    assert {row['feature_id'] for row in VARIANTS} == {
        'sunday-ministry', 'midweek-ministry', 'community-ministry', 'seasonal-events'}
    for row in VARIANTS:
        for title in ('Sunday Service', 'Youth Night', 'Food Pantry', 'Easter Service', 'Event Setup'):
            assert not any(re.search(pattern, title, re.I) for pattern in row['title_patterns'])


@pytest.mark.parametrize('row', VARIANTS, ids=lambda row: row['name'])
def test_each_reviewed_variant_maps_slots_once_without_granting_anything(session, clock, row):
    roles, types = install_reviewed_examples(session)
    item = calendar_item(row, clock)
    first = import_calendar(session, clock, [item])
    assert first['created'] == 1 and first['unknown'] == 0
    event = session.scalar(select(m.Event))
    assert event.event_type_id == types[row['name']].id
    expected = {(roles[item['role']].id, index) for item in row['recipe'] for index in range(item['count'])}
    assert {(shift.role_id, shift.slot_index) for shift in session.scalars(select(m.Shift))} == expected
    second = import_calendar(session, clock, [item])
    assert second['created'] == 0 and second['updated'] == 1
    assert len(session.scalars(select(m.Shift)).all()) == len(expected)
    assert session.scalar(select(m.Volunteer)) is None
    assert session.scalar(select(m.Qualification)) is None
    assert session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Message)) is None


def test_all_optional_patterns_are_unambiguous_with_existing_patterns(session, clock):
    seeder = Seeder(session)
    seeder.load_roles()
    defaults = seeder.load_event_types()
    patterns = {name: event_type.title_patterns for name, (event_type, recipe) in defaults.items()}
    patterns.update({row['name']: row['title_patterns'] for row in VARIANTS})
    for row in VARIANTS:
        title = calendar_item(row, clock)['summary']
        assert [name for name, candidates in patterns.items() if any(re.search(pattern, title, re.I) for pattern in candidates)] == [row['name']]


def test_stored_admin_counts_and_stricter_role_requirements_remain_authoritative(session, clock):
    roles, types = install_reviewed_examples(session)
    row = next(row for row in VARIANTS if row['name'] == 'adult_group_with_childcare')
    host_recipe = session.scalar(select(m.RoleRecipe).where(
        m.RoleRecipe.event_type_id == types[row['name']].id, m.RoleRecipe.role_id == roles['adult group host'].id))
    host_recipe.count = 3
    nursery_recipe = session.scalar(select(m.RoleRecipe).where(
        m.RoleRecipe.event_type_id == types[row['name']].id, m.RoleRecipe.role_id == roles['nursery'].id))
    nursery_recipe.count = 0
    requirements = roles['nursery'].required_qualifications + ['synthetic_additional_church_clearance']
    roles['nursery'].required_qualifications = requirements
    session.flush()
    import_calendar(session, clock, [calendar_item(row, clock)])
    assert len(session.scalars(select(m.Shift).where(m.Shift.role_id == roles['adult group host'].id)).all()) == 3
    assert session.scalar(select(m.Shift).where(m.Shift.role_id == roles['nursery'].id)) is None
    assert roles['nursery'].required_qualifications == requirements
    assert roles['nursery'].fill_policy == 'needs_approval'


@pytest.mark.parametrize('role_spec', GUARDED_ROLES, ids=lambda role: role['name'])
@pytest.mark.parametrize('evidence', ['missing', 'pending', 'expired', 'verified'])
def test_example_guarded_roles_require_current_verified_evidence(session, clock, make_volunteer, role_spec, evidence):
    roles, types = install_reviewed_examples(session)
    role = roles[role_spec['name']]
    row = next(row for row in VARIANTS if any(item['role'] == role.name for item in row['recipe']))
    import_calendar(session, clock, [calendar_item(row, clock)])
    shift = session.scalar(select(m.Shift).where(m.Shift.role_id == role.id))
    person = make_volunteer('Synthetic Optional Candidate')
    assert role.fill_policy == 'needs_approval'
    if evidence != 'missing':
        for credential in role.required_qualifications:
            session.add(m.Qualification(volunteer_id=person.id, type=credential,
                status='pending' if evidence == 'pending' else 'verified',
                verified_by='Synthetic coordinator' if evidence != 'pending' else None,
                expires_on=shift.event.starts_at.date() - timedelta(days=1) if evidence == 'expired' else shift.event.starts_at.date() + timedelta(days=1)))
        session.flush()
        session.expire(person, ['qualifications'])
    result = eligibility.check(session, person, shift)
    assert result.eligible is (evidence == 'verified'), result.reasons
    assert session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Message)) is None
