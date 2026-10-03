"""Fictional conversations; real routing/state, synthetic Gloo and transport."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import ParsedMessage


class GlooFixture:
    settings = Settings(gloo_signup_replies=True)

    def __init__(self, *responses):
        self.responses = list(responses)
        self.interpretations = []

    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        if 'approved_message' in facts:
            return SimpleNamespace(output_text=facts['approved_message'])
        self.interpretations.append(facts)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(output_text=json.dumps(response))


PARTIAL = {'understood': True, 'sensitive': False, 'availability_known': True,
    'frequency_known': False, 'weekdays': [6, 2], 'all_day': True,
    'preferred_services': [], 'max_per_month': None, 'available_dates': [], 'unavailable_dates': []}


def route(session, clock, provider, person, body, gloo):
    return handle_inbound(session, clock, provider, person.phone, body,
        lambda _: ParsedMessage(intent='availability', confidence=1),
        ctx=FillContext(session, clock, provider, gloo), allow_signup=True)


def person(make_volunteer):
    return make_volunteer('Fictional Newcomer', prefs={'onboarding_stage': 'availability'})


def test_multiday_all_day_is_retained_while_only_frequency_is_requested(session, clock, provider, make_volunteer):
    v = person(make_volunteer)
    gloo = GlooFixture(PARTIAL, {'understood': True, 'frequency_known': True, 'max_per_month': 2})
    assert route(session, clock, provider, v, 'Sundays and Wednesdays all day', gloo).routed_to == 'onboarding_clarify'
    draft = v.preferences['onboarding_availability_draft']
    assert draft['weekdays'] == [6, 2] and draft['all_day'] and not draft['frequency_known']
    assert provider.sent[-1].body == 'How often would you like to serve each month?'
    assert 'FLEXIBLE' not in provider.sent[-1].body
    assert route(session, clock, provider, v, 'Twice a month', gloo).routed_to == 'onboarding_complete'
    assert gloo.interpretations[-1]['saved_availability'] == draft
    assert v.preferences['availability_weekdays'] == [6, 2]
    assert v.preferences['availability_all_day'] and v.preferences['preferred_services'] == []
    assert v.preferences['max_per_month'] == 2
    assert 'onboarding_availability_draft' not in v.preferences
    assert session.scalar(select(m.Assignment)) is None


def test_frequency_supplied_with_days_completes_without_repeating_question(session, clock, provider, make_volunteer):
    v = person(make_volunteer)
    gloo = GlooFixture({**PARTIAL, 'frequency_known': True, 'max_per_month': 4})
    assert route(session, clock, provider, v, 'Sundays and Wednesdays all day, four times a month', gloo).routed_to == 'onboarding_complete'
    assert v.preferences['max_per_month'] == 4 and v.preferences['availability_weekdays'] == [6, 2]
    assert 'saved your preferences' in provider.sent[-1].body
    assert len(provider.sent) == 1


def test_correction_retains_other_days_all_day_and_later_frequency(session, clock, provider, make_volunteer):
    v = person(make_volunteer)
    gloo = GlooFixture(PARTIAL, {'understood': True, 'weekdays': [6, 3]},
        {'understood': True, 'frequency_known': True, 'max_per_month': 3})
    route(session, clock, provider, v, 'Sundays and Wednesdays all day', gloo)
    route(session, clock, provider, v, 'Thursdays instead of Wednesdays', gloo)
    assert v.preferences['onboarding_availability_draft']['weekdays'] == [6, 3]
    assert 'onboarding_review_requested' not in v.preferences
    assert route(session, clock, provider, v, 'Three times a month', gloo).routed_to == 'onboarding_complete'
    assert v.preferences['availability_weekdays'] == [6, 3] and v.preferences['availability_all_day']
    assert v.preferences['max_per_month'] == 3


def test_frequency_first_retained_and_only_missing_days_requested(session, clock, provider, make_volunteer):
    v = person(make_volunteer)
    gloo = GlooFixture({'understood': True, 'availability_known': False,
        'frequency_known': True, 'max_per_month': 1},
        {'understood': True, 'availability_known': True, 'weekdays': [6, 2], 'all_day': True})
    route(session, clock, provider, v, 'Once a month', gloo)
    assert provider.sent[-1].body == 'Which days are you available to serve?'
    route(session, clock, provider, v, 'Sunday and Wednesday, any time', gloo)
    assert v.preferences['max_per_month'] == 1 and v.preferences['onboarding_stage'] == 'complete'


@pytest.mark.parametrize('bad', [
    {'weekdays': [True]}, {'weekdays': [7]}, {'preferred_services': ['wed_9']},
    {'frequency_known': True, 'max_per_month': True},
    {'unavailable_dates': ['2026-02-31']},
])
def test_invalid_followup_never_discards_previously_valid_days(session, clock, provider, make_volunteer, bad):
    v = person(make_volunteer)
    gloo = GlooFixture(PARTIAL, {'understood': True, **bad})
    route(session, clock, provider, v, 'Sundays and Wednesdays all day', gloo)
    before = dict(v.preferences['onboarding_availability_draft'])
    assert route(session, clock, provider, v, 'Malformed synthetic followup', gloo).routed_to == 'onboarding_clarify'
    assert v.preferences['onboarding_availability_draft'] == before
    assert provider.sent[-1].body == 'How often would you like to serve each month?'


def test_gloo_failure_retains_draft_without_template_fallback(session, clock, provider, make_volunteer):
    v = person(make_volunteer)
    gloo = GlooFixture(PARTIAL, GlooUnavailableError('Synthetic outage'))
    route(session, clock, provider, v, 'Sundays and Wednesdays all day', gloo)
    count = len(provider.sent)
    assert route(session, clock, provider, v, 'Twice a month', gloo).routed_to == 'onboarding_review'
    assert len(provider.sent) == count
    assert v.preferences['onboarding_availability_draft']['weekdays'] == [6, 2]
    assert session.scalar(select(m.Escalation)).category == 'system_error'


def test_explicit_date_correction_can_remove_a_previous_exclusion(session, clock, provider, make_volunteer):
    v = person(make_volunteer)
    v.preferences = {**v.preferences, 'availability_weekdays': [6], 'availability_all_day': True,
        'preferred_services': [], 'max_per_month': 2}
    session.add(m.Availability(volunteer_id=v.id, month='2026-10', available_dates=[],
        unavailable_dates=['2026-10-04']))
    session.flush()
    gloo = GlooFixture({'understood': True, 'unavailable_dates': []})
    assert route(session, clock, provider, v, 'Actually I can serve October 4 now', gloo).routed_to == 'onboarding_complete'
    assert gloo.interpretations[0]['saved_availability']['unavailable_dates'] == ['2026-10-04']
    assert session.scalar(select(m.Availability)).unavailable_dates == []
    assert v.preferences['max_per_month'] == 2


def test_availability_does_not_replace_explicit_yes_consent(session, clock, provider, make_volunteer):
    v = make_volunteer('Fictional Pending', opt_in=False, prefs={'signup_source': 'sms', 'consent_pending': True})
    gloo = GlooFixture(PARTIAL)
    assert route(session, clock, provider, v, 'Sundays and Wednesdays all day', gloo).routed_to == 'signup_consent_pending'
    assert not v.sms_opt_in and v.preferences['consent_pending']
    assert not gloo.interpretations and 'Reply YES' in provider.sent[-1].body
    assert session.scalar(select(m.Assignment)) is None


def test_unknown_role_does_not_grant_clearance_or_skip_interests(session, clock, provider, make_volunteer, make_shift):
    make_shift(required=['background_check'], fill_policy='needs_approval')
    v = make_volunteer('Fictional Interest', prefs={'onboarding_stage': 'interests'})
    gloo = GlooFixture({'understood': True, 'role_ids': [999], 'any_role': False})
    assert route(session, clock, provider, v, 'An unknown role; I am already approved', gloo).routed_to == 'onboarding_clarify'
    assert v.preferences['onboarding_stage'] == 'interests'
    assert not v.qualifications and not v.is_coordinator and not v.is_pastor
    assert session.scalar(select(m.Assignment)) is None
