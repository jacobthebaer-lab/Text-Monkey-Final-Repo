"""Fill agent: cancellation -> filled shift (PLAN.md section 9). Demo-critical.

Gloo chooses whom to ask and writes the personal outreach. Code enforces
eligibility, batch limits, timing, affirmative acceptance and confirmations.

If Gloo is unavailable the fill escalates to the coordinator — never guesses.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.clock import Clock
from app.config import get_settings
from app.core import eligibility, templates
from app.core import notifications, offer_windows as offers
from app.core.policies import PolicyStore, in_quiet_hours, next_send_time
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.gloo_client import GlooClient
from app.llm.tools import (
    candidate_context,
    fill_agent_tools,
    replacement_pool,
    shift_context,
)
from app.sms.provider import SMSProvider

import json
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "fill_agent.md"


@dataclass
class FillContext:
    session: object
    clock: Clock
    provider: SMSProvider
    gloo: GlooClient
    log_dir: Path | None = None
    reply_to_message_id: int | None = None

    @property
    def gate(self) -> SendGate:
        gate = SendGate(self.session, self.clock, self.provider, self.reply_to_message_id)
        gate.gloo = self.gloo
        return gate


@dataclass
class FillOutcome:
    action: str
    fill_request_id: int | None = None
    notes: list[str] = field(default_factory=list)


# --- tranche policy (timing lives in code; PLAN.md section 9.4) --------------


@dataclass(frozen=True)
class TranchePlan:
    sizes: tuple  # per-tranche candidate counts; every batch is bounded
    waits: tuple  # timedelta per tranche
    escalate_margin: timedelta | None  # escalate this long before the shift


def tranche_plan(hours_until: float) -> TranchePlan:
    # One offer per vacancy. Legacy tiers no longer decide response timing.
    return TranchePlan((1,), (), None)


def next_wait_for(session, fill_request: m.FillRequest, now: datetime) -> timedelta:
    shift = session.get(m.Shift, fill_request.shift_id)
    deadline = offers.deadline_for(session, shift.event.starts_at, now)
    return max(timedelta(0), deadline - now) if deadline else timedelta(0)


def escalation_deadline(fill_request, event, session):
    return offers.cutoff(session, event.starts_at)


def compute_urgency(session, shift: m.Shift, now: datetime) -> str:
    """Code's default; the agent may adjust it with a stated reason."""
    context = shift_context(session, shift, now)
    hours_until = context["hours_until_start"]
    if shift.role.criticality == "critical" and context["others_still_assigned"] < context["minimum_needed"]:
        return "critical"
    if shift.role.criticality == "optional" and context["others_still_assigned"] >= context["minimum_needed"]:
        return "skip"
    if hours_until < 24:
        return "high"
    return "normal"


# --- entry points -------------------------------------------------------------


def handle_cancellation(
    ctx: FillContext, volunteer: m.Volunteer, *, shift_hint: str | None = None, sensitive: bool = False
) -> FillOutcome:
    """A cancel intent (or admin marking someone out) starts here."""
    upcoming = _upcoming_assignments(ctx, volunteer)
    if not upcoming:
        if not sensitive:
            ctx.gate.send(body=templates.clarify_generic(volunteer.name), purpose="clarify", volunteer=volunteer)
        return FillOutcome("no_upcoming_assignment")

    assignment = _resolve_assignment(ctx, upcoming, shift_hint)
    if assignment is None:
        options = [_describe(ctx, a) for a in upcoming]
        ctx.gate.send(
            body=templates.clarify_which_shift(volunteer.name, options),
            purpose="clarify_shift",
            volunteer=volunteer,
        )
        return FillOutcome("clarify_shift", notes=[f"{len(upcoming)} upcoming assignments"])

    return _cancel_and_fill(ctx, volunteer, assignment, sensitive=sensitive)


