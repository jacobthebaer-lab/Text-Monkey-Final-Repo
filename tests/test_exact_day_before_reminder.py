"""The administrator's submitted wording, with fictional saved shifts only."""
import json
from datetime import timedelta
from types import SimpleNamespace as NS
import pytest
from sqlalchemy import select
from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core import confirmations, reminders
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.gloo_client import NullGloo
from app.llm.parser import ParsedMessage
from tests.test_planning_composition import human_change, reviewed

LITERAL = "Hey Clyde, Text Monkey here. You're signed up to greet tomorrow at 10am. If we don't hear from you, we'll assume you're good to go. If you can't make it, just let me know."


class ExactGloo:
    settings=Settings(gloo_signup_replies=False)
    def __init__(self, transform=lambda body:body): self.calls=0;self.transform=transform
    def create_response(self, *, input, **kwargs):
        self.calls+=1
        if isinstance(input,str):
            facts=json.loads(input)
            return NS(output_text=self.transform(facts["approved_message"]) if facts.get("exact_copy") else facts["approved_message"],usage=None)
        return NS(output=[NS(type="message")],output_text="Leave the uncovered slot for coordinator review.",usage=None)


def case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,model=None):
    volunteer=make_volunteer("Clyde Example")
    starts=(clock.now()+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0)
    row=assign(volunteer,make_shift("greeter",starts=starts,minutes=60))
    session.info[confirmations.MODE_KEY]=True
    session.info["confirmation_now"]=clock.now()
    return row,FillContext(session,clock,provider,model or ExactGloo(),log_dir=tmp_path)


def reminder_review(session,ctx):
    reminders.process(ctx)
    return session.scalar(select(m.Approval).where(m.Approval.kind=="confirm_text",
        m.Approval.status=="pending",m.Approval.payload["purpose"].as_string()=="reminder"))


