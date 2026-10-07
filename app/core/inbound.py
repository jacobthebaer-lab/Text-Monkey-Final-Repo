"""Inbound message routing (PLAN.md section 8).

The Twilio webhook and the phone simulator both call handle_inbound(). The
parser is injected as a callable so tests and the simulator can run without
Gloo; production passes functools.partial(parse_inbound, gloo_client).

Pass a fill_agent.FillContext as `ctx` to dispatch cancellations and outreach
replies to the real fill agent (Phase 4). Without it the router returns
routed_to markers only, which keeps it testable without Gloo. Planning and
admin agent hand-offs land in later phases. Everything deterministic —
logging, escalation, approval resolution, confirmations, outreach response
matching — happens here in code.
"""

from dataclasses import dataclass, field
from datetime import timedelta
import re

from sqlalchemy import select

from app.clock import Clock
from app.core import templates, offer_windows as offers
from app.core.policies import PolicyStore
from app.core.send_gate import SendGate, handle_stop_start
from app.db import models as m
from app.llm.parser import ParsedMessage
from app.sms.provider import SMSProvider
from app.sms.transport import transport_name
from app.core.conversation import scope

APPROVAL_YES = {"YES", "Y", "APPROVE", "OK"}
APPROVAL_NO = {"NO", "N", "REJECT"}
CONFIDENCE_FLOOR = 0.7
CLARIFY_WINDOW_HOURS = 24


@dataclass
class InboundResult:
    routed_to: str
    parsed: ParsedMessage | None = None
    escalation_id: int | None = None
    approval_id: int | None = None
    notes: list[str] = field(default_factory=list)


def _schedule_instruction(body):
    """Conservative human instruction evidence; mentioning an action isn't permission."""
    text = body.strip().lower().replace("’", "'")
    if re.search(r"\b(?:do not|don't|never)\s+(?:confirm|accept|cancel)|\b(?:maybe|might|not sure|unsure)\b", text):
        return None
    explicit_absence = bool(re.match(r"^i (?:can't|cannot|won't|will not|am unable to) (?:make|come|attend|serve|help|cover)\b", text)
        and re.search(r"(?:[,;.]|\bbut\b)\s*(?:but\s+)?(?:can|could|would|show|what|which|are|how)\b", text))
    if ("?" in text and not explicit_absence and not re.match(r"^(?:please\s+)?(?:can|could) you cancel\b", text)) or re.search(r"\b(?:what if|if i|whether|would i)\b", text):
        return None
    if re.fullmatch(r"(?:yes|y|accept)(?:\s+r?\d+)?[!.]*", text):
        return "accept"
    if re.match(r"^(?:please\s+)?(?:can you\s+|could you\s+)?cancel\b", text) or re.match(r"^(?:can't|cannot|won't|unable to) (?:make|come|attend|serve|help|cover)\b", text) or re.search(r"\b(?:i\s+(?:can't|cannot|won't|will not|am unable to|am unavailable|am not available)|i (?:don't|do not) think i can)\s+(?:make|come|attend|serve|help|cover)\b", text) or re.search(r"\bi (?:need|have|want) to cancel\b", text):
        return "cancel"
    if '?' in text or re.search(r"\b(?:not|can't|cannot|don't|but|only|until)\b", text):
        return None
    if re.match(r"^(?:please\s+)?confirm(?:\s+(?:my|the|this|that)\s+(?:shift|booking|assignment))?\b", text) or re.match(r"^i (?:can|will|'ll) (?:cover|fill|help|take|serve)\b", text):
        return "accept"
    return None


def handle_inbound(session, clock, provider, phone, body, parser, ctx=None, allow_signup=False, *, existing_message=None):
    from app.core.confirmations import enabled
    keys = ("sender_phone", "sender_schedule_instruction", "confirmation_now", "record_authorized", "sender_record_permissions", "sender_profile_instruction", "conversation_origin", "sender_assignment_permissions", "sender_schedule_action")
    prior = {k: session.info.get(k) for k in keys}
    # Sender evidence exists in both review modes. It does not grant record
    # permissions; exact-mode permissions remain scoped below.
    action = _schedule_instruction(body)
    session.info.update(sender_phone=phone, sender_schedule_instruction=action is not None,
                        sender_schedule_action=action)
    if enabled(session):
        session.info.update(sender_record_permissions={}, sender_assignment_permissions=set(), sender_profile_instruction=False, confirmation_now=clock.now(), record_authorized=False)
    session.info["conversation_origin"] = transport_name(provider) if session.info.get("mac_test_session") else "mock_or_twilio"
    try:
        return _handle_inbound(session, clock, provider, phone, body, parser, ctx, allow_signup, existing_message=existing_message)
    finally:
        # Flush while the direct sender authorization is still in scope.
        try:
            session.flush()
        finally:
            for key, value in prior.items():
                if value is None:
                    session.info.pop(key, None)
                else:
                    session.info[key] = value