def cancel_recorded_assignment(ctx, volunteer, assignment_id, *, sensitive=False, expected_scope=None):
    """Cancel a checked ID under the original booking fence, never a positional choice."""
    session=ctx.session
    assignment=session.get(m.Assignment,assignment_id)
    if assignment is None or assignment.volunteer_id!=volunteer.id:
        return FillOutcome('cancellation_scope_changed')
    shift_ids=sorted({row[1] for row in expected_scope} if expected_scope else {assignment.shift_id})
    shifts=list(session.scalars(select(m.Shift).where(m.Shift.id.in_(shift_ids)).order_by(m.Shift.id)))
    # Existing transition order: Event, Shift, person, Assignment. Lock all original
    # bookings and the person FK fence before refreshing the scoped decision.
    for event_id in sorted({shift.event_id for shift in shifts}):
        session.scalar(select(m.Event).where(m.Event.id==event_id).with_for_update().execution_options(populate_existing=True))
    for shift_id in shift_ids:
        session.scalar(select(m.Shift).where(m.Shift.id==shift_id).with_for_update().execution_options(populate_existing=True))
    session.scalar(select(m.Volunteer).where(m.Volunteer.id==volunteer.id).with_for_update().execution_options(populate_existing=True))
    for booking_id in sorted({row[0] for row in expected_scope} if expected_scope else {assignment_id}):
        session.scalar(select(m.Assignment).where(m.Assignment.id==booking_id).with_for_update().execution_options(populate_existing=True))
    if expected_scope is not None:
        from app.core.cancellation_scope import bookings, snapshot
        if snapshot(bookings(session,volunteer,ctx.clock.now()))!=expected_scope:
            return FillOutcome('cancellation_scope_changed')
    assignment=session.get(m.Assignment,assignment_id)
    if (assignment is None or assignment.volunteer_id!=volunteer.id or assignment.shift_id not in shift_ids or
            assignment.status not in eligibility.ACTIVE_ASSIGNMENT_STATUSES or
            assignment.shift.event.status!='scheduled' or assignment.shift.event.starts_at<=ctx.clock.now()):
        return FillOutcome('cancellation_scope_changed')
    return _cancel_and_fill(ctx,volunteer,assignment,sensitive=sensitive)


def handle_shift_choice(ctx: FillContext, volunteer: m.Volunteer, choice: int) -> FillOutcome:
    """Numbered reply after a clarify_shift question (1-based, same ordering)."""
    upcoming = _upcoming_assignments(ctx, volunteer)
    if not 1 <= choice <= len(upcoming):
        ctx.gate.send(body=templates.clarify_generic(volunteer.name), purpose="clarify", volunteer=volunteer)
        return FillOutcome("invalid_choice")
    return _cancel_and_fill(ctx, volunteer, upcoming[choice - 1], sensitive=False)


