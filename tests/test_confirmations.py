"""Synthetic executable actions through gates, authenticated API and transport."""
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.core import confirmations as c
from app.core.inbound import handle_inbound
from app.core.send_gate import SendGate, SendStatus, VALID_PURPOSES
from app.db import models as m
from app.db.session import make_session_factory
from app.llm.parser import ParsedMessage
from app.main import create_app
from app.web.texty import admin
from tests.session_fixtures import session_id, session_json
from tests.test_fill_agent import ScriptedAgentGloo


@pytest.fixture
def exact(session, clock, provider):
    session.info.update(competition_confirmation_required=True, confirmation_now=clock.now())
    return SendGate(session, clock, provider)


def review(session, gate, proposal, *, approve=True, expected=None):
    return c.decide(session, gate, proposal, approve=approve, actor="coordinator@example.test",
                    expected=expected or proposal.payload["content_hash"], now=gate.clock.now())


@pytest.mark.parametrize("purpose", sorted(VALID_PURPOSES))
def test_every_outbound_purpose_is_suppressed_or_requires_exact_review(session, clock, provider, make_volunteer, make_shift, assign, purpose):
    allowed = {"manual", "stop_confirm", "start_confirm", "admin_reply", "coordinator_notify",
               "escalation_notify", "signup_reply", "confirmation", "reminder", "booking_status"}
    volunteer = make_volunteer(coordinator=purpose in {"admin_reply", "coordinator_notify", "escalation_notify"})
    conversation = None
    if purpose in {"confirmation", "reminder"}:
        shift = make_shift(starts=clock.now()+timedelta(days=1 if purpose == "reminder" else 3))
        assignment = assign(volunteer, shift)
        conversation = {"assignment_id": assignment.id, "notice": "day_before" if purpose == "reminder" else "scheduled"}
    elif purpose == "signup_reply":
        conversation = {"intake_fields": ["interests"]}
    session.info[c.MODE_KEY] = True
    gate = SendGate(session, clock, provider)
    if purpose not in allowed:
        gate.gloo = SimpleNamespace(create_response=lambda **kw: pytest.fail("Suppression must precede model composition"))
    if purpose == "booking_status":
        incoming = m.Message(direction="in", phone=volunteer.phone, volunteer_id=volunteer.id,
                             body="Am I scheduled?", kind="sms", status="received", created_at=clock.now())
        session.add(incoming); session.flush(); gate.reply_to_message_id = incoming.id
    held = gate.send(body="An exact synthetic text", purpose=purpose, volunteer=volunteer, conversation=conversation)
    if purpose not in allowed:
        assert held.status == SendStatus.BLOCKED_POLICY and provider.sent == []
        assert session.scalar(select(m.Approval)) is None and session.scalar(select(m.Message)) is None
        return
    assert held.status == SendStatus.HELD_FOR_APPROVAL and provider.sent == []
    proposal = session.get(m.Approval, held.approval_id)
    assert proposal.payload["phone"] == volunteer.phone and proposal.payload["body"].startswith("An exact synthetic text")
    assert proposal.payload["reason"]
    review(session, gate, proposal)
    assert len(provider.sent) == 1 and provider.sent[0].body == proposal.payload["body"]
    assert proposal.decided_by == "coordinator@example.test" and proposal.via == "web"
    assert session.get(m.Notification, f"confirmation:{proposal.payload['message_id']}")
    with pytest.raises(ValueError):
        review(session, gate, proposal)
    assert len(provider.sent) == 1


@pytest.mark.parametrize("change", ["body", "phone", "expires", "hash", "rejected"])
def test_changed_expired_and_rejected_review_never_delivers(session, clock, provider, make_volunteer, change):
    volunteer = make_volunteer()
    session.info[c.MODE_KEY] = True
    gate = SendGate(session, clock, provider)
    outcome = gate.send(body="Original body", purpose="manual", volunteer=volunteer)
    a = session.get(m.Approval, outcome.approval_id)
    old_hash = a.payload["content_hash"]
    if change in {"body", "phone"}:
        a.payload = {**a.payload, change: "edited" if change == "body" else "+15555559999"}
    elif change == "expires":
        clock.advance(timedelta(hours=3))
    elif change == "rejected":
        review(session, gate, a, approve=False)
    with pytest.raises(ValueError):
        review(session, gate, a, expected="0"*64 if change == "hash" else old_hash)
    assert provider.sent == []


