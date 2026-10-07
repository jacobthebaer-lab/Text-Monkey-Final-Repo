"""Natural affirmative replies require a reviewed delivered invitation, never a model hint."""
from datetime import datetime,timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from app.agents.fill_agent import FillContext
from app.core import confirmations,offer_windows
from app.core.inbound import handle_inbound
from app.core.send_gate import SendGate
from app.db import models as m
from app.llm.parser import ParsedMessage
from tests.test_clyde_algorithm import enable,start
from tests.test_fill_agent import ScriptedAgentGloo


@pytest.fixture(autouse=True)
def isolated_transports(monkeypatch):
    def forbidden(*args,**kwargs):pytest.fail('No external/native actions in affirmative regression')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)
    import requests.sessions,urllib.request,app.integrations.mac_messages as native
    monkeypatch.setattr(requests.sessions.Session,'request',forbidden)
    monkeypatch.setattr(urllib.request,'urlopen',forbidden)
    monkeypatch.setattr(native,'send_native',forbidden)


@pytest.fixture
def delivered_offer(session,clock,provider,make_volunteer,make_shift,request):
    zone=ZoneInfo('America/Denver');clock.set_time(datetime(2026,10,10,8,tzinfo=zone))
    role_name,day=getattr(request,'param',('Greeter',11))
    person=make_volunteer('Fictional Avery',prefs={'onboarding_stage':'complete','interested_roles':[role_name]})
    shift=make_shift(role_name,starts=datetime(2026,10,day,9,tzinfo=zone),minutes=60)
    fill=m.FillRequest(shift_id=shift.id,state='in_progress',urgency='normal',current_tranche=1,created_at=clock.now())
    session.add(fill);session.flush();enable(session)
    session.info[confirmations.MODE_KEY]=True
    gloo=ScriptedAgentGloo();context=FillContext(session,clock,provider,gloo)
    start(context,fill)
    approval=session.scalar(select(m.Approval).where(m.Approval.kind=='confirm_text',m.Approval.status=='pending'))
    assert approval is not None and not provider.sent
    gate=SendGate(session,clock,provider);gate.gloo=gloo
    confirmations.decide(session,gate,approval,approve=True,actor='reviewer@example.invalid',
                         expected=approval.payload['content_hash'],now=clock.now())
    offer=session.scalar(select(m.Outreach).where(m.Outreach.fill_request_id==fill.id))
    message=session.get(m.Message,offer.message_id)
    assert approval.status=='approved' and message.status=='sent'
    assert message.provider_sid.startswith('MOCK') and provider.sent[0].body==approval.payload['body']
    assert offer_windows.metadata(session,offer).state=='offer_active'
    session.commit();session.info[confirmations.MODE_KEY]=False
    return SimpleNamespace(person=person,shift=shift,fill=fill,offer=offer,ctx=context,message=message)


def receive(session,clock,provider,f,body,*,intent='accept',mode=True,hint='Sunday'):
    session.info[confirmations.MODE_KEY]=mode
    return handle_inbound(session,clock,provider,f.person.phone,body,
        lambda _:ParsedMessage(intent=intent,confidence=.99,shift_hint=hint),ctx=f.ctx)


@pytest.mark.parametrize('mode',[False,True])
@pytest.mark.parametrize('intent',['accept','confirm'])
@pytest.mark.parametrize('body',['Yes, I can greet this Sunday','Yes, I can help',
                                 'Sure, I can cover Greeter this Sunday',
                                 'Yes, I can serve as a Greeter this Sunday'])
def test_positive_role_day_reply_books_one_current_reviewed_offer(
    session,clock,provider,delivered_offer,body,intent,mode
):
    f=delivered_offer
    result=receive(session,clock,provider,f,body,intent=intent,mode=mode,hint='Sunday')
    assert result.routed_to=='fill_agent' and result.notes[-1]=='filled'
    assert f.fill.state=='filled' and f.offer.response=='yes'
    booking=session.scalar(select(m.Assignment));assert booking.shift_id==f.shift.id and booking.volunteer_id==f.person.id
    assert booking.status=='confirmed'
    assert session.info.get('sender_schedule_action') is None
    session.commit();session.expire_all()
    receive(session,clock,provider,f,body,intent=intent,mode=mode)
    assert len(session.scalars(select(m.Assignment)).all())==1
    assert len(session.scalars(select(m.Outreach)).all())==1


