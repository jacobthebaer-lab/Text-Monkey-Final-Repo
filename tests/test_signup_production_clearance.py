"""The exact menu may strengthen its reserved Production role, never clearance."""
import pytest
from sqlalchemy import select

from app.core.signup_copy import ensure_exact_role_menu
from app.db import models as m


@pytest.mark.parametrize('existing', [[], ['custom_training'], ['sound_training', 'custom_training']])
def test_reserved_production_training_repair_is_monotonic_and_idempotent(session, make_volunteer, existing):
    production = m.Role(id=3, name='Production', ministry='Custom Ministry',
                        required_qualifications=list(existing), criticality='critical',
                        fill_policy='needs_approval')
    child_care = m.Role(id=5, name='Child Care', ministry='Custom Kids',
                       required_qualifications=['custom_clearance'], criticality='optional',
                       fill_policy='needs_approval')
    volunteer = make_volunteer(quals=[('custom_training', 'verified', None)])
    session.add_all([production, child_care]); session.commit()
    qualification_ids = list(session.scalars(select(m.Qualification.id)))
    expected = [*existing] if 'sound_training' in existing else [*existing, 'sound_training']

    ensure_exact_role_menu(session); session.commit(); session.expire_all()
    assert production.required_qualifications == expected
    assert (production.name, production.ministry, production.criticality, production.fill_policy) == (
        'Production', 'Custom Ministry', 'critical', 'needs_approval')
    assert child_care.required_qualifications == ['custom_clearance']
    assert list(session.scalars(select(m.Qualification.id))) == qualification_ids
    assert all(q.type != 'sound_training' for q in volunteer.qualifications)

    ensure_exact_role_menu(session); session.commit(); session.expire_all()
    assert production.required_qualifications == expected
    assert list(session.scalars(select(m.Qualification.id))) == qualification_ids


def test_conflicting_reserved_menu_prevents_partial_clearance_repair(session):
    production = m.Role(id=3, name='Production', ministry='Custom Ministry',
                        required_qualifications=[], criticality='standard', fill_policy='auto')
    conflict = m.Role(id=5, name='Unrelated Role', ministry='Custom',
                      required_qualifications=['custom_clearance'], criticality='standard', fill_policy='auto')
    session.add_all([production, conflict]); session.commit()
    with pytest.raises(ValueError, match='conflicts with an existing role ID'):
        ensure_exact_role_menu(session)
    assert production.required_qualifications == []
    assert len(list(session.scalars(select(m.Role)))) == 2