def on_outreach_reply(ctx: FillContext, volunteer: m.Volunteer, outreach: m.Outreach, intent: str) -> FillOutcome:
    """Resolve the scoped offer under locks using fresh decision time."""
    session = ctx.session
    # Serialize acceptances on the slot and person, then refresh the request.
    # The same order is used by timers/cancellations to avoid competing writes.
    outreach = offers.lock(session, outreach)
    fill_request = session.get(m.FillRequest, outreach.fill_request_id)
    shift = session.get(m.Shift, fill_request.shift_id)
    volunteer = session.get(m.Volunteer, volunteer.id)
    now = offers.decision_time(session, ctx.clock)
    if outreach.volunteer_id != volunteer.id:
        return FillOutcome("unmatched_reply", fill_request.id)
    if outreach.response == "yes":
        if intent != "accept":
            return FillOutcome("accepted_offer_requires_explicit_cancellation", fill_request.id)
        existing = session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
            m.Assignment.volunteer_id == volunteer.id, m.Assignment.status == "confirmed"))
        if existing:
            notifications.deliver(ctx, key=f"winner:{fill_request.id}:{volunteer.id}",
                body=templates.assignment_confirmation(volunteer.name, shift.role.name, _when(ctx, shift.event)),
                purpose="confirmation", volunteer=volunteer,
                conversation={'assignment_id': existing.id, 'notice': 'scheduled'})
            return FillOutcome("already_filled", fill_request.id)
    issue = offers.problem(session, outreach, now)
    if issue:
        if issue in ("offer deadline reached", "shift changed or closed"):
            offers.close(session, outreach, "expired" if issue == "offer deadline reached" else "revoked", now)
            if fill_request.state in offers.OPEN_FILLS:
                _advance(ctx, fill_request)
        return FillOutcome("offer_closed", fill_request.id, notes=[issue])
    if intent != "accept":
        outreach.response = {"decline": "no", "partial": "partial"}[intent]
    outreach.responded_at = now
    logger = RunLogger(session, ctx.clock, agent="fill_agent",
                       trigger=f"reply {intent} from {volunteer.name} (fill {fill_request.id})",
                       log_dir=ctx.log_dir)

    if intent == "accept":
        occupied = session.scalar(select(m.Assignment.id).where(m.Assignment.shift_id == shift.id,
            m.Assignment.status.in_(eligibility.ACTIVE_ASSIGNMENT_STATUSES)))
        if (fill_request.state not in offers.OPEN_FILLS or occupied or not volunteer.sms_opt_in
                or shift.event.starts_at <= now or shift.event.status in ("cancelled", "completed")):
            offers.close(session, outreach, "blocked", now)
            notifications.deliver(ctx, key=f"closed:{fill_request.id}:{volunteer.id}",
                body=f"Thanks, {volunteer.name.split()[0]}! That request is no longer open. You haven’t been added to this shift.",
                purpose="thanks", volunteer=volunteer)
            logger.close("request closed")
            return FillOutcome("request_closed", fill_request.id)
        check = eligibility.check(session, volunteer, shift, tz=_tz(ctx))
        from app.core.send_gate import has_open_sensitive_escalation
        if has_open_sensitive_escalation(session, volunteer.id):
            check.reasons.append("personal-care review is open")
            check.eligible = False
        from app.core.ranking import monthly_assignment_count
        from app.core.recurring_availability import global_frequency_limit
        maximum = global_frequency_limit(volunteer.preferences)
        if maximum is not None and monthly_assignment_count(session, volunteer.id, shift.event, ZoneInfo(_tz(ctx))) >= maximum:
            check.reasons.append("monthly serving limit reached")
            check.eligible = False
        if not check:
            logger.step("decision", result={"ineligible_yes": check.reasons})
            notifications.deliver(ctx, key=f"ineligible:{outreach.id}", body=templates.thanks_anyway(volunteer.name), purpose="thanks", volunteer=volunteer)
            outreach.response = "ineligible"
            logger.close("ineligible yes; thanked, still searching")
            _advance(ctx, fill_request)
            return FillOutcome("ineligible_yes", fill_request.id, notes=check.reasons)

        from app.core.confirmations import authorize_sender_assignment
        authorize_sender_assignment(session, volunteer, shift.id, "confirmed")
        outreach.response = "yes"
        assignment = m.Assignment(
            shift_id=shift.id, volunteer_id=volunteer.id, status="confirmed",
            source="fill", created_at=now, updated_at=now,
        )
        session.add(assignment)
        fill_request.state = "filled"
        fill_request.closed_at = now
        fill_request.next_action_at = None
        session.flush()
        logger.step("decision", result={"assigned": volunteer.id, "assignment_id": assignment.id})

        notifications.deliver(ctx, key=f"winner:{fill_request.id}:{volunteer.id}",
            body=templates.assignment_confirmation(volunteer.name, shift.role.name, _when(ctx, shift.event)),
            purpose="confirmation", volunteer=volunteer,
            conversation={'assignment_id': assignment.id, 'notice': 'scheduled'})
        _thank_the_rest(ctx, fill_request, winner_id=volunteer.id)
        for escalation in session.scalars(select(m.Escalation).where(m.Escalation.status == "open", m.Escalation.category == "unfillable")):
            if escalation.related_ids.get("fill_request_id") == fill_request.id:
                escalation.status = "resolved"
        notifications.queue_staffing(ctx, shift.event)
        logger.close("filled")
        return FillOutcome("filled", fill_request.id)

    if intent == "partial":
        # Safe default: thank them and keep looking for full coverage.
        notifications.deliver(ctx, key=f"partial:{outreach.id}", body=templates.partial_thanks(volunteer.name), purpose="thanks", volunteer=volunteer)
        logger.step("decision", result={"partial_offer": "thanked; continuing for full coverage"})

    if _all_current_outreach_answered(ctx, fill_request):
        logger.step("decision", result={"tranche_exhausted": "advancing early"})
        logger.close(f"{intent}; advancing early")
        return _advance(ctx, fill_request)

    logger.close(f"{intent} recorded")
    return FillOutcome(f"{intent}_recorded", fill_request.id)