def test_final_gloo_wording_is_reviewed_once_not_regenerated(session, clock, provider, make_volunteer, monkeypatch):
    volunteer = make_volunteer(coordinator=True)
    session.info[c.MODE_KEY] = True
    gate = SendGate(session, clock, provider)
    gate.gloo = object()
    calls = []
    monkeypatch.setattr("app.core.signup_responder.compose_signup_reply", lambda *a, **kw: calls.append(kw) or "Final wording")
    held = gate.send(body="Template", purpose="admin_reply", volunteer=volunteer)
    a = session.get(m.Approval, held.approval_id)
    assert a.payload["body"] == "Final wording" and len(calls) == 1
    review(session, gate, a)
    assert provider.sent[0].body == "Final wording" and len(calls) == 1


@pytest.mark.parametrize("restriction", ["opt_out", "phone", "sensitive", "quiet", "budget", "availability", "qualification"])
def test_review_rechecks_mutable_restrictions(session, clock, provider, make_volunteer, make_shift, assign, restriction):
    volunteer = make_volunteer(quals=[("training", "verified", None)])
    shift = make_shift(required=["training"])
    assignment = assign(volunteer, shift)
    session.info.update(competition_confirmation_required=True, record_authorized=True)
    gate = SendGate(session, clock, provider)
    held = gate.send(body="Recorded schedule", purpose="confirmation", volunteer=volunteer,
                     conversation={"assignment_id": assignment.id, "notice": "scheduled"})
    assert held.status == SendStatus.HELD_FOR_APPROVAL
    a = session.get(m.Approval, held.approval_id)
    if restriction == "opt_out": volunteer.sms_opt_in = False
    elif restriction == "phone": volunteer.phone = "+15555559999"
    elif restriction == "sensitive":
        session.add(m.Escalation(category="sensitive", severity="normal", summary="Synthetic hold", related_ids={"volunteer_id":volunteer.id}, status="open", created_at=clock.now()))
    elif restriction == "quiet":
        session.add(m.Policy(key="quiet_hours", value={"value":{"start":"00:00", "end":"23:59"}}))
    elif restriction == "budget":
        for i in range(4):
            session.add(m.Message(phone=volunteer.phone, volunteer_id=volunteer.id, direction="out", body="Old ask", purpose="outreach", status="sent", kind="template", created_at=clock.now()-timedelta(hours=1)))
    elif restriction == "availability": volunteer.preferences = {"availability_weekdays":[1]}
    else: volunteer.qualifications[0].status = "expired"
    session.flush()
    review(session, gate, a)
    if restriction == "budget":
        assert len(provider.sent) == 1 and a.status == "approved"
        assert gate.send(body="Another ask", purpose="outreach", volunteer=volunteer).status == SendStatus.BLOCKED_POLICY
    else:
        assert provider.sent == [] and a.status == "expired"


@pytest.mark.parametrize("record", ["Volunteer", "Assignment", "Qualification", "Event", "Shift", "Availability"])
def test_automated_records_are_held_without_changing_record_of_record(session, clock, make_volunteer, make_shift, assign, record):
    volunteer = make_volunteer(quals=[("training", "verified", None)])
    shift = make_shift(); assignment = assign(volunteer, shift)
    availability = m.Availability(volunteer_id=volunteer.id, month="2026-10", available_dates=[], unavailable_dates=[])
    session.add(availability); session.flush()
    obj, field, after = {
        "Volunteer": (volunteer, "sms_opt_in", False), "Assignment": (assignment, "status", "cancelled"),
        "Qualification": (volunteer.qualifications[0], "status", "expired"), "Event": (shift.event, "title", "Changed title"),
        "Shift": (shift, "slot_index", 2), "Availability": (availability, "unavailable_dates", ["2026-10-04"]),
    }[record]
    before = getattr(obj, field)
    session.info.update(competition_confirmation_required=True, confirmation_now=clock.now())
    setattr(obj, field, after); session.flush()
    assert getattr(obj, field) == before
    a = session.scalar(select(m.Approval).where(m.Approval.kind == "confirm_record"))
    assert a.payload["record"] == record and a.payload["after"][field] == after
    review(session, SendGate(session, clock, __import__('app.sms.mock_provider', fromlist=['MockSMSProvider']).MockSMSProvider()), a)
    assert getattr(obj, field) == after


