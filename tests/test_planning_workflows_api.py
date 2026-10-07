"""Fictional single-church scope, authenticated owner doubles, mock delivery only."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.clock import FakeClock
from app.config import Settings
from app.core import availability_review as review, confirmations
from app.db import models as m
from app.main import create_app
from app.web import planning_workflows, texty
from tests.conftest import NOW
from tests.test_planning_composition import CopyGloo
from tests.test_admin_setup import OWNER_A, OWNER_B

PREFIX="/api/planning/availability-collections"


@pytest.fixture
def planning_client(tmp_path):
    # Other planning feature tests require their original global exact mode.
    yield from _collection_client(tmp_path, True)


@pytest.fixture(params=[True, False], ids=["global_exact", "global_automatic"])
def collection_client(tmp_path, request):
    yield from _collection_client(tmp_path, request.param)


def _collection_client(tmp_path, global_exact):
    app=create_app(Settings(database_url=f"sqlite:///{tmp_path}/planning.db",sms_provider="mock",
        demo_mode=False,automation_enabled=False,competition_confirmation_required=global_exact))
    app.include_router(planning_workflows.router)
    app.state.clock=FakeClock(NOW)
    app.state.gloo=CopyGloo()
    user={"id":OWNER_A,"email":"admin@example.test","email_confirmed_at":"2026-10-01"}
    app.dependency_overrides[texty.admin]=lambda:user
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.info["record_authorized"]=True
            session.add_all([m.Volunteer(name=f"Fictional Volunteer {i}",phone=f"+1202555010{i}",
                status="active",sms_opt_in=True,preferences={},created_at=NOW) for i in (1,2)])
            session.commit()
        yield client,app,user


def requested(client, **extra):
    response=client.post(PREFIX,json={"month":"2026-11",**extra})
    assert response.status_code==200,response.text
    return response.json()["collection"]


def decision(client,parent,action="approve",**extra):
    return client.post(f"{PREFIX}/{parent['id']}/{action}",json={"content_hash":parent["content_hash"],**extra})


def mutate(app,function):
    with app.state.session_factory() as session:
        session.info["record_authorized"]=True
        function(session);session.commit()


def test_parent_preview_is_exact_owner_bound_and_never_enables_delivery(collection_client):
    client,app,_=collection_client; parent=requested(client)
    assert parent["status"]=="pending" and parent["scope"]["recipient_count"]==2
    assert parent["scope"]["recipients"][0]=={"volunteer_id":1,"name":"Fictional Volunteer 1","phone":"+12025550101"}
    assert parent["text_review_ids"]==[] and not app.state.gloo.calls and not app.state.provider.sent
    assert client.get(PREFIX).json()["collections"]==[parent]
    response=decision(client,parent).json()
    assert response["collection"]["status"]=="approved" and response["collection"]["composition_status"]=="not_started"
    assert response["sent"]==0 and not response["delivery_enabled"] and not response["scheduler_activated"]
    assert not app.state.gloo.calls and not app.state.provider.sent and not app.state.settings.automation_enabled
    with app.state.session_factory() as session:
        child=session.get(m.Approval,response["collection"]["collection_id"])
        assert review.approved_collection_problem(session,child,app.state.clock.now()) is None


@pytest.mark.parametrize("action",["read","approve","reject","retry"])
def test_foreign_owner_cannot_find_or_decide_parent(collection_client,action):
    client,app,user=collection_client; parent=requested(client);user["id"]=OWNER_B
    assert client.get(PREFIX).json()=={"collections":[]}
    response=client.get(f"{PREFIX}/{parent['id']}") if action=="read" else decision(client,parent,action)
    assert response.status_code==404 and not app.state.gloo.calls and not app.state.provider.sent


@pytest.mark.parametrize("field",["owner_id","recipient_ids","scope","collection_owner_id"])
def test_client_scope_and_owner_cannot_override_server_preview(collection_client,field):
    client,app,_=collection_client
    assert client.post(PREFIX,json={"month":"2026-11",field:OWNER_B}).status_code==422
    parent=requested(client)
    assert decision(client,parent,**{field:OWNER_B}).status_code==422
    assert not app.state.gloo.calls and not app.state.provider.sent


@pytest.mark.parametrize("month",["2026-00","2026-1","2026-09","2027-11",True,None])
def test_bad_past_or_out_of_horizon_month(collection_client,month):
    assert collection_client[0].post(PREFIX,json={"month":month}).status_code==422


def test_request_retry_is_idempotent_and_uuid_cannot_change_month(collection_client):
    client,app,_=collection_client; key=str(uuid4()); parent=requested(client,request_id=key)
    assert requested(client,request_id=key)==parent
    assert requested(client)["id"]==parent["id"]
    assert client.post(PREFIX,json={"month":"2026-12","request_id":key}).status_code==409
    assert not app.state.gloo.calls and not app.state.provider.sent
    with app.state.session_factory() as session:
        assert all(len(key)<=80 for key in session.scalars(select(m.Policy.key)))


def test_wrong_hash_or_tampered_stored_scope_cannot_authorize(collection_client):
    client,app,_=collection_client; parent=requested(client)
    assert client.post(f"{PREFIX}/{parent['id']}/approve",json={"content_hash":"0"*64}).status_code==409
    def tamper(session):
        p=session.get(m.Approval,parent["id"])
        p.payload={**p.payload,"month":"2026-12"}
    mutate(app,tamper)
    assert decision(client,parent).status_code==409
    assert not app.state.gloo.calls and not app.state.provider.sent


@pytest.mark.parametrize("change",["phone","name","consent","newcomer","availability","timezone","care"])
def test_scope_changes_before_approval_require_new_hash(collection_client,change):
    client,app,_=collection_client; parent=requested(client)
    def update(session):
        v=session.get(m.Volunteer,1)
        if change=="phone":v.phone="+12025550109"
        if change=="name":v.name="Renamed Fictional Volunteer"
        if change=="consent":v.sms_opt_in=False
        if change=="newcomer":session.add(m.Volunteer(name="New Fictional Person",phone="+12025550103",status="active",sms_opt_in=True,preferences={},created_at=NOW))
        if change=="availability":session.add(m.Availability(volunteer_id=1,month="2026-11",available_dates=["2026-11-01"]))
        if change=="timezone":session.add(m.Policy(key="church_timezone",value={"value":"America/New_York"}))
        if change=="care":session.add(m.Escalation(category="sensitive",severity="normal",summary="Fictional hold",status="open",related_ids={"volunteer_id":1},created_at=NOW))
    mutate(app,update)
    assert decision(client,parent).status_code==409 and not app.state.gloo.calls
    fresh=requested(client,request_id=str(uuid4()))
    assert fresh["id"]!=parent["id"] and fresh["content_hash"]!=parent["content_hash"]
    assert not app.state.provider.sent


def test_expired_review_and_month_rollover_hold_without_composition(collection_client):
    client,app,_=collection_client; parent=requested(client)
    app.state.clock.advance(timedelta(hours=2))
    assert decision(client,parent).status_code==409
    assert client.get(f"{PREFIX}/{parent['id']}").json()["collection"]["status"]=="expired"
    parent=requested(client,request_id=str(uuid4()))
    app.state.clock.set_time(NOW.replace(month=12))
    assert decision(client,parent).status_code==409 and not app.state.gloo.calls and not app.state.provider.sent


def test_rejection_and_repeat_decisions_never_prepare_text(collection_client):
    client,app,_=collection_client; parent=requested(client)
    first=decision(client,parent,"reject").json();second=decision(client,parent,"reject").json()
    assert first==second and first["collection"]["status"]=="rejected"
    assert decision(client,parent,"approve").status_code==409
    assert decision(client,parent,"retry").status_code==409
    assert not app.state.gloo.calls and not app.state.provider.sent


def test_explicit_prepare_stages_one_exact_recipient_review_at_a_time(collection_client):
    client,app,_=collection_client;parent=requested(client)
    approved=decision(client,parent).json()
    assert decision(client,parent).json()==approved and not app.state.gloo.calls
    first=decision(client,parent,"retry").json()
    assert len(first["text_review_ids"])==1 and first["collection"]["composition_status"]=="reviews_pending"
    second=decision(client,parent,"retry").json()
    assert len(second["text_review_ids"])==2 and second["collection"]["suppressed_recipient_count"]==0
    assert decision(client,parent,"retry").json()==second
    with app.state.session_factory() as session:
        child=second["collection"]["collection_id"]
        receipts=[session.get(m.Policy,f"job:availability:{child}:{vid}:0") for vid in (1,2)]
        assert all(row.value["state"]=="held_for_approval" for row in receipts)
        reviews=[session.get(m.Approval, row.value["approval_id"]) for row in receipts]
        assert [row.payload["phone"] for row in reviews]==["+12025550101","+12025550102"]
        assert all(row.status=="pending" and confirmations.valid(row,app.state.clock.now()) for row in reviews)
        assert all(row.payload["conversation"]["binding"]["owner_id"]==OWNER_A for row in reviews)
        assert all("2026-11" in row.payload["body"] and "same as usual" in row.payload["body"] for row in reviews)
        assert confirmations.enabled(session)==app.state.settings.competition_confirmation_required
        assert not session.scalar(select(m.Message).where(m.Message.direction=="out"))
    assert app.state.gloo.calls==2 and not app.state.provider.sent and not app.state.settings.automation_enabled


def test_concurrent_prepare_does_not_repeat_the_same_model_or_review(collection_client):
    client,app,_=collection_client;parent=requested(client);decision(client,parent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:decision(client,parent,"retry"),range(2)))
    assert all(r.status_code==200 for r in results) and app.state.gloo.calls==2
    result=client.get(f"{PREFIX}/{parent['id']}").json()["collection"]
    assert len(result["text_review_ids"])==2 and result["suppressed_recipient_count"]==0
    with app.state.session_factory() as session:
        receipts=session.scalars(select(m.Policy).where(m.Policy.key.startswith("job:availability:"))).all()
        assert len(receipts)==2 and {r.value["volunteer_id"] for r in receipts}=={1,2}
        assert len({r.value["approval_id"] for r in receipts})==2
    assert not app.state.provider.sent


def test_after_approval_newcomer_or_changed_destination_cannot_expand_scope(collection_client):
    client,app,_=collection_client;parent=requested(client);decision(client,parent)
    def update(session):
        session.get(m.Volunteer,1).phone="+12025550109"
        session.add(m.Volunteer(name="New Person",phone="+12025550103",status="active",sms_opt_in=True,preferences={},created_at=NOW))
    mutate(app,update)
    result=decision(client,parent,"retry").json()
    assert len(result["text_review_ids"])==1 and result["collection"]["composition_status"]=="held"
    assert "Recipient details changed" in result["collection"]["hold_reason"]
    assert result["collection"]["suppressed_recipient_count"]==0
    with app.state.session_factory() as session:
        child=result["collection"]["collection_id"]
        receipts=session.scalars(select(m.Policy).where(m.Policy.key.startswith(f"job:availability:{child}:"))).all()
        assert len(receipts)==1 and receipts[0].value["phone"]=="+12025550102"
        assert session.get(m.Policy,f"job:availability:{child}:1:0") is None
        assert session.get(m.Policy,f"job:availability:{child}:3:0") is None
    assert app.state.gloo.calls==1 and not app.state.provider.sent


def test_collection_gloo_outage_is_bounded_and_never_stages_seed_copy(collection_client):
    from app.llm.gloo_client import GlooUnavailableError
    class Outage(CopyGloo):
        def create_response(self,**kwargs):
            self.calls+=1;raise GlooUnavailableError("synthetic outage")
    client,app,_=collection_client;app.state.gloo=Outage()
    mutate(app, lambda session: setattr(session.get(m.Volunteer,2), 'status', 'inactive'))
    parent=requested(client);decision(client,parent)
    first=decision(client,parent,"retry").json()
    assert first["collection"]["retry_at"] and first["text_review_ids"]==[]
    assert decision(client,parent,"retry").status_code==200 and app.state.gloo.calls==1
    for _ in range(2):
        app.state.clock.advance(timedelta(minutes=2))
        result=decision(client,parent,"retry").json()
    with app.state.session_factory() as session:
        child=result["collection"]["collection_id"]
        receipt=session.get(m.Policy,f"job:availability:{child}:1:0")
        assert receipt.value["state"]=="gloo_blocked" and receipt.value["gloo_attempts"]==3
        assert not session.scalar(select(m.Approval).where(m.Approval.kind=="confirm_text"))
    assert result["text_review_ids"]==[] and app.state.gloo.calls==3 and not app.state.provider.sent


def test_generic_legacy_child_cannot_compose_in_either_mode(collection_client):
    from app.agents.fill_agent import FillContext
    from app.agents.planning_agent import collect
    client,app,_=collection_client
    with app.state.session_factory() as session:
        a=m.Approval(kind="collect_availability",status="approved",payload={"month":"2026-11"},requested_at=NOW)
        session.add(a);session.flush()
        result=collect(FillContext(session,app.state.clock,app.state.provider,app.state.gloo),a)
        assert result["reviews"]==[] and result["sent"]==[]
        if app.state.settings.competition_confirmation_required:
            assert "signed-in" in result["held"]
        else:
            receipts=session.scalars(select(m.Policy).where(m.Policy.key.startswith('job:availability:'))).all()
            assert len(receipts)==2 and all(row.value['state']=='blocked_policy' for row in receipts)
    assert not app.state.gloo.calls and not app.state.provider.sent


@pytest.mark.parametrize('hold', ['quiet', 'budget', 'phone_care', 'stop'])
def test_post_scope_contact_holds_precede_composition(collection_client, hold):
    client,app,_=collection_client
    mutate(app, lambda session: setattr(session.get(m.Volunteer,2), 'status', 'inactive'))
    parent=requested(client);decision(client,parent)
    if hold=='quiet':
        app.state.clock.set_time(NOW.replace(hour=23))
    else:
        def update(session):
            person=session.get(m.Volunteer,1)
            if hold=='budget':
                session.add(m.Policy(key='monthly_ask_budget_per_volunteer', value={'value':1}))
                session.add(m.Message(direction='out', volunteer_id=1, phone=person.phone,
                    body='Prior synthetic ask', purpose='availability_ask', kind='ai', status='sent', created_at=NOW))
            if hold=='phone_care':
                session.add(m.Escalation(category='sensitive', severity='normal', summary='Fictional care hold',
                    related_ids={'phone':person.phone}, status='open', created_at=NOW))
            if hold=='stop':
                session.add(m.Policy(key='sms_opt_out:'+person.phone,value={'value':True}))
        mutate(app,update)
    result=decision(client,parent,'retry').json()
    assert result['text_review_ids']==[] and not app.state.gloo.calls and not app.state.provider.sent
    if hold=='quiet':
        app.state.clock.set_time(NOW.replace(day=2,hour=8))
        resumed=decision(client,parent,'retry').json()
        assert len(resumed['text_review_ids'])==1 and app.state.gloo.calls==1 and not app.state.provider.sent


@pytest.mark.parametrize('invalid', ['emdash', 'missing_month'])
def test_invalid_gloo_collection_copy_never_stages_or_uses_local_fallback(collection_client, invalid):
    from types import SimpleNamespace
    class Invalid(CopyGloo):
        def create_response(self, **kwargs):
            self.calls+=1
            facts=json.loads(kwargs['input'])
            body=facts['approved_message']+'\u2014' if invalid=='emdash' else 'Invented unrelated reply'
            return SimpleNamespace(output_text=body, usage=None)
    client,app,_=collection_client;app.state.gloo=Invalid()
    parent=requested(client);decision(client,parent)
    result=decision(client,parent,'retry').json()
    assert result['text_review_ids']==[] and result['collection']['retry_at']
    with app.state.session_factory() as session:
        assert not session.scalar(select(m.Approval).where(m.Approval.kind=='confirm_text'))
        assert not session.scalar(select(m.Notification).where(m.Notification.purpose=='availability_composition'))
    assert app.state.gloo.calls==1 and not app.state.provider.sent


@pytest.mark.parametrize('global_exact', [True, False])
def test_real_auth_dependency_prepares_only_after_scope_and_exact_hash(tmp_path, monkeypatch, global_exact):
    app=create_app(Settings(database_url=f'sqlite:///{tmp_path}/signed-in.db', sms_provider='mock',
        demo_mode=False, automation_enabled=False, competition_confirmation_required=global_exact,
        supabase_url='https://auth.example.test', supabase_publishable_key='synthetic-public-key',
        admin_email_allowlist='allowed@example.test'))
    app.state.clock=FakeClock(NOW);app.state.gloo=CopyGloo()
    original=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={
        'id':OWNER_A, 'email':'allowed@example.test', 'email_confirmed_at':'2026-10-01'}))
    monkeypatch.setattr(texty.httpx,'AsyncClient',lambda **kwargs:original(transport=transport,**kwargs))
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.info['record_authorized']=True
            session.add(m.Volunteer(name='Fictional Authenticated Volunteer', phone='+12025550131',
                status='active', sms_opt_in=True, preferences={}, created_at=NOW));session.commit()
        assert client.post(PREFIX,json={'month':'2026-11'}).status_code==401
        client.headers['Authorization']='Bearer synthetic-token'
        parent=requested(client)
        assert decision(client,parent,'retry').status_code==409
        assert decision(client,parent).status_code==200 and app.state.gloo.calls==0
        staged=decision(client,parent,'retry').json()
        assert len(staged['text_review_ids'])==1 and app.state.gloo.calls==1
        with app.state.session_factory() as session:
            exact=session.get(m.Approval,staged['text_review_ids'][0])
            assert exact.status=='pending' and exact.payload['phone']=='+12025550131'
            assert exact.payload['conversation']['binding']['owner_id']==OWNER_A
            digest=exact.payload['content_hash']
            assert confirmations.enabled(session)==global_exact
        assert client.post(f"/api/proposals/{exact.id}/approve",json={'content_hash':'0'*64}).status_code==409
        assert not app.state.provider.sent
        response=client.post(f"/api/proposals/{exact.id}/approve",json={'content_hash':digest})
        assert response.status_code==200,response.text
        with app.state.session_factory() as session:
            saved=session.get(m.Approval,exact.id)
            assert saved.status=='approved' and saved.payload['message_id']
            outgoing=session.get(m.Message,saved.payload['message_id'])
            assert outgoing.phone==exact.payload['phone'] and outgoing.body==exact.payload['body']
            assert outgoing.purpose=='availability_ask' and '\u2014' not in outgoing.body
            assert confirmations.enabled(session)==global_exact
        assert app.state.gloo.calls==1 and not app.state.settings.automation_enabled
        assert len(decision(client,parent,'retry').json()['text_review_ids'])==0


@pytest.mark.parametrize("verified,email",[(None,"allowed@example.test"),("2026-10-01","other@example.test")])
def test_actual_auth_dependency_rejects_unverified_or_unallowlisted_account(tmp_path,monkeypatch,verified,email):
    app=create_app(Settings(database_url=f"sqlite:///{tmp_path}/auth.db",automation_enabled=False,
        sms_provider="mock",competition_confirmation_required=True,supabase_url="https://auth.example.test",
        supabase_publishable_key="synthetic-public-key",admin_email_allowlist="allowed@example.test"))
    app.include_router(planning_workflows.router)
    original=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"id":OWNER_A,"email":email,"email_confirmed_at":verified}))
    monkeypatch.setattr(texty.httpx,"AsyncClient",lambda **kwargs:original(transport=transport,**kwargs))
    with TestClient(app) as client:
        assert client.get(PREFIX).status_code==401
        assert client.post(PREFIX,headers={"Authorization":"Bearer synthetic-token"},json={"month":"2026-11"}).status_code==403