def on_outreach_approved(ctx: FillContext, fill_request_id: int) -> None:
    """Attach approved delivery; retain its immutable dispatch timer."""
    session = ctx.session
    fill_request = session.get(m.FillRequest, fill_request_id)
    if fill_request and fill_request.state == "waiting_approval":
        _mark_sent_outreach(ctx, fill_request)
        rows = session.scalars(select(m.Outreach).where(
            m.Outreach.fill_request_id == fill_request.id, m.Outreach.tranche == fill_request.current_tranche)).all()
        if any(o.message_id is None and o.response == "none" for o in rows):
            return
        fill_request.state = "in_progress"
        unresolved = [o for o in rows if o.response in offers.OPEN_RESPONSES]
        rows = [offers.metadata(session, o) for o in unresolved]
        active = [r.expires_at for r in rows if r and r.state == "offer_active"]
        shift = session.get(m.Shift, fill_request.shift_id)
        fill_request.next_action_at = min(active) if active else (offers.cutoff(session, shift.event.starts_at) if unresolved else ctx.clock.now())



def advance_due(ctx: FillContext) -> list[FillOutcome]:
    """Timer tick: advance every fill request whose next_action_at has passed.

    Called by the scheduler job (app/jobs.py) and by demo fast-forward.
    """
    offers.begin_decision(ctx.session)
    now = ctx.clock.now()
    due = ctx.session.scalars(
        select(m.FillRequest).where(
            m.FillRequest.state.in_(("in_progress", "waiting_quiet", "waiting_approval"))
        )
    ).all()
    outcomes = []
    for fr in due:
        slot = ctx.session.get(m.Shift, fr.shift_id)
        ctx.session.scalar(select(m.Event).where(m.Event.id == slot.event_id).with_for_update().execution_options(populate_existing=True))
        ctx.session.scalar(select(m.Shift).where(m.Shift.id == fr.shift_id).with_for_update().execution_options(populate_existing=True))
        ctx.session.scalar(select(m.Role).where(m.Role.id == slot.role_id).with_for_update(read=True).execution_options(populate_existing=True))
        locked = ctx.session.scalar(select(m.FillRequest).where(m.FillRequest.id == fr.id).with_for_update().execution_options(populate_existing=True))
        now = offers.decision_time(ctx.session, ctx.clock)
        changed = False
        for o in ctx.session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == locked.id,
                m.Outreach.response.in_(offers.OPEN_RESPONSES))).all():
            meta = offers.metadata(ctx.session, o)
            if meta and meta.detail.get("snapshot") != offers.snapshot(slot):
                message = ctx.session.get(m.Message, o.message_id) if o.message_id else None
                if (meta.state == "offer_uncertain" or message and message.status in ("dispatching", "uncertain")):
                    changed = True
                    continue
                offers.close(ctx.session, o, "revoked", now)
                changed = True
        if changed:
            locked.state, locked.next_action_at = "in_progress", now
        if changed or (locked.next_action_at and locked.next_action_at <= now):
            outcomes.append(_advance(ctx, locked))
    return outcomes


