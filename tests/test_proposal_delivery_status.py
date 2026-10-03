"""Proposal receipts reflect gate outcomes; no native delivery runs here."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.web.texty import admin
from tests.test_confirmations import mode_app  # noqa: F401
from tests.test_manual_reply_gloo import ExactGloo, request


def prepare_review(client, app, volunteer, body):
    app.state.gloo = ExactGloo()
    response = client.post('/api/reply', json=request(volunteer, body))
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared['delivery'] == 'awaiting_confirmation' and prepared['body'] == body
    assert len(app.state.gloo.calls) == 1
    with app.state.session_factory() as session:
        approval = session.get(m.Approval, prepared['approval_id'])
        assert approval.status == 'pending' and approval.payload['body'] == body
        assert approval.payload['content_hash'] == prepared['content_hash']
        proof = session.get(m.Notification, f'google-voice-gloo:{approval.id}')
        assert proof is not None and proof.state == 'composed'
        assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None
    return prepared['approval_id'], prepared['content_hash']


@pytest.mark.parametrize('case', ['policy', 'queued', 'reject'])
def test_exact_review_reports_blocked_or_actual_queue_without_transport_guess(mode_app, monkeypatch, case):
    app, volunteers, _ = mode_app
    app.dependency_overrides[admin] = lambda: {'email':'coordinator@example.test'}
    with TestClient(app) as client:
        ident, digest = prepare_review(client, app, volunteers[0], 'Synthetic reviewed text.')
        if case == 'policy':
            monkeypatch.setattr('app.core.outbound_conversation.problem', lambda *args, **kwargs:'Synthetic conversation policy suppression')
        decision = 'reject' if case == 'reject' else 'approve'
        response = client.post(f'/api/proposals/{ident}/{decision}',json={'content_hash':digest})
        assert response.status_code == 200, response.text
        receipt = response.json()
        with app.state.session_factory() as session:
            texts = session.scalars(select(m.Message).where(m.Message.direction=='out')).all()
            if case == 'queued':
                assert receipt['delivery'] == 'queued_for_mac' and receipt['message_status'] == 'queued'
                assert len(texts) == 1 and texts[0].id == receipt['message_id']
            else:
                assert receipt['delivery'] == ('blocked_policy' if case == 'policy' else 'rejected')
                assert receipt['message_id'] is None and receipt['message_status'] is None and not texts
                assert receipt['mock_sms_count'] == 0
                if case == 'policy': assert any('blocked_policy' in note for note in receipt['notes'])
        assert len(app.state.gloo.calls) == 1


def test_changed_consent_returns_no_queue_receipt(mode_app):
    app, volunteers, _ = mode_app
    app.dependency_overrides[admin] = lambda: {'email':'coordinator@example.test'}
    with TestClient(app) as client:
        ident, digest = prepare_review(client, app, volunteers[0], 'Synthetic exact draft.')
        with app.state.session_factory() as session:
            volunteer = session.get(m.Volunteer, volunteers[0].id)
            # Model a separately authorized STOP, rather than staging another record review.
            session.info['record_authorized'] = True
            volunteer.sms_opt_in=False;session.commit()
        response=client.post(f'/api/proposals/{ident}/approve',json={'content_hash':digest})
        assert response.status_code == 200, response.text
        receipt=response.json()
        assert receipt['delivery']=='not_queued' and receipt['message_id'] is None
        assert receipt['message_status'] is None and receipt['mock_sms_count'] == 0
        assert any('Nothing delivered' in note for note in receipt['notes'])
        assert len(app.state.gloo.calls) == 1
        with app.state.session_factory() as session:
            assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None