def _handle_inbound(
    session,
    clock: Clock,
    provider: SMSProvider,
    phone: str,
    body: str,
    parser,
    ctx=None,
    allow_signup: bool = False,
    *, existing_message=None,
) -> InboundResult:
    now = clock.now()
    gate = SendGate(session, clock, provider)
    policies = PolicyStore(session)
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
    parsed = None

    test_session = session.info.get("mac_test_session")
    if existing_message is not None:
        transport = transport_name(provider)
        valid_resume = transport == 'google_voice'
        if transport == 'mac_messages':
            from types import SimpleNamespace
            from app.integrations.mac_progress import _source
            job = session.get(m.Notification, session.info.get('mac_progress_resume')) if session.info.get('mac_progress_resume') else None
            if job and job.detail.get('workflow') == 'schedule' and job.state in {'ready', 'extracting'}:
                _, original, _, error = _source(session, SimpleNamespace(provider=provider, mac_delivery_clock=clock), job, require_ack=True)
                valid_resume = error is None and original is existing_message
        # Stored Mac input is reusable only through its original durable job
        # after native acknowledgment, with unchanged sender and booking scope.
        kind = 'mac_test_in' if transport == 'mac_messages' else 'google_voice_test_in'
        if (not valid_resume or not test_session or
                session.get(m.Message, existing_message.id) is not existing_message or
                existing_message.direction != "in" or existing_message.phone != phone or
                existing_message.body != body or existing_message.status != "received" or
                existing_message.kind != kind or
                existing_message.purpose != "test:" + test_session.id or
                not test_session.starts_at <= existing_message.created_at <= now):
            raise ValueError("Stored incoming message could not be verified")
        incoming_message = existing_message
    else:
        incoming_message = m.Message(
            direction="in",
            volunteer_id=volunteer.id if volunteer else None,
            phone=phone,
            body=body,
            kind=("google_voice_test_in" if transport_name(provider) == "google_voice" else "mac_test_in") if test_session else "inbound",
            purpose="test:"+test_session.id if test_session else None,
            status="received",
            created_at=now,
        )
        session.add(incoming_message)
    session.flush()
    gate.reply_to_message_id = incoming_message.id
    if ctx is not None:
        ctx.reply_to_message_id = incoming_message.id
        gate.gloo = ctx.gloo

    from app.core.consent_controls import control_action
    control = control_action(body)
    # 1. Unknown numbers get one polite template and nothing else.
    if volunteer is None:
        if control == 'stop':
            key = "sms_opt_out:" + phone
            suppression = session.get(m.Policy, key)
            if suppression is None:
                session.add(m.Policy(key=key, value={"value": True}))
            else:
                suppression.value = {**suppression.value, 'value': True}
            from app.core.confirmations import suppress_phone
            suppress_phone(session, phone)
            return InboundResult(routed_to="stop")
        if allow_signup and ctx is not None:
            from app.core.signup import request_signup

            optout = session.get(m.Policy, "sms_opt_out:" + phone)
            if optout:
                if control == 'start':
                    session.delete(optout)
                    session.flush()
                else:
                    return InboundResult(routed_to="stop")
            signup = request_signup(session, clock, ctx.gloo, phone, body, gate=gate)
            if signup:
                return InboundResult(routed_to=signup)
            from app.core.signup_copy import compose_welcome
            from app.core.signup_delivery import intake_context, send_intake
            send_intake(session, clock, gate,
                compose=lambda: compose_welcome(session, clock, ctx.gloo, phone),
                purpose="signup_reply",
                phone=phone,
                conversation=intake_context(session,phone,'name',['name']),
            )
            return InboundResult(routed_to="signup_invitation")
        gate.send(
            body=templates.unknown_number(policies.church_name()),
            purpose="unknown_number",
            phone=phone,
        )
        return InboundResult(routed_to="unknown_number")

    # A registered cloud demo participant's actual name reply, including one
    # whose profile was imported earlier, owns its scoped pending signup.
    if test_session and test_session.outbound_prefix.startswith("GV") and control not in {"stop", "start"}:
        from app.integrations.google_voice_demo import RECIPIENT_KEY
        registration = session.get(m.Policy, RECIPIENT_KEY + phone)
        if registration and registration.value.get("consent_state") == "awaiting_name" and ctx is not None:
            from app.core.signup import request_signup
            return InboundResult(routed_to=request_signup(session, clock, ctx.gloo, phone, body, gate=gate) or "signup_identity_review")
    # 2. Opt-out keywords beat everything.
    if volunteer.preferences.get("consent_pending") and control != 'stop':
        from app.core.signup import finish_signup

        return InboundResult(
            routed_to=finish_signup(session, clock, gate, volunteer, body, gloo=ctx.gloo if ctx else None)
        )
    if control == 'stop':
        for o in session.scalars(select(m.Outreach).where(m.Outreach.volunteer_id == volunteer.id,
                m.Outreach.response.in_(offers.OPEN_RESPONSES))).all():
            offers.lock(session, o)
    keyword = handle_stop_start(session, clock, provider, volunteer, body)
    if keyword:
        if keyword == "stop":
            for o in session.scalars(select(m.Outreach).where(m.Outreach.volunteer_id == volunteer.id,
                    m.Outreach.response.in_(offers.OPEN_RESPONSES))).all():
                offers.close(session, o, "blocked", now)
                fill = session.get(m.FillRequest, o.fill_request_id)
                if fill.state in offers.OPEN_FILLS:
                    fill.next_action_at = now
        return InboundResult(routed_to=keyword)

    from app.core.cancellation_scope import route as cancellation_route
    held=cancellation_route(session,clock,gate,volunteer,incoming_message,parser,ctx,
        instruction=_schedule_instruction(body)=='cancel')
    if held is not None:
        routed,notes,classified,escalation_id=held
        return InboundResult(routed_to=routed,notes=notes,parsed=classified,escalation_id=escalation_id)

    # A clear schedule question is answered from records even during setup.
    # Care keywords retain their escalation route; SendGate still owns holds.
    from app.core import booking_status
    from app.llm.parser import keyword_sensitive
    if volunteer.sms_opt_in and not keyword_sensitive(body) and booking_status.requested(session, volunteer, body, now):
        booking_status.reply(session, clock, gate, volunteer, ctx.gloo if ctx else None)
        return InboundResult(routed_to="booking_status")

    if body.strip().upper() == "HELP":
        from app.core.signup_responder import compose_signup_reply
        gate.send(body=compose_signup_reply(session, clock, ctx.gloo if ctx else None,
            "Text Monkey helps you volunteer by text. Text a cancellation if plans change. Contact your ministry coordinator for help.", volunteer=volunteer),
            purpose="signup_reply", volunteer=volunteer)
        return InboundResult(routed_to="help")
    if ctx is not None and volunteer.sms_opt_in and body.strip().upper() in {"SETUP", "PROFILE"}:
        from app.core.onboarding import start
        start(session, clock, gate, volunteer, ctx.gloo)
        return InboundResult(routed_to="onboarding_interests")
    # A reply to an outstanding which-shift question is scheduling input,
    # even if preference setup was already in progress.
    if ctx is not None and body.strip().isdigit() and len(body.strip()) <= 2:
        recent = session.scalar(
            scope(select(m.Message), session.info.get("mac_test_session")).where(
                m.Message.volunteer_id == volunteer.id,
                m.Message.direction == "out",
                m.Message.purpose == "clarify_shift",
                m.Message.status.in_(("sent", "submitted", "uncertain")),
                m.Message.created_at >= now - timedelta(hours=CLARIFY_WINDOW_HOURS),
            )
        )
        if recent is not None:
            from app.agents import fill_agent
            session.info["sender_schedule_instruction"] = True
            session.info["sender_schedule_action"] = "cancel"
            outcome = fill_agent.handle_shift_choice(ctx, volunteer, int(body.strip()))
            return InboundResult(routed_to="fill_agent", notes=[outcome.action])
    if ctx is not None and volunteer.sms_opt_in:
        from app.core.onboarding import handle
        # Restarting preferences must not intercept an existing booking's
        # cancellation. Classify only when unfinished setup overlaps a booking.
        setup_stage = volunteer.preferences.get("onboarding_stage")
        booked = session.scalar(select(m.Assignment.id).join(m.Shift).join(m.Event).where(
            m.Assignment.volunteer_id == volunteer.id,
            m.Assignment.status.in_(("proposed", "approved", "confirmed")),
            m.Shift.starts_at > now,
        ).limit(1)) if setup_stage in {"welcome_name", "interests", "availability"} else None
        if booked is not None:
            parsed = parser(body)
        cancellation = parsed is not None and parsed.intent == "cancel" and parsed.confidence >= CONFIDENCE_FLOOR
        if not cancellation and not (parsed is not None and parsed.parse_error):
            onboarding = handle(session, clock, gate, volunteer, body, ctx.gloo)
            if onboarding:
                return InboundResult(routed_to=onboarding)

    # 3. The coordinator: approval replies first, everything else to the admin agent.
    if volunteer.is_coordinator:
        return _handle_coordinator(session, gate, volunteer, body, now, ctx)

    # An either/or cancellation question is not an invitation to accept.
    # Persist this intent across requests, even while its exact text is held.
    commitment_scope = _commitment_scope(session, volunteer)
    if commitment_scope and re.fullmatch(r"(?:YES|Y|NO|N)[!.]*", body.strip(), re.I):
        return InboundResult(routed_to="clarify_commitment", notes=["Please specify the invitation or booked role and day."])

    # An explicit invitation RSVP must not depend on an AI guessing "confirm".
    if ctx is not None and re.fullmatch(r"(?:YES|Y|NO|N)(?:\s+R?\d+)?[!.]*", body.strip(), re.I):
        intent = "accept" if body.strip().upper().startswith("Y") else "decline"
        code = re.search(r"\d+", body)
        matches = _outreach_matches(session, volunteer, now, int(code.group()) if code else None)
        active = [o for o in matches if _reply_open(session, o, now)]
        if not code and _ambiguous_offer_reply(session, volunteer, matches, active, now):
            _clarify_offer(session, gate, volunteer, active, now)
            return InboundResult(routed_to="clarify_offer")
        outreach = matches[0] if code and matches else (active[0] if active else (matches[0] if matches else None))
        if outreach:
            from app.agents import fill_agent
            outcome = fill_agent.on_outreach_reply(ctx, volunteer, outreach, intent)
            return InboundResult(routed_to="fill_agent", notes=[outcome.action])
        if code:
            gate.send(body="That offer code doesn't match a text sent to you. Please use the code in your invitation.", purpose="clarify", volunteer=volunteer)
            return InboundResult(routed_to="unmatched_reply")

    # 4. Classify. The parser applies the keyword backstop itself.
    parsed = parsed if parsed is not None else parser(body)
    result = InboundResult(routed_to="", parsed=parsed)

    # 5. Sensitive check on every volunteer message. Escalate to the pastor,
    #    block automated replies (the escalation row makes the send gate refuse),
    #    and keep routing the logistics without messaging them.
    if parsed.sensitive:
        result.escalation_id = _escalate_sensitive(
            session, gate, volunteer, body, parsed, now
        )

    from app.core.confirmations import enabled
    if enabled(session) and parsed.intent in {"accept", "confirm", "cancel"} and session.info.get("sender_schedule_action") != ("cancel" if parsed.intent == "cancel" else "accept"):
        review_id = _escalate(session, 'unclear', 'normal', f'Scheduling instruction needs human clarification: {body!r}', volunteer, now)
        from app.core.ordinary_reply import reply
        reply(session, clock, gate, volunteer, review_escalation_id=review_id)
        return InboundResult(routed_to="human_review", notes=["Scheduling interpretation needs human clarification; records were not changed."])
    # 6. Route by intent.
    if parsed.parse_error:
        result.routed_to = "escalated_unclear"
        result.escalation_id = result.escalation_id or _escalate(
            session,
            "system_error",
            "normal",
            f"Could not classify message from {volunteer.name}: {body!r}",
            volunteer,
            now,
        )
        from app.core.ordinary_reply import reply
        reply(session, clock, gate, volunteer, review_escalation_id=result.escalation_id)
        return result

    intent = parsed.intent
    if intent != "confirm" and parsed.confidence < CONFIDENCE_FLOOR:
        intent = "unclear"

    if commitment_scope and intent in ("accept", "confirm", "decline", "partial", "cancel") and not parsed.shift_hint:
        # Explicitly declining the invitation resolves the stored question's
        # invitation branch. A general yes/no or cancel remains ambiguous.
        if ctx and intent in ("decline", "cancel") and re.search(r"\b(?:invitation|invite|offer)\b", body, re.I):
            choices = [o for o in _outreach_matches(session, volunteer, now)
                       if o.id in commitment_scope.detail["outreach_ids"] and _reply_open(session, o, now)]
            if len(choices) == 1:
                from app.agents import fill_agent
                outcome = fill_agent.on_outreach_reply(ctx, volunteer, choices[0], "decline")
                commitment_scope.state = "scope_resolved"
                return InboundResult(routed_to="fill_agent", notes=[outcome.action])
        return InboundResult(routed_to="clarify_commitment", notes=["Please specify the invitation or booked role and day."])

    if intent == "cancel" and ctx is not None:
        matches = _outreach_matches(session, volunteer, now)
        active = [o for o in matches if _reply_open(session, o, now)]
        if active:
            bookings = session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
                m.Assignment.volunteer_id == volunteer.id, m.Assignment.status.in_(("proposed", "approved", "confirmed")),
                m.Shift.starts_at > now)).all()
            offer_choices = _offers_for_hint(session, active, parsed.shift_hint) if parsed.shift_hint else active
            booking_choices = [a for a in bookings if parsed.shift_hint and _hint_matches_shift(
                session, session.get(m.Shift, a.shift_id), parsed.shift_hint)]
            clear_cancellation = len(booking_choices) == 1 and not offer_choices
            if bookings and not clear_cancellation and (offer_choices or not booking_choices):
                held = gate.send(body="Do you mean declining the new invitation, or cancelling a shift you already agreed to serve? Please tell me the role and day.",
                    purpose="clarify", volunteer=volunteer)
                _save_reply_scope(session, volunteer, now, held, state="commitment_scope",
                    detail={"kind":"commitment", "outreach_ids":[o.id for o in active], "assignment_ids":[a.id for a in bookings]},
                    expires_at=min(offers.metadata(session,o).expires_at for o in active))
                return InboundResult(routed_to="clarify_commitment")
            if not clear_cancellation and len(offer_choices) == 1:
                relevant = _offers_for_hint(session, matches, parsed.shift_hint) if parsed.shift_hint else matches
                if _ambiguous_offer_reply(session, volunteer, relevant, offer_choices, now):
                    _clarify_offer(session, gate, volunteer, offer_choices, now)
                    return InboundResult(routed_to="clarify_offer")
                from app.agents import fill_agent
                outcome = fill_agent.on_outreach_reply(ctx, volunteer, offer_choices[0], "decline")
                if commitment_scope:
                    commitment_scope.state = "scope_resolved"
                return InboundResult(routed_to="fill_agent", notes=[outcome.action])
    if intent == "cancel":
        if ctx is not None:
            from app.agents import fill_agent

            outcome = fill_agent.handle_cancellation(
                ctx, volunteer, shift_hint=parsed.shift_hint, sensitive=parsed.sensitive
            )
            result.notes.append(outcome.action)
            if commitment_scope and outcome.action not in ("clarify_shift", "no_upcoming_assignment", "invalid_choice"):
                commitment_scope.state = "scope_resolved"
        elif not parsed.sensitive:
            gate.send(
                body=templates.cancellation_ack(volunteer.name),
                purpose="cancellation_ack",
                volunteer=volunteer,
            )
        result.routed_to = "fill_agent"
    elif intent in ("accept", "decline", "partial"):
        matches = _outreach_matches(session, volunteer, now)
        if parsed.shift_hint:
            matches = _offers_for_hint(session, matches, parsed.shift_hint)
        active = [o for o in matches if _reply_open(session, o, now)]
        if _ambiguous_offer_reply(session, volunteer, matches, active, now, explicit_hint=bool(parsed.shift_hint)):
            _clarify_offer(session, gate, volunteer, active, now)
            return InboundResult(routed_to="clarify_offer")
        outreach = _record_outreach_response(session, volunteer, intent, now, record=ctx is None, shift_hint=parsed.shift_hint)
        result.notes.append(f"outreach_matched={outreach is not None}")
        if outreach is not None:
            if ctx is not None:
                from app.agents import fill_agent

                outcome = fill_agent.on_outreach_reply(ctx, volunteer, outreach, intent)
                result.notes.append(outcome.action)
                if commitment_scope and parsed.shift_hint:
                    commitment_scope.state = "scope_resolved"
            result.routed_to = "fill_agent"
        else:
            result.routed_to = _clarify_or_escalate(
                session, gate, volunteer, body, now, result
            )
    elif intent == "availability":
        if ctx is not None and not parsed.sensitive:
            from app.core.serving_requests import save_serving_request
            save_serving_request(session, clock, gate, ctx.gloo, volunteer, body, parsed, incoming_message.id)
            result.notes.append("availability_update_recorded")
        result.routed_to = "availability"
    elif intent == "confirm":
        offer_matches = _outreach_matches(session, volunteer, now)
        active_offers = [o for o in offer_matches if _reply_open(session, o, now)]
        if offer_matches and ctx is not None:
            outreach = _record_outreach_response(session, volunteer, "accept", now, record=ctx is None, shift_hint=parsed.shift_hint)
            if outreach:
                from app.agents import fill_agent
                outcome = fill_agent.on_outreach_reply(ctx, volunteer, outreach, "accept")
                result.notes.append(outcome.action)
                if commitment_scope and parsed.shift_hint:
                    commitment_scope.state = "scope_resolved"
                result.routed_to = "fill_agent"
            else:
                _clarify_offer(session, gate, volunteer, active_offers, now)
                result.routed_to = "clarify_offer"
            return result
        confirmed = _confirm_next_assignment(session, volunteer, now)
        result.notes.append(f"confirmed_assignment={confirmed}")
        result.routed_to = "confirmed" if confirmed else "unmatched_reply"
    else:  # question, other, unclear, low confidence
        result.routed_to = _clarify_or_escalate(
            session, gate, volunteer, body, now, result
        )

    return result