# --- internals -----------------------------------------------------------------


def _cancel_and_fill(ctx: FillContext, volunteer, assignment: m.Assignment, *, sensitive: bool) -> FillOutcome:
    session, now = ctx.session, ctx.clock.now()
    shift = session.get(m.Shift, assignment.shift_id)
    logger = RunLogger(session, ctx.clock, agent="fill_agent",
                       trigger=f"cancellation by {volunteer.name} (assignment {assignment.id})",
                       model=get_settings().agent_model, log_dir=ctx.log_dir)

    session.scalar(select(m.Event).where(m.Event.id == shift.event_id).with_for_update())
    session.scalar(select(m.Shift).where(m.Shift.id == shift.id).with_for_update())
    session.refresh(assignment)
    if assignment.status == "cancelled":
        existing = session.scalar(select(m.FillRequest).where(m.FillRequest.cancelled_assignment_id == assignment.id))
        return FillOutcome("already_cancelled", existing.id if existing else None)
    from app.core.confirmations import authorize_sender_assignment
    authorize_sender_assignment(session, volunteer, assignment.shift_id, "cancelled")
    assignment.status = "cancelled"
    assignment.updated_at = now
    logger.step("decision", result={"cancelled_assignment": assignment.id, "sensitive": sensitive})

    if not sensitive:
        notifications.deliver(ctx, key=f"cancel:{assignment.id}", body=templates.cancellation_ack(volunteer.name), purpose="cancellation_ack", volunteer=volunteer)

    notifications.queue_staffing(ctx, shift.event)
    urgency = compute_urgency(session, shift, now)
    fill_request = m.FillRequest(
        shift_id=shift.id, cancelled_assignment_id=assignment.id,
        urgency=urgency, state="open", current_tranche=0, created_at=now,
    )
    session.add(fill_request)
    session.flush()
    logger.step("decision", result={"fill_request_id": fill_request.id, "default_urgency": urgency})

    if urgency == "skip":
        fill_request.state = "skipped"
        fill_request.closed_at = now
        notifications.queue_staffing(ctx, shift.event)
        logger.close("skipped (enough coverage)")
        return FillOutcome("skipped", fill_request.id)

    outcome = _open_tranche(ctx, fill_request, exclude_ids=(volunteer.id,), logger=logger)
    logger.close(outcome.action)
    return outcome


