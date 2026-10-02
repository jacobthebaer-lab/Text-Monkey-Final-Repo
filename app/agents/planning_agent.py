"""Planning agent: monthly schedule (PLAN.md section 10).

Deterministic: event scan + unknown-event escalation, shift generation from
recipes, availability asks and the 3-day nudge, the greedy solver, the
validator, publish approval, and the on-approval assignment texts.
Model: reviewing the finished draft and proposing swaps (each re-validated
in code), plus a short summary for the coordinator.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agents.fill_agent import FillContext, _coordinator, _notify_coordinator, _when
from app.config import get_settings
from app.core import availability as avail
from app.core import eligibility, scheduler, templates
from app.core.policies import PolicyStore
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.tools import ToolDef, _obj

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "planning_agent.md"
MAX_REVIEW_ROUNDS = 3
PLANNING_STATE_KEY = "planning_state"
NUDGE_AFTER_DAYS = 3


def month_label(month: str) -> str:
    year, mon = map(int, month.split("-"))
    return datetime(year, mon, 1).strftime("%B")


# --- step 1-3: scan events, generate shifts, ask availability -------------------


def start_planning(ctx: FillContext, month: str) -> dict:
    session, now = ctx.session, ctx.clock.now()
    logger = RunLogger(session, ctx.clock, agent="planning_agent",
                       trigger=f"start planning {month}", log_dir=ctx.log_dir)

    unknown = _scan_events(ctx, month, logger)
    created = _generate_missing_shifts(ctx, month, logger)
    asked = _send_availability_asks(ctx, month, logger)

    store = PolicyStore(session)
    row = session.get(m.Policy, PLANNING_STATE_KEY)
    state = {"month": month, "asks_sent_at": now.isoformat(), "nudged": False}
    if row is None:
        session.add(m.Policy(key=PLANNING_STATE_KEY, value={"value": state}))
    else:
        row.value = {"value": state}
    session.flush()

    logger.close(f"scan done: {unknown} unknown events, {created} shifts created, {asked} asks sent")
    return {"unknown_events": unknown, "shifts_created": created, "availability_asks": asked}


def _scan_events(ctx: FillContext, month: str, logger: RunLogger) -> int:
    session, now = ctx.session, ctx.clock.now()
    tz = ZoneInfo(get_settings().church_timezone)
    year, mon = map(int, month.split("-"))
    unknown = 0
    for event in session.scalars(select(m.Event).where(m.Event.status == "scheduled")):
        local = event.starts_at.astimezone(tz)
        if (local.year, local.month) != (year, mon) or event.event_type_id is not None:
            continue
        already = session.scalars(
            select(m.Escalation).where(m.Escalation.category == "unknown_event", m.Escalation.status == "open")
        ).all()
        if any(e.related_ids.get("event_id") == event.id for e in already):
            continue
        escalation = m.Escalation(
            category="unknown_event", severity="normal",
            summary=f"'{event.title}' on {_when(ctx, event)} has no event type; what help will it need?",
            related_ids={"event_id": event.id},
            assigned_to=_coordinator(session).id if _coordinator(session) else None,
            status="open", created_at=now,
        )
        session.add(escalation)
        session.flush()
        _notify_coordinator(
            ctx, f"I see '{event.title}' on {_when(ctx, event)}. What help will it need?"
        )
        logger.step("escalation", result={"unknown_event": event.title, "escalation_id": escalation.id})
        unknown += 1
    return unknown


def _generate_missing_shifts(ctx: FillContext, month: str, logger: RunLogger) -> int:
    session = ctx.session
    tz = ZoneInfo(get_settings().church_timezone)
    year, mon = map(int, month.split("-"))
    created = 0
    for event in session.scalars(select(m.Event).where(m.Event.status == "scheduled")):
        local = event.starts_at.astimezone(tz)
        if (local.year, local.month) != (year, mon) or event.event_type_id is None or event.shifts:
            continue
        for recipe in session.scalars(
            select(m.RoleRecipe).where(m.RoleRecipe.event_type_id == event.event_type_id)
        ):
            for slot in range(recipe.count):
                session.add(m.Shift(event_id=event.id, role_id=recipe.role_id, slot_index=slot))
                created += 1
    session.flush()
    if created:
        logger.step("decision", result={"shifts_created": created})
    return created


def _send_availability_asks(ctx: FillContext, month: str, logger: RunLogger) -> int:
    session = ctx.session
    label = month_label(month)
    sent = 0
    for vol in session.scalars(select(m.Volunteer).order_by(m.Volunteer.id)):
        if not vol.sms_opt_in or vol.status != "active" or vol.is_coordinator or vol.is_pastor:
            continue
        outcome = ctx.gate.send(
            body=templates.availability_ask(vol.name, label), purpose="availability_ask", volunteer=vol
        )
        if outcome.sent:
            sent += 1
    logger.step("decision", result={"availability_asks_sent": sent})
    return sent


def record_availability(ctx: FillContext, volunteer: m.Volunteer, body: str) -> m.Availability:
    month = open_planning_month(ctx)
    return avail.record_reply(ctx.session, volunteer, body, month, ctx.clock.now())


def open_planning_month(ctx: FillContext) -> str:
    row = ctx.session.get(m.Policy, PLANNING_STATE_KEY)
    if row and row.value["value"].get("month"):
        return row.value["value"]["month"]
    # No planning in flight: assume next month.
    local = ctx.clock.now().astimezone(ZoneInfo(get_settings().church_timezone))
    year, mon = (local.year + (local.month == 12), local.month % 12 + 1)
    return f"{year:04d}-{mon:02d}"


def nudge_nonresponders(ctx: FillContext) -> int:
    """One reminder, 3 days after the ask, to volunteers with no availability row."""
    session, now = ctx.session, ctx.clock.now()
    row = session.get(m.Policy, PLANNING_STATE_KEY)
    if row is None:
        return 0
    state = row.value["value"]
    if state.get("nudged") or not state.get("month"):
        return 0
    asks_sent_at = datetime.fromisoformat(state["asks_sent_at"])
    if now < asks_sent_at + timedelta(days=NUDGE_AFTER_DAYS):
        return 0

    month = state["month"]
    label = month_label(month)
    responded = set(
        session.scalars(select(m.Availability.volunteer_id).where(m.Availability.month == month))
    )
    nudged = 0
    for vol in session.scalars(select(m.Volunteer)):
        if vol.id in responded or not vol.sms_opt_in or vol.status != "active":
            continue
        if vol.is_coordinator or vol.is_pastor:
            continue
        if ctx.gate.send(
            body=templates.availability_ask(vol.name, label), purpose="availability_ask", volunteer=vol
        ).sent:
            nudged += 1
    row.value = {"value": {**state, "nudged": True}}
    session.flush()
    return nudged


# --- step 5-7: solve, review, publish approval ------------------------------------


def build_draft(ctx: FillContext, month: str) -> scheduler.ValidationResult:
    session, now = ctx.session, ctx.clock.now()
    settings = get_settings()
    logger = RunLogger(session, ctx.clock, agent="planning_agent",
                       trigger=f"build draft {month}", model=settings.agent_model, log_dir=ctx.log_dir)

    solve = scheduler.solve_month(session, month, now, tz=settings.church_timezone)
    logger.step("decision", result={"solver": {"assigned": solve.assigned, "total": solve.total_shifts,
                                               "fill_pct": solve.fill_pct, "gaps": len(solve.gaps)}})
    validation = scheduler.validate_month(session, month, tz=settings.church_timezone)

    for round_no in range(MAX_REVIEW_ROUNDS):
        if validation.ok and not validation.gaps:
            break
        result = _run_review(ctx, month, validation, logger)
        if result["outcome"] != "completed":
            logger.step("decision", result={"review_skipped": result["outcome"]})
            break
        new_validation = scheduler.validate_month(session, month, tz=settings.church_timezone)
        improved = (len(new_validation.gaps), len(new_validation.violations)) < (
            len(validation.gaps), len(validation.violations)
        )
        validation = new_validation
        logger.step("decision", result={"review_round": round_no + 1, "fill_pct": validation.fill_pct,
                                        "gaps": len(validation.gaps), "improved": improved})
        if not improved:
            break

    _create_publish_approval(ctx, month, validation, logger)
    logger.close(f"draft ready: {validation.fill_pct}% filled, {len(validation.gaps)} gaps, "
                 f"{len(validation.violations)} violations")
    return validation


def _review_tools(ctx: FillContext, month: str) -> dict[str, ToolDef]:
    session = ctx.session
    settings = get_settings()

    def get_draft_status(args: dict) -> dict:
        v = scheduler.validate_month(session, month, tz=settings.church_timezone)
        return {"fill_pct": v.fill_pct, "gaps": v.gaps[:20], "violations": v.violations[:20],
                "fairness": v.fairness}

    def propose_swap(args: dict) -> dict:
        shift = session.get(m.Shift, int(args["shift_id"]))
        new_vol = session.get(m.Volunteer, int(args["in_volunteer_id"]))
        if shift is None or new_vol is None:
            return {"error": "no such shift or volunteer"}
        out_id = args.get("out_volunteer_id")
        removed = None
        if out_id is not None:
            current = [
                a for a in shift.assignments
                if a.volunteer_id == int(out_id) and a.status in eligibility.ACTIVE_ASSIGNMENT_STATUSES
            ]
            if not current:
                return {"error": "that volunteer is not on that shift"}
            removed = current[0]
            removed_status = removed.status
            removed.status = "cancelled"
            removed.updated_at = ctx.clock.now()
            session.flush()

        def undo() -> None:
            if removed is not None:
                removed.status = removed_status
                session.flush()

        check = eligibility.check(session, new_vol, shift, tz=settings.church_timezone)
        if not check:
            undo()
            return {"error": "not eligible", "reasons": check.reasons}
        zone = ZoneInfo(settings.church_timezone)
        if scheduler._month_count(session, new_vol.id, month, zone) >= new_vol.preferences.get("max_per_month", 3):
            undo()
            return {"error": "over monthly max"}
        now = ctx.clock.now()
        session.add(m.Assignment(shift_id=shift.id, volunteer_id=new_vol.id, status="proposed",
                                 source="planner", created_at=now, updated_at=now))
        session.flush()
        return {"status": "swapped"}

    return {
        "get_draft_status": ToolDef(
            "get_draft_status", "Current draft: fill %, gaps, violations, fairness.", _obj({}, []),
            get_draft_status,
        ),
        "propose_swap": ToolDef(
            "propose_swap",
            "Move a shift to in_volunteer_id (optionally removing out_volunteer_id first). "
            "Eligibility and monthly max are re-checked in code; the call fails if the rules say no.",
            _obj({"shift_id": {"type": "integer"}, "in_volunteer_id": {"type": "integer"},
                  "out_volunteer_id": {"type": "integer"}}, ["shift_id", "in_volunteer_id"]),
            propose_swap,
        ),
    }


def _run_review(ctx: FillContext, month: str, validation: scheduler.ValidationResult, logger: RunLogger) -> dict:
    settings = get_settings()
    payload = {
        "task": "Review this draft schedule; use propose_swap only where it genuinely helps, then summarize.",
        "month": month,
        "fill_pct": validation.fill_pct,
        "gaps": validation.gaps[:20],
        "violations": validation.violations[:20],
        "fairness": validation.fairness,
    }
    return run_agent(
        ctx.gloo, logger, model=settings.agent_model, instructions=PROMPT_PATH.read_text(),
        user_input=json.dumps(payload, default=str), tools=_review_tools(ctx, month),
        max_steps=settings.max_agent_steps,
    )


def _create_publish_approval(ctx: FillContext, month: str, validation: scheduler.ValidationResult,
                             logger: RunLogger) -> m.Approval:
    session, now = ctx.session, ctx.clock.now()
    approval = m.Approval(
        kind="publish_schedule",
        payload={"month": month, "fill_pct": validation.fill_pct, "gaps": len(validation.gaps),
                 "violations": len(validation.violations)},
        status="pending", requested_at=now,
    )
    session.add(approval)
    session.flush()
    _notify_coordinator(
        ctx,
        f"{month_label(month)} schedule ready: {validation.fill_pct:.0f}% filled, "
        f"{len(validation.gaps)} gaps, {len(validation.violations)} conflicts. "
        "Review on the Approvals page or reply YES to publish.",
    )
    logger.step("decision", result={"publish_approval_id": approval.id})
    return approval


# --- step 8: publish -------------------------------------------------------------


def on_publish_approved(ctx: FillContext, month: str) -> int:
    """proposed -> approved for the month; assignment texts go out; reminders
    are handled by the daily job."""
    session, now = ctx.session, ctx.clock.now()
    settings = get_settings()
    zone = ZoneInfo(settings.church_timezone)
    logger = RunLogger(session, ctx.clock, agent="planning_agent",
                       trigger=f"publish {month}", log_dir=ctx.log_dir)
    year, mon = map(int, month.split("-"))

    published = 0
    for assignment in session.scalars(
        select(m.Assignment).where(m.Assignment.status == "proposed", m.Assignment.source == "planner")
    ):
        local = assignment.shift.event.starts_at.astimezone(zone)
        if (local.year, local.month) != (year, mon):
            continue
        assignment.status = "approved"
        assignment.updated_at = now
        published += 1
        ctx.gate.send(
            body=templates.assignment_confirmation(
                assignment.volunteer.name, assignment.shift.role.name, _when(ctx, assignment.shift.event)
            ),
            purpose="confirmation", volunteer=assignment.volunteer,
        )

    row = session.get(m.Policy, PLANNING_STATE_KEY)
    if row and row.value["value"].get("month") == month:
        row.value = {"value": {}}
    session.flush()
    logger.close(f"published {published} assignments for {month}")
    return published