def _handle_coordinator(
    session, gate: SendGate, coordinator, body: str, now, ctx=None
) -> InboundResult:
    from app.llm.parser import keyword_sensitive
    if keyword_sensitive(body):
        from app.core.care import escalate_sensitive
        escalation_id = escalate_sensitive(session, gate, coordinator, body, now)
        return InboundResult(routed_to='escalated_sensitive', escalation_id=escalation_id)
    from app.core.confirmations import enabled
    if enabled(session):
        return InboundResult(routed_to="human_review", notes=["Review exact actions in the signed-in dashboard; SMS cannot approve them."])
    normalized = body.strip().upper().rstrip("!.")
    approval_code = re.fullmatch(r"(YES|Y|NO|N)\s+A(\d+)", normalized)
    if approval_code:
        selected = session.get(m.Approval, int(approval_code.group(2)))
        if selected is None or selected.status != "pending":
            return InboundResult(routed_to="admin_agent", notes=["approval no longer pending"])
        notes = decide_approval(session, gate, selected, approve=approval_code.group(1) in APPROVAL_YES,
                                decided_by=coordinator.name, via="sms", now=now, ctx=ctx)
        return InboundResult(routed_to="approval", approval_id=selected.id, notes=notes)
    if normalized in APPROVAL_YES | APPROVAL_NO:
        query = select(m.Approval).where(m.Approval.status == "pending")
        if hasattr(gate.provider, "allows"):
            query = query.where(
                m.Approval.payload["transport"].as_string() == transport_name(gate.provider)
            )
        pending = session.scalars(query.order_by(m.Approval.requested_at)).all()
        groups = {a.payload.get("fill_request_id", f"approval:{a.id}") for a in pending}
        if len(groups) > 1:
            gate.send(body="Several approvals are waiting. Reply YES A followed by the approval number, or review them in Text Monkey.",
                      purpose="admin_reply", volunteer=coordinator)
            return InboundResult(routed_to="clarify_approval")
        oldest = pending[0] if pending else None
        if oldest is None:
            return InboundResult(routed_to="admin_agent", notes=["no pending approval"])

        notes = decide_approval(
            session,
            gate,
            oldest,
            approve=normalized in APPROVAL_YES,
            decided_by=coordinator.name,
            via="sms",
            now=now,
            ctx=ctx,
        )
        return InboundResult(routed_to="approval", approval_id=oldest.id, notes=notes)
    if ctx is not None:
        from app.agents.admin_agent import prepare
        outcome = prepare(ctx, coordinator, body)
        return InboundResult(routed_to="admin_agent", notes=[str(outcome)])
    return InboundResult(routed_to="admin_agent")