def _open_tranche(
    ctx: FillContext, fill_request: m.FillRequest, *, exclude_ids=(), logger: RunLogger
) -> FillOutcome:
    session = ctx.session
    now = offers.decision_time(session, ctx.clock)
    shift = session.get(m.Shift, fill_request.shift_id)
    plan = tranche_plan((shift.event.starts_at - now).total_seconds() / 3600)
    if shift.event.starts_at <= now or shift.event.status in ("cancelled", "completed"):
        return _escalate_unfilled(ctx, fill_request, logger, "service has started or closed")

    occupied = session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
        m.Assignment.status.in_(eligibility.ACTIVE_ASSIGNMENT_STATUSES)))
    if occupied:
        fill_request.state, fill_request.next_action_at, fill_request.closed_at = "filled", None, now
        _thank_the_rest(ctx, fill_request, occupied.volunteer_id)
        notifications.queue_staffing(ctx, shift.event)
        return FillOutcome("already_filled", fill_request.id)
    if offers.deadline_for(session, shift.event.starts_at, now) is None:
        return _escalate_unfilled(ctx, fill_request, logger, "too little time remains for an automatic offer")
    if offers.delivery_hold(session, shift_id=shift.id):
        return _escalate_unfilled(ctx, fill_request, logger, "delivery needs reconciliation before another offer")
    outstanding = session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fill_request.id,
        m.Outreach.response.in_(offers.OPEN_RESPONSES))).all()
    if outstanding:
        return FillOutcome("waiting_offer", fill_request.id)
    policies = PolicyStore(session)
    local_now = now.astimezone(policies.church_tz())
    start, end = policies.urgent_quiet_hours() if shift.event.starts_at-now < timedelta(hours=24) else policies.quiet_hours()
    if in_quiet_hours(local_now, start, end):
        fill_request.state = "waiting_quiet"
        fill_request.next_action_at = min(next_send_time(local_now, start, end), escalation_deadline(fill_request, shift.event, session))
        return FillOutcome("waiting_quiet", fill_request.id)

    already_asked = set(
        session.scalars(
            select(m.Outreach.volunteer_id).where(
                m.Outreach.fill_request_id == fill_request.id
            )
        )
    )
    cancelled_id = _cancelled_volunteer_id(session, fill_request)
    exclude = tuple(
        already_asked | set(exclude_ids) | ({cancelled_id} if cancelled_id else set())
    )
    candidates = [
        c
        for c in replacement_pool(session, fill_request, now, _tz(ctx))
        if c.volunteer.id not in exclude
    ]
    size = plan.sizes[min(fill_request.current_tranche, len(plan.sizes)-1)]
    if not candidates:
        return _escalate_unfilled(
            ctx, fill_request, logger, "no eligible candidates left"
        )

    fill_request.current_tranche += 1
    fill_request.state = "in_progress"
    session.flush()
    logger.step(
        "decision",
        result={
            "tranche": fill_request.current_tranche,
            "eligible_count": len(candidates),
            "selection": "gloo",
            "max_candidates": size,
        },
    )

    result = _run_outreach_agent(ctx, fill_request, shift, candidates, size, logger)
    if result["outcome"] != "completed":
        return _escalate_system(ctx, fill_request, logger, result["outcome"])
    if fill_request.state == "escalated":
        return FillOutcome("escalated", fill_request.id)

    selected = session.scalars(
        select(m.Outreach).where(
            m.Outreach.fill_request_id == fill_request.id,
            m.Outreach.tranche == fill_request.current_tranche,
        )
    ).all()
    if not selected:
        return _escalate_system(
            ctx, fill_request, logger, "Gloo did not choose replacements"
        )

    held = session.scalars(
        select(m.Approval).where(m.Approval.status == "pending")
    ).all()
    held = [a for a in held if a.payload.get("fill_request_id") == fill_request.id]
    if held:
        fill_request.state = "waiting_approval"
        fill_request.next_action_at = escalation_deadline(fill_request, shift.event, session)
        notifications.queue_staffing(ctx, shift.event)
        logger.step("decision", result={"waiting_approval": [a.id for a in held]})
        return FillOutcome("waiting_approval", fill_request.id)

    _mark_sent_outreach(ctx, fill_request)
    if all(o.response not in offers.OPEN_RESPONSES for o in selected):
        fill_request.next_action_at = offers.decision_time(session, ctx.clock)
        return FillOutcome("offer_blocked", fill_request.id)
    if any(o.message_id is None for o in selected):
        return _escalate_system(
            ctx, fill_request, logger, "Gloo did not complete replacement outreach"
        )
    active = [offers.metadata(session, o) for o in selected]
    deadlines = [r.expires_at for r in active if r and r.state == "offer_active"]
    fill_request.next_action_at = min(deadlines) if deadlines else escalation_deadline(fill_request, shift.event, session)
    return FillOutcome("tranche_sent", fill_request.id)


def _run_outreach_agent(
    ctx, fill_request, shift, candidates, size, logger: RunLogger
) -> dict:
    settings = get_settings()
    payload = {
        "task": "Choose the best replacements from the full eligible pool using choose_replacements. "
        "Then send each selected person a personal ask, schedule the next batch and summarize your choice.",
        "shift": shift_context(ctx.session, shift, ctx.clock.now()),
        "urgency": fill_request.urgency,
        "tranche": fill_request.current_tranche,
        "max_candidates": size or len(candidates),
        "candidates": [candidate_context(c) for c in candidates],
    }
    tools = fill_agent_tools(
        ctx.session, ctx.clock, ctx.gate, fill_request, tz=_tz(ctx), max_candidates=size
    )
    return run_agent(
        ctx.gloo,
        logger,
        model=settings.agent_model,
        instructions=PROMPT_PATH.read_text(),
        user_input=json.dumps(payload, default=str),
        tools=tools,
        max_steps=settings.max_agent_steps,
    )


