"""Independent consent checks with adversarial model output; no real transport."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core import confirmations
from app.db import models as m
from tests.test_concise_signup import ConciseGloo, route


class ConsentClaimGloo(ConciseGloo):
    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        if isinstance(facts, list):
            self.calls.append(facts)
            return SimpleNamespace(output_text=json.dumps({
                'signup': True, 'first_name': 'Alex', 'last_name': 'Example',
                'consent': True, 'sms_opt_in': True, 'consent_pending': False,
            }))
        return super().create_response(**kwargs)


@pytest.mark.parametrize('body', [
    'Alex Example', '"Alex Example YES"', 'Alex Example said "YES"',
])
def test_model_consent_claim_cannot_authorize_sender(session, clock, provider, body):
    session.info[confirmations.MODE_KEY] = True
    gloo = ConsentClaimGloo()
    result = route(session, clock, provider, body, gloo)
    person = session.scalar(select(m.Volunteer))
    assert result.routed_to in {'signup_consent_pending', 'signup_identity_review'}
    assert person is None or (not person.sms_opt_in and person.status == 'inactive')
    assert session.scalar(select(m.Assignment)) is None


def test_interests_cannot_complete_pending_consent(session, clock, provider, make_volunteer):
    person = make_volunteer(name='Alex Example', opt_in=False, status='inactive',
        prefs={'signup_source': 'sms', 'consent_pending': True})
    # The same sender's role preference is useful information, not an affirmative opt-in.
    person.phone = '+12025550190'
    session.flush()
    result = route(session, clock, provider, 'I can help with music on Sundays', ConsentClaimGloo())
    assert result.routed_to == 'signup_consent_pending'
    assert not person.sms_opt_in and person.preferences['consent_pending'] is True
    assert session.scalar(select(m.Assignment)) is None