def decide_approval(
    session,
    gate: SendGate,
    approval: m.Approval,
    *,
    approve: bool,
    decided_by: str,
    via: str,
    now,
    ctx=None,
) -> list[str]:
    """Resolve an approval — and its whole batch: one YES covers every pending
    approval for the same fill request (the coordinator was asked about them
    as a group). Used by both the SMS reply path and the approvals web page."""
    from app.core.confirmations import enabled
    if enabled(session):
        raise ValueError("Use exact-content review in the signed-in Text Monkey dashboard")
    fill_request_id = approval.payload.get("fill_request_id")
    fill = session.get(m.FillRequest, fill_request_id) if fill_request_id else None
    if fill:
        slot = session.get(m.Shift, fill.shift_id)
        session.scalar(select(m.Event).where(m.Event.id == slot.event_id).with_for_update())
        session.scalar(select(m.Shift).where(m.Shift.id == slot.id).with_for_update())
        fill = session.scalar(select(m.FillRequest).where(m.FillRequest.id == fill.id).with_for_update().execution_options(populate_existing=True))
    if fill_request_id is not None:
        batch = session.scalars(
            select(m.Approval).where(m.Approval.status == "pending",
                                    m.Approval.payload["fill_request_id"].as_integer() == fill_request_id)
            .with_for_update().execution_options(populate_existing=True)
        ).all()
        batch = [
            a for a in batch if a.payload.get("fill_request_id") == fill_request_id
        ]
        if hasattr(gate.provider, "allows"):
            batch = [a for a in batch if a.payload.get("transport") == transport_name(gate.provider)]
    else:
        batch = [approval]

    if fill and (fill.state != "waiting_approval" or session.get(m.Shift, fill.shift_id).starts_at <= now):
        for item in batch:
            item.status = "expired"
        return ["This replacement batch is no longer open; no asks were sent."]
    notes = []
    for item in sorted(batch, key=lambda a: a.payload.get("volunteer_id") or 0):
        item.decided_at = now
        item.decided_by = decided_by
        item.via = via
        if approve:
            item.status = "approved"
            if item.kind == "signup":
                from app.core.signup import approve_signup

                approve_signup(session, gate.clock, item)
                notes.append(
                    f"approved signup #{item.id}; consent and qualifications remain unverified"
                )
            elif item.kind in ("publish_schedule", "collect_availability", "admin_change"):
                if ctx is None:
                    raise ValueError("approval needs workflow context")
                if item.kind == "publish_schedule":
                    from app.agents.planning_agent import publish
                    outcome = publish(ctx, item)
                elif item.kind == "collect_availability":
                    from app.agents.planning_agent import collect
                    outcome = collect(ctx, item)
                else:
                    from app.agents.admin_agent import apply
                    outcome = apply(ctx, item)
                notes.append(str(outcome))
            elif item.kind == "send_outreach":
                outcome = gate.send_approved(item)
                if outcome.retry_at:
                    item.payload = {**item.payload, "retry_at": outcome.retry_at.isoformat()}
                notes.append(f"approved #{item.id}, send={outcome.status.value}")
            else:
                notes.append(f"approved #{item.id}")
        else:
            item.status = "rejected"
            notes.append(f"rejected #{item.id}")

    if ctx is not None and fill_request_id is not None:
        from app.agents import fill_agent
        from app.core.notifications import queue_staffing
        if approve:
            fill_agent.on_outreach_approved(ctx, fill_request_id)
        elif fill:
            fill.state = "escalated"
            fill.next_action_at = None
            session.add(m.Escalation(category="unfillable", severity="normal", summary="Coordinator declined the restricted-role replacement batch.",
                                    related_ids={"fill_request_id": fill.id}, status="open", created_at=now))
        if fill:
            queue_staffing(ctx, session.get(m.Shift, fill.shift_id).event)
    return notes