@pytest.mark.parametrize('mode',[False,True])
@pytest.mark.parametrize('body',['Yes, I can greet this Monday','Yes, I can cover Nursery this Sunday',
    "Yes, I can't greet this Sunday",'Yes, I can greet this Sunday if my meeting ends',
    'Yes, I can greet this Sunday?','Yes, I can greet only the first hour',
    'Yes, I can greet until 10:30','Yes, I might greet this Sunday'])
def test_wrong_scope_or_conditional_reply_cannot_accept_even_with_matching_model_hint(
    session,clock,provider,delivered_offer,body,mode
):
    f=delivered_offer
    result=receive(session,clock,provider,f,body,mode=mode,hint='Greeter Sunday')
    assert result.routed_to=='human_review'
    assert f.fill.state!='filled' and f.offer.response=='none'
    assert session.scalar(select(m.Assignment)) is None
    assert not any('confirmed for' in text.body for text in provider.sent)


@pytest.mark.parametrize('mode',[False,True])
@pytest.mark.parametrize('change',['uncertain','dispatching','wrong_phone','wrong_person','body','expired'])
def test_positive_wording_does_not_replace_fresh_delivery_identity_and_expiry_proof(
    session,clock,provider,delivered_offer,make_volunteer,change,mode
):
    f=delivered_offer
    if change in {'uncertain','dispatching'}:f.message.status=change
    elif change=='wrong_phone':f.message.phone=make_volunteer().phone
    elif change=='wrong_person':f.message.volunteer_id=make_volunteer().id
    elif change=='body':f.message.body='Changed source invitation'
    else:clock.set_time(offer_windows.metadata(session,f.offer).expires_at)
    session.commit();session.expire_all()
    receive(session,clock,provider,f,'Yes, I can greet this Sunday',mode=mode)
    assert f.fill.state!='filled' and f.offer.response!='yes'
    assert session.scalar(select(m.Assignment)) is None


@pytest.mark.parametrize('mode',[False,True])
def test_positive_role_day_reply_without_invitation_never_promotes_existing_booking(
    session,clock,provider,make_volunteer,make_shift,assign,mode
):
    clock.set_time(datetime(2026,10,10,8,tzinfo=ZoneInfo('America/Denver')))
    person=make_volunteer(prefs={'onboarding_stage':'complete'})
    booking=assign(person,make_shift('Greeter',starts=clock.now()+timedelta(days=1)),status='approved')
    session.commit();f=SimpleNamespace(person=person,ctx=FillContext(session,clock,provider,ScriptedAgentGloo()))
    receive(session,clock,provider,f,'Yes, I can greet this Sunday',intent='confirm',mode=mode)
    assert booking.status=='approved' and session.scalar(select(m.FillRequest)) is None


@pytest.mark.parametrize('delivered_offer',[('Coffee',11),('Greeter',18)],indirect=True)
@pytest.mark.parametrize('mode',[False,True])
@pytest.mark.parametrize('intent',['accept','confirm'])
def test_matching_model_hint_cannot_supply_missing_role_or_this_week_authority(
    session,clock,provider,delivered_offer,mode,intent
):
    f=delivered_offer
    receive(session,clock,provider,f,'Yes, I can greet this Sunday',intent=intent,mode=mode,
            hint=f.shift.role.name+' Sunday')
    assert f.fill.state!='filled' and f.offer.response=='none'
    assert session.scalar(select(m.Assignment)) is None


@pytest.mark.parametrize('intent',['accept','confirm'])
def test_model_hint_cannot_override_literal_role_day_with_unique_delivered_source(
    session,clock,provider,delivered_offer,intent
):
    f=delivered_offer
    result=receive(session,clock,provider,f,'Yes, I can greet this Sunday',intent=intent,
                   mode=True,hint='Wednesday')
    assert result.routed_to=='fill_agent' and f.fill.state=='filled'
    assert session.scalar(select(m.Assignment)).shift_id==f.shift.id
