"""Proposal receipts reflect gate outcomes; no native delivery runs here."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.web.texty import admin
from tests.test_confirmations import mode_app  # noqa: F401


@pytest.mark.parametrize('case', ['policy', 'queued', 'reject'])
def test_exact_review_reports_blocked_or_actual_queue_without_transport_guess(mode_app, monkeypatch, case):
    app, volunteers, _ = mode_app
    app.dependency_overrides[admin] = lambda: {'email':'coordinator@example.test'}
    with app.state.session_factory() as session:
        result = SendGate(session, app.state.clock, app.state.provider).send(
            body='Synthetic reviewed text.', purpose='manual', volunteer=session.get(m.Volunteer, volunteers[0].id))
        assert result.status == SendStatus.HELD_FOR_APPROVAL
        approval = session.get(m.Approval, result.approval_id)
        ident, digest = approval.id, approval.payload['content_hash']
        session.commit()
    if case == 'policy':
        monkeypatch.setattr('app.core.outbound_conversation.problem', lambda *args, **kwargs:'Synthetic conversation policy suppression')
    with TestClient(app) as client:
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


def test_changed_consent_returns_no_queue_receipt(mode_app):
    app, volunteers, _ = mode_app
    app.dependency_overrides[admin] = lambda: {'email':'coordinator@example.test'}
    with app.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, volunteers[0].id)
        result = SendGate(session, app.state.clock, app.state.provider).send(body='Synthetic exact draft.',purpose='manual',volunteer=volunteer)
        approval = session.get(m.Approval,result.approval_id)
        ident,digest=approval.id,approval.payload['content_hash']
        # Model a separately authorized STOP, rather than staging another record review.
        session.info['record_authorized'] = True
        volunteer.sms_opt_in=False;session.commit()
    with TestClient(app) as client:
        receipt=client.post(f'/api/proposals/{ident}/approve',json={'content_hash':digest}).json()
        assert receipt['delivery']=='not_queued' and receipt['message_id'] is None
        assert any('Nothing delivered' in note for note in receipt['notes'])