def _escalate(session, category: str, severity: str, summary: str, volunteer, now) -> int:
    pastor_or_coordinator = session.scalar(
        select(m.Volunteer).where(
            m.Volunteer.is_pastor if category == "sensitive" else m.Volunteer.is_coordinator
        )
    )
    escalation = m.Escalation(
        category=category,
        severity=severity,
        summary=summary,
        related_ids={"volunteer_id": volunteer.id},
        assigned_to=pastor_or_coordinator.id if pastor_or_coordinator else None,
        status="open",
        created_at=now,
    )
    session.add(escalation)
    session.flush()
    return escalation.id


def _escalate_sensitive(session, gate: SendGate, volunteer, body: str, parsed: ParsedMessage, now) -> int:
    from app.core.care import escalate_sensitive
    return escalate_sensitive(session, gate, volunteer, body, now, severity=parsed.severity)


def _outreach_matches(session, volunteer, now, outreach_id=None):
    query = (scope(select(m.Outreach).join(m.Message, m.Outreach.message_id == m.Message.id), session.info.get("mac_test_session"))
             .where(m.Outreach.volunteer_id == volunteer.id, m.Message.direction == "out",
                    m.Message.status.in_(("sent", "submitted", "uncertain", "dispatching"))))
    if outreach_id is not None:
        query = query.where(m.Outreach.id == outreach_id)
    return session.scalars(query.order_by(m.Outreach.id.desc())).all()


