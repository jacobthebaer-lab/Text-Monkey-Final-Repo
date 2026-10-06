"""A short real preference reply must not wait on long interpretation."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.integrations import mac_progress
from app.web.mac_messages import Incoming
from tests.session_fixtures import session_id
from tests.test_mac_progress import PHONE, progress_app, incoming, post, submit_ack


def enable_conversation(application):
    with application.state.session_factory() as session:
        session.add(m.Policy(key='conversational_signup:' + PHONE,
            value={'value': True, 'session_id': session_id(PHONE)}))
        session.commit()


@pytest.mark.parametrize('body', ['Sure, fixed is great', 'First and third Sundays'])
def test_short_reply_gets_one_ack_before_any_extraction(progress_app, body):
    enable_conversation(progress_app)
    data = incoming(guid='short-preference', body=body)
    with TestClient(progress_app) as client:
        reply = post(client, '/mac/inbound', data)
        assert reply.status_code == 200
        assert reply.json()['progress_state'] == 'waiting_ack'
        assert not progress_app.state.gloo.extracting.is_set()
        assert post(client, '/mac/inbound', data).json()['duplicate']
        with progress_app.state.session_factory() as session:
            assert len(session.scalars(select(m.Message).where(m.Message.direction == 'out')).all()) == 1
            assert session.scalar(select(m.Assignment)) is None
        submit_ack(client)
        assert not progress_app.state.gloo.extracting.is_set()


@pytest.mark.parametrize('body', ['STOP', 'Delete my data', 'Am I booked?', 'I want to hurt myself'])
def test_short_control_care_and_questions_keep_their_own_routes(progress_app, body):
    enable_conversation(progress_app)
    with progress_app.state.session_factory() as session:
        assert not mac_progress.complex_availability(session, progress_app.state, Incoming(**incoming(body=body)))


@pytest.mark.parametrize('scope', ['absent', 'wrong_session', 'disabled'])
def test_short_ack_needs_current_explicit_conversation_scope(progress_app, scope):
    with progress_app.state.session_factory() as session:
        if scope != 'absent':
            session.add(m.Policy(key='conversational_signup:' + PHONE,
                value={'value': scope != 'disabled', 'session_id': '0'*32 if scope == 'wrong_session' else session_id(PHONE)}))
            session.flush()
        assert not mac_progress.complex_availability(session, progress_app.state,
            Incoming(**incoming(body='Sure, fixed is great')))
