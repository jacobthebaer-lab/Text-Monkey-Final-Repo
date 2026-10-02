"""Fill agent: cancellation -> filled shift (PLAN.md section 9). Demo-critical.

Deterministic core, AI at the edges:
- code: identifying the shift, default urgency, candidate ranking, tranche
  membership and timing, assignment (with eligibility re-check), thank-yous,
  escalation deadlines
- model: writing the warm per-person asks, optionally adjusting urgency with
  a stated reason

If Gloo is unavailable the fill escalates to the coordinator — never guesses.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.clock import Clock
from app.config import get_settings
from app.core import eligibility, ranking, templates
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.gloo_client import GlooClient
from app.llm.tools import fill_agent_tools, shift_context
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

    @property
    def gate(self) -> SendGate:
        return SendGate(self.session, self.clock, self.provider)


@dataclass
class FillOutcome:
    action: str
    fill_request_id: int | None = None
    notes: list[str] = field(default_factory=list)


# --- tranche policy (timing lives in code; PLAN.md section 9.4) --------------

@dataclass(frozen=True)
class TranchePlan:
    sizes: tuple  # per-tranche candidate counts; None = everyone left
    waits: tuple  # timedelta per tranche
    escalate_margin: timedelta | None  # escalate this long before the shift


def tranche_plan(hours_until: float) -> TranchePlan:
    if hours_until > 48:
        return TranchePlan((3, 5, None), (timedelta(hours=4), timedelta(hours=4), timedelta(hours=6)), timedelta(hours=24))
    if hours_until > 12:
        return TranchePlan((3, 5, None), (timedelta(hours=1), timedelta(hours=1), timedelta(hours=2)), timedelta(hours=6))
    if hours_until > 2:
        return TranchePlan((3, 5, None), (timedelta(minutes=20), timedelta(minutes=20), timedelta(minutes=30)), timedelta(minutes=90))
    return TranchePlan((5, 8), (timedelta(minutes=10), timedelta(minutes=10)), None)


def next_wait_for(session, fill_request: m.FillRequest, now: datetime) -> timedelta:
    shift = session.get(m.Shift, fill_request.shift_id)
    hours_until = (shift.event.starts_at - now).total_seconds() / 3600
    plan = tranche_plan(hours_until)
    index = min(fill_request.current_tranche, len(plan.waits)) - 1
    return plan.waits[max(index, 0)]


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


def handle_shift_choice(ctx: FillContext, volunteer: m.Volunteer, choice: int) -> FillOutcome:
    """Numbered reply after a clarify_shift question (1-based, same ordering)."""
    upcoming = _upcoming_assignments(ctx, volunteer)
    if not 1 <= choice <= len(upcoming):
        ctx.gate.send(body=templates.clarify_generic(volunteer.name), purpose="clarify", volunteer=volunteer)
        return FillOutcome("invalid_choice")
    return _cancel_and_fill(ctx, volunteer, upcoming[choice - 1], sensitive=False)


def on_outreach_reply(ctx: FillContext, volunteer: m.Volunteer, outreach: m.Outreach, intent: str) -> FillOutcome:
    """inbound.py already recorded the response on the outreach row."""
    session, now = ctx.session, ctx.clock.now()
    fill_request = session.get(m.FillRequest, outreach.fill_request_id)
    shift = session.get(m.Shift, fill_request.shift_id)
    logger = RunLogger(session, ctx.clock, agent="fill_agent",
                       trigger=f"reply {intent} from {volunteer.name} (fill {fill_request.id})",
                       log_dir=ctx.log_dir)

    if intent == "accept":
        if fill_request.state == "filled":
            ctx.gate.send(body=templates.filled_thanks(volunteer.name), purpose="filled_thanks", volunteer=volunteer)
            logger.close("yes after filled; thanked")
            return FillOutcome("already_filled", fill_request.id)
        check = eligibility.check(session, volunteer, shift, tz=_tz(ctx))
        if not check:
            logger.step("decision", result={"ineligible_yes": check.reasons})
            ctx.gate.send(body=templates.thanks_anyway(volunteer.name), purpose="thanks", volunteer=volunteer)
            logger.close("ineligible yes; thanked, still searching")
            return FillOutcome("ineligible_yes", fill_request.id, notes=check.reasons)

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

        ctx.gate.send(
            body=templates.assignment_confirmation(volunteer.name, shift.role.name, _when(ctx, shift.event)),
            purpose="confirmation", volunteer=volunteer,
        )
        _thank_the_rest(ctx, fill_request, winner_id=volunteer.id)
        _notify_coordinator(ctx, f"{shift.role.name} on {_when(ctx, shift.event)} is covered — {volunteer.name} said yes.")
        logger.close("filled")
        return FillOutcome("filled", fill_request.id)

    if intent == "partial":
        # Safe default: thank them and keep looking for full coverage.
        ctx.gate.send(body=templates.partial_thanks(volunteer.name), purpose="thanks", volunteer=volunteer)
        logger.step("decision", result={"partial_offer": "thanked; continuing for full coverage"})

    if _all_current_outreach_answered(ctx, fill_request):
        logger.step("decision", result={"tranche_exhausted": "advancing early"})
        logger.close(f"{intent}; advancing early")
        return _advance(ctx, fill_request)

    logger.close(f"{intent} recorded")
    return FillOutcome(f"{intent}_recorded", fill_request.id)


def on_outreach_approved(ctx: FillContext, fill_request_id: int) -> None:
    """Coordinator said YES; the gate has sent the held asks. Restart the timer."""
    session = ctx.session
    fill_request = session.get(m.FillRequest, fill_request_id)
    if fill_request and fill_request.state == "waiting_approval":
        fill_request.state = "in_progress"
        fill_request.next_action_at = ctx.clock.now() + next_wait_for(session, fill_request, ctx.clock.now())
        _mark_sent_outreach(ctx, fill_request)


def advance_due(ctx: FillContext) -> list[FillOutcome]:
    """Timer tick: advance every fill request whose next_action_at has passed.

    Called by the scheduler job (app/jobs.py) and by demo fast-forward.
    """
    now = ctx.clock.now()
    due = ctx.session.scalars(
        select(m.FillRequest).where(
            m.FillRequest.state == "in_progress", m.FillRequest.next_action_at <= now
        )
    ).all()
    return [_advance(ctx, fr) for fr in due]


# --- internals -----------------------------------------------------------------


def _cancel_and_fill(ctx: FillContext, volunteer, assignment: m.Assignment, *, sensitive: bool) -> FillOutcome:
    session, now = ctx.session, ctx.clock.now()
    shift = session.get(m.Shift, assignment.shift_id)
    logger = RunLogger(session, ctx.clock, agent="fill_agent",
                       trigger=f"cancellation by {volunteer.name} (assignment {assignment.id})",
                       model=get_settings().agent_model, log_dir=ctx.log_dir)

    assignment.status = "cancelled"
    assignment.updated_at = now
    logger.step("decision", result={"cancelled_assignment": assignment.id, "sensitive": sensitive})

    if not sensitive:
        ctx.gate.send(body=templates.cancellation_ack(volunteer.name), purpose="cancellation_ack", volunteer=volunteer)

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
        _notify_coordinator(ctx, f"{volunteer.name} is off {shift.role.name} on {_when(ctx, shift.event)}; coverage is still fine, so I'm not asking anyone.")
        logger.close("skipped (enough coverage)")
        return FillOutcome("skipped", fill_request.id)

    outcome = _open_tranche(ctx, fill_request, exclude_ids=(volunteer.id,), logger=logger)
    logger.close(outcome.action)
    return outcome


def _open_tranche(ctx: FillContext, fill_request: m.FillRequest, *, exclude_ids=(), logger: RunLogger) -> FillOutcome:
    session, now = ctx.session, ctx.clock.now()
    shift = session.get(m.Shift, fill_request.shift_id)
    plan = tranche_plan((shift.event.starts_at - now).total_seconds() / 3600)

    if fill_request.current_tranche >= len(plan.sizes):
        return _escalate_unfilled(ctx, fill_request, logger, "all tranches exhausted")

    already_asked = set(
        session.scalars(select(m.Outreach.volunteer_id).where(m.Outreach.fill_request_id == fill_request.id))
    )
    cancelled_id = _cancelled_volunteer_id(session, fill_request)
    exclude = tuple(already_asked | set(exclude_ids) | ({cancelled_id} if cancelled_id else set()))
    ranked = ranking.rank_candidates(session, shift, now, exclude_ids=exclude, tz=_tz(ctx))
    size = plan.sizes[fill_request.current_tranche]
    members = ranked if size is None else ranked[:size]
    if not members:
        return _escalate_unfilled(ctx, fill_request, logger, "no eligible candidates left")

    fill_request.current_tranche += 1
    fill_request.state = "in_progress"
    for c in members:
        session.add(m.Outreach(fill_request_id=fill_request.id, volunteer_id=c.volunteer.id,
                               tranche=fill_request.current_tranche))
    session.flush()
    logger.step("decision", result={
        "tranche": fill_request.current_tranche,
        "members": [{"id": c.volunteer.id, "name": c.volunteer.name, "score": c.score} for c in members],
    })

    result = _run_outreach_agent(ctx, fill_request, shift, members, logger)
    if result["outcome"] != "completed":
        return _escalate_system(ctx, fill_request, logger, result["outcome"])

    held = session.scalars(
        select(m.Approval).where(m.Approval.status == "pending")
    ).all()
    held = [a for a in held if a.payload.get("fill_request_id") == fill_request.id]
    if held:
        fill_request.state = "waiting_approval"
        names = [session.get(m.Volunteer, a.payload["volunteer_id"]).name for a in held]
        cancelled = session.get(m.Volunteer, cancelled_id) if cancelled_id else None
        ctx.gate.send(
            body=templates.approval_request(cancelled.name if cancelled else "Someone",
                                            f"{shift.role.name} on {_when(ctx, shift.event)}", names),
            purpose="coordinator_notify", volunteer=_coordinator(session),
        )
        logger.step("decision", result={"waiting_approval": [a.id for a in held]})
        return FillOutcome("waiting_approval", fill_request.id)

    _mark_sent_outreach(ctx, fill_request)
    if fill_request.next_action_at is None:  # agent forgot schedule_next_tranche
        fill_request.next_action_at = now + plan.waits[fill_request.current_tranche - 1]
    return FillOutcome("tranche_sent", fill_request.id)


def _run_outreach_agent(ctx, fill_request, shift, members, logger: RunLogger) -> dict:
    settings = get_settings()
    payload = {
        "task": "A volunteer cancelled. Write and send one personal ask to each tranche member, "
                "then schedule the next tranche and summarize.",
        "shift": shift_context(ctx.session, shift, ctx.clock.now()),
        "urgency": fill_request.urgency,
        "tranche": fill_request.current_tranche,
        "members": [
            {"volunteer_id": c.volunteer.id, "name": c.volunteer.name, "why_ranked": c.breakdown}
            for c in members
        ],
    }
    tools = fill_agent_tools(ctx.session, ctx.clock, ctx.gate, fill_request, tz=_tz(ctx))
    return run_agent(
        ctx.gloo, logger,
        model=settings.agent_model,
        instructions=PROMPT_PATH.read_text(),
        user_input=json.dumps(payload, default=str),
        tools=tools,
        max_steps=settings.max_agent_steps,
    )


def _advance(ctx: FillContext, fill_request: m.FillRequest) -> FillOutcome:
    session, now = ctx.session, ctx.clock.now()
    if fill_request.state != "in_progress":
        return FillOutcome("not_in_progress", fill_request.id)
    shift = session.get(m.Shift, fill_request.shift_id)
    logger = RunLogger(session, ctx.clock, agent="fill_agent",
                       trigger=f"timer/advance (fill {fill_request.id})", log_dir=ctx.log_dir)

    plan = tranche_plan((shift.event.starts_at - now).total_seconds() / 3600)
    past_deadline = (
        plan.escalate_margin is not None and now >= shift.event.starts_at - plan.escalate_margin
    ) or now >= shift.event.starts_at
    if past_deadline:
        outcome = _escalate_unfilled(ctx, fill_request, logger, "escalation deadline reached")
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
    session.add(m.Escalation(category="unfillable", severity="urgent" if fill_request.urgency == "critical" else "normal",
                             summary=summary, related_ids={"fill_request_id": fill_request.id},
                             assigned_to=_coordinator(session).id if _coordinator(session) else None,
                             status="open", created_at=now))
    fill_request.state = "escalated"
    fill_request.next_action_at = None
    session.flush()
    _notify_coordinator(ctx, summary)
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
    _notify_coordinator(ctx, summary)
    logger.step("escalation", result={"why": why})
    return FillOutcome("escalated_system", fill_request.id, notes=[why])


def _thank_the_rest(ctx, fill_request, winner_id: int) -> None:
    session = ctx.session
    for o in session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fill_request.id)):
        if o.volunteer_id == winner_id or o.response in ("no",) or o.message_id is None:
            continue
        vol = session.get(m.Volunteer, o.volunteer_id)
        ctx.gate.send(body=templates.filled_thanks(vol.name), purpose="filled_thanks", volunteer=vol)


def _all_current_outreach_answered(ctx, fill_request) -> bool:
    open_asks = ctx.session.scalars(
        select(m.Outreach).where(
            m.Outreach.fill_request_id == fill_request.id, m.Outreach.response == "none"
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
        vol = session.get(m.Volunteer, o.volunteer_id)
        msg = session.scalar(
            select(m.Message).where(
                m.Message.volunteer_id == vol.id, m.Message.direction == "out",
                m.Message.purpose == "outreach",
            ).order_by(m.Message.id.desc())
        )
        if msg is not None:
            o.message_id = msg.id


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
    # No usable hint, but a reminder just went out ("Reply X if something came
    # up"): an X/cancel right after it means that reminded shift.
    recently_reminded = [
        a for a in upcoming
        if a.reminded_at is not None and a.reminded_at >= ctx.clock.now() - timedelta(hours=26)
    ]
    if len(recently_reminded) == 1:
        return recently_reminded[0]
    return None


def _describe(ctx, assignment: m.Assignment) -> str:
    shift = ctx.session.get(m.Shift, assignment.shift_id)
    return f"{shift.role.name} {_when(ctx, shift.event)}"


def _when(ctx, event: m.Event) -> str:
    local = event.starts_at.astimezone(ZoneInfo(_tz(ctx)))
    return local.strftime("%a %b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def _tz(ctx) -> str:
    return get_settings().church_timezone


def _coordinator(session) -> m.Volunteer | None:
    return session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator))


def _cancelled_volunteer_id(session, fill_request) -> int | None:
    if fill_request.cancelled_assignment_id is None:
        return None
    assignment = session.get(m.Assignment, fill_request.cancelled_assignment_id)
    return assignment.volunteer_id if assignment else None


def _notify_coordinator(ctx, text: str) -> None:
    coordinator = _coordinator(ctx.session)
    if coordinator is not None:
        ctx.gate.send(body=text[:320], purpose="coordinator_notify", volunteer=coordinator)