def _reply_open(session, outreach, now):
    # Legacy rows may still be matched for a safe closed-offer response, but
    # they never authorize an assignment without dispatch metadata.
    return offers.problem(session, outreach, now) is None


def _hint_matches_shift(session, shift, hint):
    local = shift.starts_at.astimezone(PolicyStore(session).church_tz())
    description = shift.role.name + " " + shift.event.title + " " + local.strftime("%A %a %B %b %-d %Y %-I:%M%p %Y-%m-%d")
    tokens = lambda text: set(re.findall(r"[a-z0-9]+", text.lower().replace(":00", "")))
    return bool(tokens(hint)) and tokens(hint) <= tokens(description)


def _offers_for_hint(session, rows, hint):
    return [o for o in rows if _hint_matches_shift(session,
        session.get(m.Shift, session.get(m.FillRequest, o.fill_request_id).shift_id), hint)]


def _reply_scope(session, volunteer):
    selected = session.info.get("mac_test_session")
    return f"offer-reply:{volunteer.id}:" + (selected.id if selected else "local")


def _save_reply_scope(session, volunteer, now, result, *, state, detail, expires_at):
    key = _reply_scope(session, volunteer)
    row = session.get(m.Notification, key)
    if row is None:
        row = m.Notification(key=key, volunteer_id=volunteer.id, purpose="offer_reply_scope",
                             body="", created_at=now, due_at=now, detail={})
        session.add(row)
    row.state, row.message_id, row.expires_at = state, result.message_id, expires_at
    row.detail = {**detail, "approval_id":result.approval_id}
    return row