def test_same_sender_phone_is_not_permission_to_change_consent(session, clock, provider, make_volunteer):
    v = make_volunteer()
    session.info.update(competition_confirmation_required=True, sender_phone=v.phone)
    v.sms_opt_in = False; session.flush()
    assert v.sms_opt_in is True and session.scalar(select(m.Approval)).kind == "confirm_record"


def test_stop_immediately_suppresses_pending_and_queued_but_ack_waits(session, clock, provider, make_volunteer):
    v = make_volunteer(); session.info[c.MODE_KEY] = True
    gate = SendGate(session, clock, provider)
    pending = gate.send(body="Pending", purpose="manual", volunteer=v)
    session.add(m.Message(direction="out", volunteer_id=v.id, phone=v.phone, body="Queued", kind="template", purpose="manual", status="queued", created_at=clock.now()))
    session.flush()
    handle_inbound(session, clock, provider, v.phone, "STOP", lambda b: pytest.fail("No parser for STOP"))
    assert not v.sms_opt_in and provider.sent == []
    assert session.get(m.Approval, pending.approval_id).status == "expired"
    assert session.scalar(select(m.Message).where(m.Message.direction == "out")).status == "blocked_opt_out"
    ack = session.scalar(select(m.Approval).where(m.Approval.status == "pending"))
    assert ack.payload["purpose"] == "stop_confirm"
    review(session, gate, ack)
    assert len(provider.sent) == 1


def test_distress_urgent_attention_is_human_only_and_blocks_reply(session, clock, provider, make_volunteer):
    v = make_volunteer(); pastor = make_volunteer(pastor=True)
    session.info[c.MODE_KEY] = True
    handle_inbound(session, clock, provider, v.phone, "I want to kill myself", lambda b: ParsedMessage(intent="other", sensitive=True, severity="urgent", confidence=1))
    hold = session.scalar(select(m.Escalation).where(m.Escalation.category == "sensitive"))
    assert hold.severity == "urgent" and hold.assigned_to is None
    assert provider.sent == []
    assert SendGate(session, clock, provider).send(body="Reply", purpose="manual", volunteer=v).status == SendStatus.BLOCKED_SENSITIVE


def test_model_guess_cannot_confirm_without_sender_instruction(session, clock, provider, make_volunteer, make_shift, assign):
    v = make_volunteer(); a = assign(v, make_shift(), status="approved")
    session.info[c.MODE_KEY] = True
    result = handle_inbound(session, clock, provider, v.phone, "huh?", lambda b: ParsedMessage(intent="confirm", confidence=1))
    assert result.routed_to == "human_review" and a.status == "approved"


@pytest.fixture
def mode_app(session, clock, make_volunteer):
    volunteers = [make_volunteer("Synthetic " + str(i)) for i in range(3)]
    session.commit()
    token = "synthetic-bridge-" + "x"*40
    app = create_app(Settings(database_url="sqlite://", demo_mode=True, sms_provider="mac_messages", mac_bridge_enabled=True,
        mac_bridge_token=token, admin_password="synthetic-strong-admin-password", mac_demo_phones=",".join(v.phone for v in volunteers), mac_test_sessions=session_json([v.phone for v in volunteers], clock.now()),
        competition_confirmation_required=True, supabase_url="https://fixture.example", supabase_publishable_key="fixture-public", admin_email_allowlist="coordinator@example.test"))
    app.state.session_factory = make_session_factory(session.get_bind())
    app.state.session_factory.configure(info={c.MODE_KEY:True, "confirmation_now":clock.now()})
    app.state.clock = app.state.mac_delivery_clock = clock
    app.state.gloo = ScriptedAgentGloo()
    return app, volunteers, {"Authorization":"Bearer " + token}