def _advance(ctx: FillContext, fill_request: m.FillRequest) -> FillOutcome:
    session = ctx.session
    now = offers.decision_time(session, ctx.clock)
    if fill_request.state not in ("in_progress", "waiting_quiet", "waiting_approval"):
        return FillOutcome("not_in_progress", fill_request.id)
    shift = session.get(m.Shift, fill_request.shift_id)
    logger = RunLogger(session, ctx.clock, agent="fill_agent",
                       trigger=f"timer/advance (fill {fill_request.id})", log_dir=ctx.log_dir)

    for o in session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fill_request.id,
            m.Outreach.response.in_(offers.OPEN_RESPONSES))).all():
        row = offers.metadata(session, o)
        message = session.get(m.Message, o.message_id) if o.message_id else None
        if (message and message.status in ("dispatching", "uncertain") or
                row and row.state == "offer_uncertain"):
            return _escalate_unfilled(ctx, fill_request, logger, "delivery needs reconciliation before another offer")
        changed = row and row.detail.get("snapshot") != offers.snapshot(shift)
        if changed or shift.event.status in ("cancelled", "completed"):
            offers.close(session, o, "revoked", now)
        elif row and row.state == "offer_active" and now >= row.expires_at:
            offers.close(session, o, "expired", now)
        elif not session.get(m.Volunteer, o.volunteer_id).sms_opt_in:
            offers.close(session, o, "blocked", now)
        elif row and row.state == "offer_active":
            fill_request.next_action_at = row.expires_at
            return FillOutcome("waiting_offer", fill_request.id)
    past_deadline = now >= escalation_deadline(fill_request, shift.event, session)
    if shift.event.status in ("cancelled", "completed"):
        outcome = _escalate_unfilled(ctx, fill_request, logger, "event closed")
    elif past_deadline:
        outcome = _escalate_unfilled(ctx, fill_request, logger, "escalation deadline reached")
    elif fill_request.state == "waiting_approval":
        fill_request.next_action_at = escalation_deadline(fill_request, shift.event, session)
        outcome = FillOutcome("waiting_approval", fill_request.id)
    else:
        outcome = _open_tranche(ctx, fill_request, logger=logger)
    logger.close(outcome.action)
    return outcome


def _escalate_unfilled(ctx, fill_request, logger: RunLogger, why: str) -> FillOutcome:
    session, now = ctx.session, ctx.clock.now()
    shift = session.get(m.Shift, fill_request.shift_id)
    asked, declined = [], []
    for o in session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fill_request.id)):
        name = session.get(m.Volunteer, o.volunteer_id).name
        asked.append(name)
        if o.response == "no":
            declined.append(name)
    summary = (
        f"Unfilled: {shift.role.name} on {_when(ctx, shift.event)} ({why}). "
        f"Asked: {', '.join(asked) or 'nobody'}. Declined: {', '.join(declined) or 'nobody'}. "
        "Options: combine rooms, move someone from an optional role (with leader OK), or call directly."
    )
    offers.task_once(session, fill_request, now, summary)
    fill_request.state = "escalated"
    fill_request.next_action_at = None
    for approval in session.scalars(select(m.Approval).where(m.Approval.status == "pending")):
        if approval.payload.get("fill_request_id") == fill_request.id:
            approval.status = "expired"
    session.flush()
    notifications.queue_staffing(ctx, shift.event)
    logger.step("escalation", result={"why": why})
    return FillOutcome("escalated", fill_request.id, notes=[why])