def _commitment_scope(session, volunteer):
    row = session.get(m.Notification, _reply_scope(session, volunteer))
    return row if row and row.state == "commitment_scope" else None


def _scope_proof(session, pending):
    if not pending:
        return None
    message_id = pending.message_id
    if message_id is None and pending.detail.get("approval_id"):
        approval = session.get(m.Approval, pending.detail["approval_id"])
        message_id = approval.payload.get("message_id") if approval and approval.status == "approved" else None
    return session.get(m.Message, message_id) if message_id else None


def _ambiguous_offer_reply(session, volunteer, matches, active, now, *, explicit_hint=False):
    if len(active) > 1:
        return True
    if not active:
        return False
    # Keep prior-offer existence beyond any lookback or selected test-session
    # boundary. This reads only an application-owned ID, never old text bodies.
    prior = len(matches) > 1 or (not explicit_hint and session.scalar(
        select(m.Outreach.id).join(m.Message, m.Outreach.message_id == m.Message.id).where(
            m.Outreach.volunteer_id == volunteer.id, m.Outreach.id != active[0].id,
            m.Message.direction == "out", m.Message.purpose == "outreach",
            m.Message.status.in_(("sent", "submitted", "uncertain", "dispatching"))).limit(1)) is not None)
    if not prior:
        return False
    pending = session.get(m.Notification, _reply_scope(session, volunteer))
    proof = _scope_proof(session, pending)
    return not (pending and pending.state == "offer_scope" and pending.expires_at > now and
                proof and proof.phone == volunteer.phone and proof.status in ("sent", "submitted") and
                pending.detail.get("outreach_id") == active[0].id)