def test_authenticated_action_api_enforces_exact_hash_and_disables_legacy(mode_app):
    app, volunteers, headers = mode_app
    with app.state.session_factory() as session:
        a = SendGate(session, app.state.clock, app.state.provider).send(body="Exact API text", purpose="manual", volunteer=session.get(m.Volunteer, volunteers[0].id))
        session.commit(); approval_id = a.approval_id
    with TestClient(app) as client:
        assert client.get("/api/config").json()["humanConfirmationRequired"] is True
        assert client.post(f"/api/proposals/{approval_id}/approve", json={}).status_code == 401
        assert client.get("/approvals").status_code == 403
        app.dependency_overrides[admin] = lambda: {"email":"coordinator@example.test"}
        assert client.post(f"/api/proposals/{approval_id}/approve", json={}).status_code == 409
        proposal = next(p for p in client.get("/api/state").json()["proposals"] if p["id"] == str(approval_id))
        assert proposal["phone"] == volunteers[0].phone and proposal["reply"] == "Exact API text" and proposal["reason"]
        assert client.post(f"/api/proposals/{approval_id}/approve", json={"content_hash":proposal["content_hash"]}).status_code == 200
        assert client.post(f"/api/proposals/{approval_id}/approve", json={"content_hash":proposal["content_hash"]}).status_code == 409
        batch = client.post("/mac/outbound/pull", headers=headers, json={}).json()["messages"]
        assert len(batch) == 1 and batch[0]["confirmation_required"] is True
        item = batch[0]
        assert client.post(f"/mac/outbound/{item['id']}/verify", headers=headers, json={"token":item["token"], "content_hash":item["content_hash"]}).status_code == 200
        client.post("/mac/inbound", headers=headers, json={"guid":"synthetic-stop-after-claim", "phone":volunteers[0].phone, "body":"STOP", "session_id":session_id(volunteers[0].phone)})
        assert client.post(f"/mac/outbound/{item['id']}/verify", headers=headers, json={"token":item["token"], "content_hash":item["content_hash"]}).status_code == 409


def test_real_cancel_and_unoffered_yes_do_not_release_sibling(session, clock, make_shift, assign, monkeypatch, mode_app):
    app, (original, first, second), headers = mode_app
    shift = make_shift("Greeter")
    old = assign(original, shift); session.commit()
    monkeypatch.setattr("app.web.mac_messages.parse_inbound", lambda gloo, body: ParsedMessage(intent="cancel" if body.startswith("Can't") else "accept", confidence=1))
    app.dependency_overrides[admin] = lambda: {"email":"coordinator@example.test"}
    with TestClient(app) as client:
        def inbound(v, body, guid):
            r=client.post("/mac/inbound", headers=headers, json={"guid":guid, "phone":v.phone,"body":body,"session_id":session_id(v.phone)})
            assert r.status_code == 200, r.text
            return r.json()
        inbound(original, "Can't come Sunday", "synthetic-cancel")
        assert client.get("/api/state").json()["assignments"] == []
        assert client.post("/mac/outbound/pull", headers=headers, json={}).json()["messages"] == []
        proposals = client.get("/api/state").json()["proposals"]
        assert not any(p["intent"] == "confirm_text" for p in proposals)
        # No offer was sent, so an unsolicited YES cannot invent a placement.
        inbound(first, "YES", "synthetic-yes")
        inbound(first, "YES", "synthetic-duplicate-yes")
        inbound(second, "YES", "synthetic-sibling-yes")
        after = client.get("/api/state").json()
        assert after["assignments"] == []
        assert not any(p["intent"] == "confirm_text" for p in after["proposals"])
        assert client.post("/mac/outbound/pull", headers=headers, json={}).json()["messages"] == []
    with app.state.session_factory() as s:
        assert s.get(m.Assignment, old.id).status == "cancelled"
        fills = s.scalars(select(m.FillRequest)).all()
        assert len(fills) == 1 and fills[0].shift_id == shift.id
        outreach = s.scalars(select(m.Outreach)).all()
        assert all(row.message_id is None for row in outreach)
        assert s.scalar(select(m.Message).where(m.Message.direction == "out")) is None