def _escalate_system(ctx, fill_request, logger: RunLogger, why: str) -> FillOutcome:
    session, now = ctx.session, ctx.clock.now()
    shift = session.get(m.Shift, fill_request.shift_id)
    summary = f"Fill for {shift.role.name} on {_when(ctx, shift.event)} needs a human ({why}). Nothing was guessed."
    session.add(m.Escalation(category="system_error", severity="normal", summary=summary,
                             related_ids={"fill_request_id": fill_request.id},
                             assigned_to=_coordinator(session).id if _coordinator(session) else None,
                             status="open", created_at=now))
    fill_request.state = "escalated"
    fill_request.next_action_at = None
    session.flush()
    notifications.queue_staffing(ctx, shift.event)
    logger.step("escalation", result={"why": why})
    return FillOutcome("escalated_system", fill_request.id, notes=[why])


def _thank_the_rest(ctx, fill_request, winner_id: int) -> None:
    session = ctx.session
    for o in session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fill_request.id)):
        if o.volunteer_id == winner_id or o.response in ("no", "expired", "revoked", "blocked") or o.message_id is None:
            continue
        vol = session.get(m.Volunteer, o.volunteer_id)
        msg = session.get(m.Message, o.message_id)
        if msg.status == "queued":
            msg.status = "superseded"
            continue  # don't send a closure to someone whose ask never went out
        notifications.deliver(ctx, key=f"closed:{fill_request.id}:{vol.id}",
            body=templates.filled_thanks(vol.name), purpose="filled_thanks", volunteer=vol)


def _all_current_outreach_answered(ctx, fill_request) -> bool:
    open_asks = ctx.session.scalars(
        select(m.Outreach).where(
            m.Outreach.fill_request_id == fill_request.id, m.Outreach.response.in_(offers.OPEN_RESPONSES)
        )
    ).all()
    return not open_asks


def _mark_sent_outreach(ctx, fill_request) -> None:
    """Attach message ids to outreach rows sent via the approval path."""
    session = ctx.session
    rows = session.scalars(
        select(m.Outreach).where(
            m.Outreach.fill_request_id == fill_request.id, m.Outreach.message_id.is_(None)
        )
    ).all()
    for o in rows:
        meta = offers.metadata(session, o)
        if meta and meta.message_id:
            o.message_id = meta.message_id



def _upcoming_assignments(ctx, volunteer) -> list[m.Assignment]:
    return list(ctx.session.scalars(
        select(m.Assignment)
        .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
        .join(m.Event, m.Shift.event_id == m.Event.id)
        .where(
            m.Assignment.volunteer_id == volunteer.id,
            m.Assignment.status.in_(eligibility.ACTIVE_ASSIGNMENT_STATUSES),
            m.Event.starts_at > ctx.clock.now(),
        )
        .order_by(m.Event.starts_at)
    ))


def _resolve_assignment(ctx, upcoming: list[m.Assignment], hint: str | None) -> m.Assignment | None:
    if len(upcoming) == 1:
        return upcoming[0]
    if hint:
        tokens = hint.lower().split()
        matches = [a for a in upcoming if all(t in _describe(ctx, a).lower() for t in tokens)]
        if len(matches) == 1:
            return matches[0]
    return None


def _describe(ctx, assignment: m.Assignment) -> str:
    shift = ctx.session.get(m.Shift, assignment.shift_id)
    return f"{shift.role.name} {_when(ctx, shift.event)}"


def _when(ctx, event: m.Event) -> str:
    local = event.starts_at.astimezone(ZoneInfo(_tz(ctx)))
    return local.strftime("%a %b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def _tz(ctx) -> str:
    return PolicyStore(ctx.session).get("church_timezone")


def _coordinator(session) -> m.Volunteer | None:
    return session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator))


def _cancelled_volunteer_id(session, fill_request) -> int | None:
    if fill_request.cancelled_assignment_id is None:
        return None
    assignment = session.get(m.Assignment, fill_request.cancelled_assignment_id)
    return assignment.volunteer_id if assignment else None