def test_literal_saved_greeting_is_gloo_composed_reviewed_and_deduplicated(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    assert reminders.day_before_copy(row,clock.now().tzinfo)==LITERAL
    review=reminder_review(session,ctx)
    assert review.payload["body"]==LITERAL and review.payload["kind"]=="ai" and not provider.sent
    assert review.payload["conversation"]["assignment_id"]==row.id
    assert review.payload["conversation"]["notice"]=="day_before"
    assert review.payload["conversation"]["source"]==reminders.assignment_source(row,"reminder")
    assert len(review.payload["conversation"]["keys"])==1
    assert review.payload["content_hash"]==confirmations.digest(review.payload)
    calls=ctx.gloo.calls;reminders.process(ctx);assert ctx.gloo.calls==calls
    reviewed(session,ctx,review)
    assert len(provider.sent)==1 and provider.sent[0].body==LITERAL and row.status=="approved"
    reminders.process(ctx);assert ctx.gloo.calls==calls and len(provider.sent)==1


def test_scheduled_notice_is_assignment_bound_without_a_confirmation_loop(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    def postpone():
        row.shift.event.starts_at+=timedelta(days=1)
        row.shift.event.ends_at+=timedelta(days=1)
    human_change(session,postpone);reminders.process(ctx)
    review=session.scalar(select(m.Approval).where(m.Approval.payload["purpose"].as_string()=="confirmation"))
    assert review.payload["conversation"]["assignment_id"]==row.id
    assert review.payload["conversation"]["notice"]=="scheduled"
    assert review.payload["conversation"]["source"]==reminders.assignment_source(row,"confirmation")
    assert review.payload["content_hash"]==confirmations.digest(review.payload)
    assert "You're scheduled for greeter" in review.payload["body"]
    assert all(fragment not in review.payload["body"] for fragment in ("Reply C","Reply YES","X if"))
    assert not provider.sent and row.status=="approved"
    calls=ctx.gloo.calls;reminders.process(ctx);assert ctx.gloo.calls==calls


def test_policy_suppression_is_terminal_without_repeated_gate_or_composition(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,monkeypatch):
    from app.core import outbound_conversation
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    meta,error=outbound_conversation.metadata(session,purpose="reminder",volunteer=row.volunteer,
        phone=row.volunteer.phone,now=clock.now(),supplied={"assignment_id":row.id,"notice":"day_before"})
    assert error is None
    session.add(m.Notification(key=meta["keys"][0],purpose="conversation_delivery",body="",state="reserved",
        due_at=clock.now(),created_at=clock.now(),detail=meta));session.flush()
    attempts=[];gate_type=type(ctx.gate);send=gate_type.send
    def recorded_send(gate,**kwargs):
        attempts.append(kwargs);return send(gate,**kwargs)
    monkeypatch.setattr(gate_type,"send",recorded_send)
    assert reminder_review(session,ctx) is None
    assert session.get(m.Policy,f"job:reminder:{row.id}").value["state"]=="blocked_policy"
    reminders.process(ctx)
    assert len(attempts)==0 and ctx.gloo.calls==0 and not provider.sent


@pytest.mark.parametrize("transform",[
    lambda text:text+" Reply YES.",lambda text:text+" STOP",lambda text:text+" HELP",
    lambda text:text+" Reply C to confirm.",lambda text:text+" 🐒",lambda text:text+"\n",
    lambda text:'"'+text+'"',lambda text:text.replace("10am","11am"),
    lambda text:text.replace("just let me know","please tell me"),lambda text:text.replace("You're","You are"),
])
def test_any_model_paraphrase_footer_or_decoration_is_held(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,transform):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,ExactGloo(transform))
    assert reminder_review(session,ctx) is None
    assert not provider.sent and row.status=="approved"
    receipt=session.get(m.Policy,f"job:reminder:{row.id}")
    assert receipt.value["state"]=="gloo_unavailable" and not receipt.value.get("body")


def test_model_outage_never_falls_back_to_literal_seed(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,NullGloo())
    assert reminder_review(session,ctx) is None and not provider.sent and row.status=="approved"


def test_forbidden_punctuation_in_saved_role_requires_correction_without_rewriting(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    human_change(session,lambda:setattr(row.shift.role,"name","sound\u2014team"))
    assert reminder_review(session,ctx) is None and not provider.sent and ctx.gloo.calls==0
    assert row.shift.role.name=="sound\u2014team" and row.status=="approved"


def test_missing_model_never_queues_seed_or_crashes_tick(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    ctx.gloo=None
    assert reminder_review(session,ctx) is None and not provider.sent and row.status=="approved"


@pytest.mark.parametrize("hour,minute,formatted",[(0,0,"12am"),(12,0,"12pm"),(10,15,"10:15am"),(15,30,"3:30pm")])
def test_actual_saved_time_is_the_only_time_substitution(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,hour,minute,formatted):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    def update():
        row.shift.event.starts_at=row.shift.event.starts_at.astimezone(clock.now().tzinfo).replace(hour=hour,minute=minute)
        row.shift.event.ends_at=row.shift.event.starts_at+timedelta(hours=1)
    human_change(session,update)
    expected=LITERAL.replace("10am",formatted)
    assert reminders.day_before_copy(row,clock.now().tzinfo)==expected


def test_saved_role_and_recipient_substitute_without_a_confirmation_request(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    def update(): row.volunteer.name="Jordan Example";row.shift.role.name="sound"
    human_change(session,update)
    assert reminders.day_before_copy(row,clock.now().tzinfo)==LITERAL.replace("Clyde","Jordan").replace("greet","serve in the sound role")


def test_timezone_change_blocks_old_literal_at_review_and_native_preflight(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    review=reminder_review(session,ctx);reviewed(session,ctx,review)
    message=session.get(m.Message,review.payload["message_id"])
    session.add(m.Policy(key="church_timezone",value={"value":"America/New_York"}));session.flush()
    assert "local time changed" in confirmations.delivery_problem(session,provider,review,clock.now(),message)
    assert len(provider.sent)==1  # Preflight check is read-only, never retries delivery.


def test_legacy_nonliteral_pending_receipt_cannot_dispatch(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    # An old copy approval may still be visible during deployment; it cannot send.
    body="Old reminder. Reply C to confirm."
    assert not reminders.once(ctx,f"reminder:{row.id}",row.volunteer,body,"reminder",
        source=reminders.assignment_source(row,"reminder"))
    old=session.scalar(select(m.Approval).where(m.Approval.payload["purpose"].as_string()=="reminder"))
    reviewed(session,ctx,old)
    assert old.status=="expired" and not provider.sent
    fresh=reminder_review(session,ctx)
    assert fresh.id!=old.id and fresh.payload["body"]==LITERAL


def test_silence_keeps_assignment_and_explicit_cancellation_routes_replacement(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    review=reminder_review(session,ctx);reviewed(session,ctx,review)
    clock.advance(timedelta(hours=8));reminders.process(ctx)
    assert row.status=="approved" and len(provider.sent)==1
    parsed=lambda text:ParsedMessage(intent="cancel",confidence=0.99,shift_hint="tomorrow")
    result=handle_inbound(session,clock,provider,row.volunteer.phone,"I can't make it tomorrow",parsed,ctx=ctx)
    assert result.routed_to=="fill_agent" and row.status=="cancelled"
    fill=session.scalar(select(m.FillRequest).where(m.FillRequest.shift_id==row.shift_id))
    assert fill is not None and not any(a.status in ("approved","confirmed") for a in row.shift.assignments)


def test_late_day_before_assignment_is_picked_by_existing_jobs_without_duplicate_confirmation(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,monkeypatch):
    from app import jobs
    clock.set_time(clock.now().replace(day=3,hour=18))
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    monkeypatch.setattr(jobs,"process_due_fill_requests",lambda current:[])
    result=jobs.process_jobs(ctx)
    review=session.scalar(select(m.Approval).where(m.Approval.kind=="confirm_text"))
    assert result["messages"]=={"reminders":0,"confirmations":0,"summaries":0}
    assert review.payload["body"]==LITERAL and review.payload["purpose"]=="reminder"
    assert row.shift.event.starts_at.astimezone(clock.now().tzinfo).strftime("%Y-%m-%d %H:%M")=="2026-10-04 10:00"
    assert len(session.scalars(select(m.Approval).where(m.Approval.kind=="confirm_text")).all())==1
    jobs.process_jobs(ctx)
    assert ctx.gloo.calls==1 and row.status=="approved" and not provider.sent


def test_equivalent_local_and_utc_source_does_not_block_freshly_created_assignment(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    event=row.shift.event
    # Keep the saved local-time ORM object alive, like the one-shot setup path.
    human_change(session,lambda:setattr(event,"starts_at",event.starts_at.astimezone(clock.now().tzinfo)))
    source=reminders.assignment_source(row,"reminder")
    assert source["starts_at"].endswith("+00:00")
    assert reminders.source_problem(session,row.volunteer,source,clock.now()) is None
    assert reminder_review(session,ctx).payload["body"]==LITERAL and not provider.sent


def test_day_before_supersedes_a_still_valid_earlier_confirmation_review(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    clock.set_time(clock.now().replace(hour=23,minute=30))
    row,ctx=case(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    session.add(m.Policy(key="quiet_hours",value={"value":{"start":"02:00","end":"03:00"}}))
    def postpone():
        row.shift.event.starts_at+=timedelta(days=1)
        row.shift.event.ends_at+=timedelta(days=1)
    human_change(session,postpone);reminders.process(ctx)
    old=session.scalar(select(m.Approval).where(m.Approval.payload["purpose"].as_string()=="confirmation"))
    clock.advance(timedelta(hours=1))
    assert confirmations.valid(old,clock.now())
    # Even before a job tick, approving the now-superseded C/X request cannot send.
    reviewed(session,ctx,old);assert old.status=="expired" and not provider.sent
    literal=reminder_review(session,ctx)
    assert literal.payload["body"]==LITERAL and literal.id!=old.id