@pytest.mark.parametrize("restriction", ["expired", "edited", "unapproved", "qualification"])
def test_mac_claim_rechecks_proof_session_body_and_eligibility(session, clock, make_shift, mode_app, restriction):
    app, volunteers, headers = mode_app
    shift = make_shift(required=["training"] if restriction == "qualification" else [])
    with app.state.session_factory() as s:
        s.info["record_authorized"] = True
        v = s.get(m.Volunteer, volunteers[0].id)
        if restriction == "qualification":
            s.add(m.Qualification(volunteer_id=v.id,type="training",status="verified"));s.flush()
        assignment = m.Assignment(shift_id=shift.id, volunteer_id=v.id, status="confirmed", source="planner",
                                  created_at=clock.now(), updated_at=clock.now())
        s.add(assignment); s.flush()
        a = SendGate(s,clock,app.state.provider).send(body="Synthetic scheduled shift",purpose="confirmation",volunteer=v,
                conversation={"assignment_id": assignment.id, "notice": "scheduled"})
        assert a.status == SendStatus.HELD_FOR_APPROVAL
        proposal=s.get(m.Approval,a.approval_id)
        review(s,SendGate(s,clock,app.state.provider),proposal)
        s.commit();message_id=proposal.payload['message_id']
        assert s.get(m.Message, message_id).status == 'queued'
        assert s.get(m.Notification, f'confirmation:{message_id}') is not None
    with app.state.session_factory() as s:
        s.info['record_authorized']=True
        row=s.get(m.Message,message_id)
        if restriction == 'edited':row.body='Changed after review'
        elif restriction == 'unapproved':s.delete(s.get(m.Notification,f'confirmation:{message_id}'))
        elif restriction == 'qualification':s.scalar(select(m.Qualification)).status='expired'
        else:clock.advance(timedelta(hours=1))
        s.commit()
    with TestClient(app) as client:
        assert client.post('/mac/outbound/pull',headers=headers,json={}).json()['messages']==[]


def test_legacy_mac_proposals_and_care_bodies_are_filtered_before_dashboard_history(session, clock, mode_app):
    app, volunteers, headers=mode_app
    with app.state.session_factory() as s:
        s.add(m.Approval(kind='send_outreach',payload={'phone':volunteers[0].phone,'transport':'mac_messages','body':'Legacy body must stay private'},status='pending',requested_at=clock.now()-timedelta(days=1)))
        s.add(m.Escalation(category='sensitive',severity='normal',summary='Legacy personal summary',related_ids={'volunteer_id':volunteers[0].id},status='open',created_at=clock.now()-timedelta(days=1)))
        s.commit()
    app.dependency_overrides[admin]=lambda:{'email':'coordinator@example.test'}
    with TestClient(app) as client:
        state=client.get('/api/state').json()
        assert state['proposals']==[] and state['escalations']==[]
        assert 'Legacy body' not in json.dumps(state) and 'Legacy personal' not in json.dumps(state)


def test_sender_signup_consent_and_setup_records_have_bounded_authorization(session, clock, provider, make_shift):
    from app.agents.fill_agent import FillContext
    from tests.test_mvp_flows import ProfileGloo
    shift=make_shift('Greeter')
    session.add(m.Policy(key='full_text_onboarding',value={'value':True}));session.flush()
    session.info[c.MODE_KEY]=True
    gloo=ProfileGloo([{'signup':True,'first_name':'Synthetic','last_name':'Volunteer'},
                     {'understood':True,'role_ids':[shift.role_id]},
                     {'understood':True,'weekdays':[6],'services':['sun_9'],'max_per_month':2}])
    ctx=FillContext(session,clock,provider,gloo)
    parser=lambda b:ParsedMessage(intent='other',confidence=1)
    phone='+12025550188'
    handle_inbound(session,clock,provider,phone,'JOIN Synthetic Volunteer',parser,ctx=ctx,allow_signup=True)
    v=session.scalar(select(m.Volunteer))
    assert v is not None and v.preferences['consent_pending'] and not v.sms_opt_in
    handle_inbound(session,clock,provider,phone,'YES',parser,ctx=ctx,allow_signup=True)
    assert v.sms_opt_in and v.status=='active'
    handle_inbound(session,clock,provider,phone,'Greeter',parser,ctx=ctx)
    handle_inbound(session,clock,provider,phone,'Sundays 9am twice a month',parser,ctx=ctx)
    assert v.preferences['onboarding_stage']=='complete' and provider.sent==[]
    assert session.scalar(select(m.Approval).where(m.Approval.kind=='confirm_record')) is None
    prompts = session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text')).all()
    assert len(prompts) == 3
    assert all(p.payload['purpose'] == 'signup_reply' and p.payload['conversation']['intake_fields'] for p in prompts)


