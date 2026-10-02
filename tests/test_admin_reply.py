"""Admin composing creates held exact review only, using fictional test phones."""
from datetime import timedelta
from dataclasses import replace
from fastapi.testclient import TestClient
from sqlalchemy import select
import pytest
from app.clock import FakeClock
from app.db import models as m
from app.web.texty import admin
from tests.test_confirmations import mode_app


def sign_in_fixture(app):
    app.dependency_overrides[admin] = lambda: {"email":"coordinator@example.test"}


def test_reply_requires_auth_and_holds_exact_roster_recipient_body(mode_app, monkeypatch):
    app, volunteers, _ = mode_app
    def never_deliver(*args, **kwargs):
        raise AssertionError('Composing must not invoke a transport or model')
    monkeypatch.setattr(app.state.provider, 'send', never_deliver)
    monkeypatch.setattr(app.state.gloo, 'create_response', never_deliver)
    body = "  Synthetic exact words.\nSecond line. 🐒  "
    with TestClient(app) as client:
        payload={"volunteer_id":volunteers[0].id,"body":body}
        assert client.post('/api/reply',json=payload).status_code == 401
        sign_in_fixture(app)
        assert client.get('/api/config').json()['adminReplyAvailable'] is True
        for _ in range(2):
            response=client.post('/api/reply',json=payload)
            assert response.status_code == 200
            result=response.json()
            assert result['delivery']=='awaiting_confirmation'
            assert result['phone']==volunteers[0].phone and result['body']==body
            assert len(result['content_hash'])==64
        review=next(p for p in client.get('/api/state').json()['proposals'] if p['id']==str(result['approval_id']))
        assert review['reply']==body and review['phone']==volunteers[0].phone
        assert review['content_hash']==result['content_hash'] and review['status']=='pending'
        with app.state.session_factory() as session:
            assert len(session.scalars(select(m.Approval)).all())==1
            assert session.scalar(select(m.Message)) is None
        assert client.post('/mac/outbound/pull',headers={'Authorization':'Bearer synthetic-bridge-'+'x'*40},json={}).json()['messages']==[]


@pytest.mark.parametrize('payload', [None, [], {}, {'volunteer_id':True,'body':'Hi'}, {'volunteer_id':1,'body':''}, {'volunteer_id':1,'body':' '*4}, {'volunteer_id':1,'body':'x'*1601}, {'volunteer_id':1,'body':123}, {'volunteer_id':1,'body':'Hi','phone':'+15555550199'}, {'volunteer_id':1,'body':'Hi','_approved':True}])
def test_reply_rejects_invalid_or_client_override_payloads(mode_app, payload):
    app, _, _ = mode_app
    sign_in_fixture(app)
    with TestClient(app) as client:
        assert client.post('/api/reply',json=payload).status_code==400
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.Message)) is None


@pytest.mark.parametrize('guard', ['opt_out','inactive','phone_opt_out','care','outside_scope','expired_session','missing_recipient','review_disabled'])
def test_reply_rechecks_consent_care_transport_session_and_exact_review(mode_app, guard):
    app, volunteers, _ = mode_app
    sign_in_fixture(app)
    target=volunteers[0]
    with app.state.session_factory() as session:
        # Establish explicit fictional fixture state, not an automated record proposal.
        session.info["record_authorized"] = True
        v=session.get(m.Volunteer,target.id)
        if guard=='opt_out': v.sms_opt_in=False
        if guard=='inactive': v.status='inactive'
        if guard=='phone_opt_out': session.add(m.Policy(key='sms_opt_out:'+v.phone,value={'value':True}))
        if guard=='care': session.add(m.Escalation(category='sensitive',severity='normal',status='open',summary='Synthetic private concern',related_ids={'volunteer_id':v.id},created_at=app.state.clock.now()))
        session.commit()
    if guard=='outside_scope': app.state.provider.phones=frozenset()
    if guard=='expired_session': app.state.mac_delivery_clock=FakeClock(app.state.clock.now()+timedelta(hours=3))
    if guard=='review_disabled': app.state.settings=replace(app.state.settings,competition_confirmation_required=False)
    with TestClient(app) as client:
        result=client.post('/api/reply',json={'volunteer_id':999999 if guard=='missing_recipient' else target.id,'body':'Synthetic exact text'})
        assert result.status_code in {403,404,409}
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.Message)) is None