def _clarify_offer(session, gate, volunteer, active, now):
    descriptions = []
    for o in active[:3]:
        shift = session.get(m.Shift, session.get(m.FillRequest, o.fill_request_id).shift_id)
        when = shift.starts_at.astimezone(PolicyStore(session).church_tz()).strftime("%a %b %-d, %-I:%M%p %Z")
        descriptions.append(f"{shift.role.name} on {when}")
    if len(active) == 1:
        body = f"Do you mean the invitation to serve {descriptions[0]}? Please reply yes or no to this question."
    else:
        body = "Which invitation do you mean: " + "; ".join(descriptions) + "? Please tell me the role and day."
    result = gate.send(body=body, purpose="clarify", volunteer=volunteer)
    # Only a delivered clarifier can scope the next natural reply. Held or
    # uncertain clarifiers cannot authorize a new interpretation.
    if len(active) == 1:
        _save_reply_scope(session, volunteer, now, result, state="offer_scope",
            detail={"kind":"offer", "outreach_id":active[0].id}, expires_at=offers.metadata(session, active[0]).expires_at)


def _record_outreach_response(session, volunteer, intent: str, now, *, record=True, shift_hint=None) -> m.Outreach | None:
    matches = _outreach_matches(session, volunteer, now)
    if shift_hint:
        matches = _offers_for_hint(session, matches, shift_hint)
    active = [o for o in matches if _reply_open(session, o, now)]
    # Natural-language acceptance is also ambiguous across multiple invitations.
    if _ambiguous_offer_reply(session, volunteer, matches, active, now, explicit_hint=bool(shift_hint)):
        return None
    outreach = active[0] if active else (matches[0] if matches else None)
    if outreach and record:
        outreach.response = {"accept": "yes", "decline": "no", "partial": "partial"}[intent]
        outreach.responded_at = now
    return outreach


def _confirm_next_assignment(session, volunteer, now) -> bool:
    assignment = session.scalar(
        select(m.Assignment)
        .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
        .join(m.Event, m.Shift.event_id == m.Event.id)
        .where(
            m.Assignment.volunteer_id == volunteer.id,
            m.Assignment.status == "approved",
            m.Shift.starts_at > now,
        )
        .order_by(m.Shift.starts_at)
    )
    if assignment is None:
        return False
    from app.core.confirmations import authorize_sender_assignment
    authorize_sender_assignment(session, volunteer, assignment.shift_id, "confirmed")
    assignment.status = "confirmed"
    assignment.updated_at = now
    return True


def _clarify_or_escalate(session, gate: SendGate, volunteer, body: str, now, result: InboundResult) -> str:
    """Clarify through Gloo, acknowledging actual review after repeated ambiguity."""
    recent_clarify = session.scalar(
        scope(select(m.Message), session.info.get("mac_test_session"))
        .where(
            m.Message.volunteer_id == volunteer.id,
            m.Message.direction == "out",
            m.Message.purpose == "clarify",
            m.Message.created_at >= now - timedelta(hours=CLARIFY_WINDOW_HOURS),
        )
        .order_by(m.Message.id.desc())
    )
    recent_reply = session.scalar(scope(select(m.Notification.key).join(m.Message, m.Notification.message_id == m.Message.id),
        session.info.get('mac_test_session')).where(
        m.Notification.key.startswith('ordinary-reply:'), m.Notification.volunteer_id == volunteer.id,
        m.Notification.state == 'sent', m.Message.status.in_(('sent', 'submitted')),
        m.Notification.detail['conversation_meta']['binding']['thanks'].as_boolean().is_(False),
        m.Message.created_at >= now-timedelta(hours=CLARIFY_WINDOW_HOURS)))
    from app.core.ordinary_reply import reply
    if recent_clarify is not None or recent_reply is not None:
        result.escalation_id = result.escalation_id or _escalate(
            session, "unclear", "normal",
            f"Still unclear after a clarifying question. {volunteer.name} said: {body!r}",
            volunteer, now,
        )
        reply(session, gate.clock, gate, volunteer, review_escalation_id=result.escalation_id)
        return "escalated_unclear"
    reply(session, gate.clock, gate, volunteer)
    return "clarify"