def test_bulk_record_write_cannot_bypass_human_review(session, clock, make_volunteer):
    from sqlalchemy import update
    v=make_volunteer();session.info[c.MODE_KEY]=True
    with pytest.raises(ValueError):session.execute(update(m.Volunteer).where(m.Volunteer.id==v.id).values(sms_opt_in=False))
    assert v.sms_opt_in


def test_generic_approved_flag_cannot_bypass_exact_review(session, clock, provider, make_volunteer):
    v=make_volunteer();session.info[c.MODE_KEY]=True
    outcome=SendGate(session,clock,provider).send(body='Synthetic',purpose='manual',volunteer=v,_approved=True)
    assert outcome.status==SendStatus.HELD_FOR_APPROVAL and provider.sent==[]


@pytest.mark.parametrize('proof', ['missing','expired','changed','uncertain'])
def test_native_preflight_checks_exact_approval_and_uncertain_never_resends(tmp_path,proof):
    import httpx
    from datetime import datetime, timezone
    from app.integrations.mac_messages import MacWorker
    from tests.test_mac_messages import config, ReaderFixture, PHONE
    now=datetime.now(timezone.utc)
    item={'id':90,'token':'t'*64,'phone':PHONE,'session_id':session_id(PHONE),'body':'Exact native fixture',
          'confirmation_required':True,'content_hash':'h'*64,'approval_expires_at':(now+timedelta(minutes=30)).isoformat()}
    if proof=='missing':item.pop('confirmation_required')
    if proof=='expired':item['approval_expires_at']=(now-timedelta(minutes=1)).isoformat()
    calls=[];sent=[];acks=[]
    def server(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/pull'):return httpx.Response(200,json={'messages':[item]})
        if request.url.path.endswith('/verify'):
            return httpx.Response(200,json={'verified':True,'phone':PHONE,'body':'Changed' if proof=='changed' else item['body'],'content_hash':item['content_hash']})
        acks.append(json.loads(request.content));return httpx.Response(200,json={})
    cfg={**config(tmp_path),'competition_confirmation_required':True}
    worker=MacWorker(cfg,live=True,client=httpx.Client(transport=httpx.MockTransport(server)),reader=ReaderFixture(),sender=lambda p,b:sent.append((p,b)) or 'uncertain')
    if proof=='uncertain':
        worker.once();worker.once()
        assert len(sent)==1 and all(ack['outcome']=='uncertain' for ack in acks)
        assert calls.count('/mac/outbound/90/verify')==1
    else:
        with pytest.raises(ValueError):worker.once()
        assert sent==[]


def test_new_automated_assignment_stays_proposed_until_exact_human_review(session, clock, provider, make_volunteer, make_shift):
    v=make_volunteer();slot=make_shift()
    session.info.update(competition_confirmation_required=True,confirmation_now=clock.now())
    pending=m.Assignment(shift_id=slot.id,volunteer_id=v.id,status='confirmed',source='fill',created_at=clock.now(),updated_at=clock.now())
    session.add(pending);session.flush()
    assert session.scalar(select(m.Assignment)) is None
    proposal=session.scalar(select(m.Approval))
    assert proposal.payload['before'] is None
    review(session,SendGate(session,clock,provider),proposal)
    assert session.scalar(select(m.Assignment)).status=='confirmed'


def test_existing_schedule_confirmation_uses_own_slot_without_double_booking_false_positive(session, clock, provider, make_volunteer, make_shift, assign):
    v=make_volunteer();a=assign(v,make_shift(),status='approved')
    session.info.update(competition_confirmation_required=True,confirmation_now=clock.now())
    a.status='confirmed';session.flush()
    assert a.status=='approved'
    review(session,SendGate(session,clock,provider),session.scalar(select(m.Approval)))
    assert a.status=='confirmed'


def test_mutable_json_write_cannot_bypass_record_hold(session, clock, make_volunteer):
    from sqlalchemy.orm.attributes import flag_modified
    v=make_volunteer(prefs={'max_per_month':2})
    session.info.update(competition_confirmation_required=True,confirmation_now=clock.now())
    v.preferences['max_per_month']=50;flag_modified(v,'preferences');session.flush()
    assert v.preferences['max_per_month']==2
    assert session.scalar(select(m.Approval)).payload['after']['preferences']['max_per_month']==50


@pytest.mark.parametrize('value',['treu','', 'optional'])
def test_malformed_activation_flag_fails_closed(monkeypatch,value):
    from app.config import settings_from_env
    monkeypatch.setenv('COMPETITION_CONFIRMATION_REQUIRED',value)
    with pytest.raises(ValueError):settings_from_env()


def test_direct_live_provider_is_outside_supported_confirmation_scope():
    with pytest.raises(ValueError):create_app(Settings(database_url='sqlite://',competition_confirmation_required=True,sms_provider='twilio',live_sms=True))


@pytest.mark.parametrize('body', ['Please do not confirm it.', "Don't accept my shift.", 'Can I confirm my booking?', 'Maybe I can cover it.', 'What if I cannot make my shift?'])
def test_negated_questions_and_tentative_mentions_do_not_authorize_model_confirmation(session,clock,provider,make_volunteer,make_shift,assign,body):
    v=make_volunteer();booking=assign(v,make_shift(),status='approved')
    session.info[c.MODE_KEY]=True
    result=handle_inbound(session,clock,provider,v.phone,body,lambda b:ParsedMessage(intent='confirm',confidence=1))
    assert result.routed_to=='human_review' and booking.status=='approved' and provider.sent==[]


def test_cancellation_containing_my_shift_keeps_priority_over_booking_status(session,clock,provider,make_volunteer,make_shift,assign):
    from app.agents.fill_agent import FillContext
    v=make_volunteer();booking=assign(v,make_shift(),status='confirmed');make_volunteer()
    session.info[c.MODE_KEY]=True
    result=handle_inbound(session,clock,provider,v.phone,'I cannot make my shift.',lambda b:ParsedMessage(intent='cancel',confidence=1),ctx=FillContext(session,clock,provider,ScriptedAgentGloo()))
    assert result.routed_to=='fill_agent' and booking.status=='cancelled' and provider.sent==[]


@pytest.mark.parametrize('status',['queued','sent'])
def test_numbered_cancellation_requires_delivered_app_clarification(session,clock,provider,make_volunteer,make_shift,assign,status):
    from app.agents.fill_agent import FillContext
    v=make_volunteer();booking=assign(v,make_shift(),status='confirmed')
    session.add(m.Message(direction='out',volunteer_id=v.id,phone=v.phone,body='Which shift?',purpose='clarify_shift',kind='template',status=status,created_at=clock.now()));session.flush()
    session.info[c.MODE_KEY]=True
    handle_inbound(session,clock,provider,v.phone,'1',lambda b:ParsedMessage(intent='other',confidence=1),ctx=FillContext(session,clock,provider,ScriptedAgentGloo()))
    assert booking.status==('cancelled' if status=='sent' else 'confirmed')


def test_unrelated_name_mentions_cannot_authorize_model_signup(session,clock,provider):
    from app.agents.fill_agent import FillContext
    from tests.test_mvp_flows import ProfileGloo
    session.info[c.MODE_KEY]=True
    gloo=ProfileGloo([{'signup':True,'first_name':'Synthetic','last_name':'Volunteer'}])
    result=handle_inbound(session,clock,provider,'+12025550188','What does Synthetic Volunteer mean?',lambda b:ParsedMessage(intent='other'),ctx=FillContext(session,clock,provider,gloo),allow_signup=True)
    assert result.routed_to=='signup_identity_review' and session.scalar(select(m.Volunteer)) is None and provider.sent==[]


def test_unsupported_ingress_cannot_invoke_model_in_confirmation_mode():
    app=create_app(Settings(database_url='sqlite://',competition_confirmation_required=True,twilio_auth_token='synthetic-token'))
    with TestClient(app) as client:
        assert client.post('/sms/inbound',data={'From':'+12025550188','Body':'Synthetic'}).status_code==503
