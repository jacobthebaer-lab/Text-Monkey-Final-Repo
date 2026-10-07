"""Gloo reads coordinator context and stages validated, exact record proposals."""
from pathlib import Path
import hashlib
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import get_settings
from app.core import admin_changes, confirmations
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.tools import ToolDef

PROMPT = Path(__file__).resolve().parents[2] / "prompts/admin_agent.md"
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ORDINALS = ("1st", "2nd", "3rd", "4th", "5th")


def _event_labels(event, tz):
    start, end = event.starts_at.astimezone(ZoneInfo(tz)), event.ends_at.astimezone(ZoneInfo(tz))
    def label(moment):
        offset = moment.isoformat()[-6:]
        return f"{moment.date().isoformat()} {DAYS[moment.weekday()]} {moment.strftime('%I:%M %p').lstrip('0')} {tz} (UTC{offset})"
    return {"event_id": event.id, "weekday_name": DAYS[start.weekday()], "timezone": tz,
        "local_starts_at": start.isoformat(), "local_ends_at": end.isoformat(),
        "start_label": label(start), "end_label": label(end)}


def _summary_problem(text, facts):
    """Only explicit structured planner claims, not general language inference.

    Compare ordinal+weekday phrases, ISO dates/timestamps, and explicit AM/PM
    time ranges against facts actually returned by these planning tools.
    Unrelated prose, bare month names and implicit time claims are not parsed.
    """
    if not text or not facts["active"]:
        return None
    day_pattern = '|'.join(DAYS)
    ordinal_pattern = r'first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th'
    ordinals = {name: i + 1 for i, name in enumerate(('first','second','third','fourth','fifth'))}
    ordinals.update({name: i + 1 for i, name in enumerate(ORDINALS)})
    for match in re.finditer(r'\b(' + ordinal_pattern + r')\s+(' + day_pattern + r')s?\b', text, re.I):
        if (ordinals[match[1].lower()], DAYS.index(match[2].capitalize())) not in facts['ordinals']:
            return 'Planner summary weekday does not match the returned evidence'
    for match in re.finditer(r'\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}))?\b', text):
        token = match[0]
        if len(token) == 10:
            if token not in facts['dates']:
                return 'Planner summary date is not present in the returned evidence'
            nearby = text[max(0, match.start()-18):min(len(text), match.end()+18)]
            names = re.findall(r'\b(' + day_pattern + r')\b', nearby, re.I)
            if len(names) == 1 and names[0].capitalize() != DAYS[date.fromisoformat(token).weekday()]:
                return 'Planner summary dated weekday does not match the returned evidence'
        else:
            try:
                moment = datetime.fromisoformat(token.replace('Z','+00:00'))
                if not any(moment == datetime.fromisoformat(e[field]) for e in facts['events'].values()
                    for field in ('local_starts_at','local_ends_at')):
                    return 'Planner summary timestamp is not present in the returned evidence'
            except ValueError:
                return 'Planner summary timestamp is invalid'
    time = r'(?:0?[1-9]|1[0-2])(?::[0-5]\d)?\s*[AP]M'
    def minutes(value):
        raw = re.sub(r'\s+', '', value.upper())
        hour, _, minute = raw[:-2].partition(':')
        return (int(hour) % 12 + (12 if raw[-2:] == 'PM' else 0)) * 60 + int(minute or 0)
    ranges = {(datetime.fromisoformat(e['local_starts_at']).hour * 60 + datetime.fromisoformat(e['local_starts_at']).minute,
        datetime.fromisoformat(e['local_ends_at']).hour * 60 + datetime.fromisoformat(e['local_ends_at']).minute)
        for e in facts['events'].values()}
    for match in re.finditer(r'\b(' + time + r')\s*(?:to|[-–,])\s*(' + time + r')\b', text, re.I):
        if (minutes(match[1]), minutes(match[2])) not in ranges:
            return 'Planner summary local time range does not match the returned evidence'
    endpoints = {minute for pair in ranges for minute in pair}
    for match in re.finditer(r'\b(' + time + r')\b', text, re.I):
        if minutes(match[1]) not in endpoints:
            return 'Planner summary local time is not present in the returned evidence'
    for match in re.finditer(r'\bEvent\s*#?\s*(\d+)\s+(starts|begins|ends)\s+(?:at\s+)?(' + time + r')\b', text, re.I):
        event = facts['events'].get(int(match[1]))
        field = 'local_ends_at' if match[2].lower() == 'ends' else 'local_starts_at'
        if not event or minutes(match[3]) != datetime.fromisoformat(event[field]).hour * 60 + datetime.fromisoformat(event[field]).minute:
            return 'Planner summary event time does not match the returned evidence'
    instant = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})'
    for match in re.finditer(r'\bEvent\s*#?\s*(\d+)\s+(starts|begins|ends)\s+(?:at\s+)?(' + instant + r')\b', text, re.I):
        event = facts['events'].get(int(match[1]))
        field = 'local_ends_at' if match[2].lower() == 'ends' else 'local_starts_at'
        try:
            if not event or datetime.fromisoformat(match[3].replace('Z','+00:00')) != datetime.fromisoformat(event[field]):
                return 'Planner summary event timestamp does not match the returned evidence'
        except ValueError:
            return 'Planner summary event timestamp is invalid'
    return None


def prepare(ctx, coordinator, command):
    if not coordinator.is_coordinator or coordinator.status != "active":
        raise ValueError("Active coordinator identity required")
    settings = getattr(ctx.gloo, "settings", get_settings())
    logger = RunLogger(ctx.session, ctx.clock, agent="admin_agent", trigger=command,
        model=settings.agent_model, log_dir=ctx.log_dir)
    from app.llm.parser import keyword_sensitive
    if keyword_sensitive(command):
        ctx.session.add(m.Escalation(category="sensitive", summary="Coordinator request needs private human review.",
            severity="normal", related_ids={"coordinator_id": coordinator.id}, status="open", created_at=ctx.clock.now()))
        result = {"outcome": "human_review", "final_text": None, "approval_ids": [], "applied": False}
        logger.step("decision", result=result)
        logger.close(result["outcome"])
        return result
    existing_reviews = set(ctx.session.scalars(select(m.Approval.id).where(
        m.Approval.kind == "confirm_record", m.Approval.status == "pending")))
    created = []
    context_read = [False]
    pattern_reads = {}
    pair_reads = {}
    summary_facts = {"active": False, "dates": set(), "ordinals": set(), "events": {}}

    def event_evidence(event, tz):
        labels = _event_labels(event, tz)
        summary_facts['events'][event.id] = labels
        summary_facts['dates'].update((labels['local_starts_at'][:10], labels['local_ends_at'][:10]))
        return labels

    def pattern_labels(pattern):
        if not pattern:
            return []
        return [{"weekday": w['weekday'], "weekday_name": DAYS[w['weekday']],
            "ordinal_labels": [f"{ORDINALS[n-1]} {DAYS[w['weekday']]}" for n in w['ordinals']]}
            for w in pattern['weekday_ordinals']]

    def planning_access(args, fields):
        if set(args) != set(fields) or not context_read[0]:
            raise ValueError("Read current context and supply only the required planning fields")
        ctx.session.flush()
        ctx.session.expire_all()  # Refresh facts after a model/network turn, not cached ORM history.
        current = ctx.session.scalar(select(m.Volunteer).where(m.Volunteer.id == coordinator.id)
            .execution_options(populate_existing=True))
        if not current or not current.is_coordinator or current.status != "active":
            raise ValueError("Active coordinator identity required")
        if "volunteer_id" in fields:
            identity = args["volunteer_id"]
            if type(identity) is not int:
                raise ValueError("Choose an existing volunteer ID")
            person = ctx.session.scalar(select(m.Volunteer).where(m.Volunteer.id == identity)
                .execution_options(populate_existing=True))
            if not person:
                raise ValueError("Choose an existing volunteer ID")
            return person

    def pattern_facts(person):
        from app.core import planning_patterns
        from app.core.policies import PolicyStore
        now = ctx.clock.now()
        tz = str(PolicyStore(ctx.session).church_tz())
        report = planning_patterns.learned_patterns(ctx.session, person, now, tz)
        facts = {"report": report, "before": confirmations.values(person), "timezone": tz,
            "month": now.astimezone(PolicyStore(ctx.session).church_tz()).strftime("%Y-%m")}
        fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()
        return report, fingerprint, tz

    def learned(args):
        person = planning_access(args, {"volunteer_id"})
        report, fingerprint, tz = pattern_facts(person)
        pattern_reads[person.id] = fingerprint
        summary_facts['active'] = True
        evidence = []
        for item in report.get('evidence', []):
            summary_facts['ordinals'].add((item['ordinal'], item['weekday']))
            observations = [{**o, **event_evidence(ctx.session.get(m.Event, o['event_id']), tz)}
                for o in item['observations']]
            evidence.append({**item, "weekday_name": DAYS[item['weekday']],
                "ordinal_label": f"{ORDINALS[item['ordinal']-1]} {DAYS[item['weekday']]}", "observations": observations})
        return {**report, "evidence": evidence, "pattern_labels": pattern_labels(report.get('proposal')),
            "timezone": tz, "evidence_hash": fingerprint}

    def stage_pattern(args):
        from app.core import planning_patterns
        person = planning_access(args, {"volunteer_id", "evidence_hash"})
        report, fingerprint, tz = pattern_facts(person)
        if (not isinstance(args["evidence_hash"], str) or pattern_reads.get(person.id) != fingerprint
                or args["evidence_hash"] != fingerprint or report.get("held") or not report.get("proposal")):
            raise ValueError("Read unchanged completed serving evidence before requesting its exact review")
        before = confirmations.values(person)
        after = {"preferences": {**(person.preferences or {}), planning_patterns.KEY: report["proposal"]}}
        if before["preferences"] == after["preferences"]:
            return {"approval_ids": [], "applied": False, "state": "already_recorded"}
        records = {("Volunteer", coordinator.id): confirmations.values(coordinator),
            ("Volunteer", person.id): before}
        for evidence in report["evidence"]:
            for observation in evidence["observations"]:
                assignment = ctx.session.get(m.Assignment, observation["assignment_id"])
                for label, obj in (("Assignment", assignment), ("Shift", assignment.shift),
                                   ("Event", assignment.shift.event)):
                    records[(label, obj.id)] = confirmations.values(obj)
        source = {"action": "calendar_pattern_review", "requested_by": coordinator.id,
            "timezone": tz, "records": [{"record": label, "id": identity, "values": values}
                for (label, identity), values in sorted(records.items())]}
        selected = ctx.session.info.get("mac_test_session")
        if selected and not selected.active(ctx.clock.now()):
            raise ValueError("Selected review session expired")
        reason = f"Coordinator {coordinator.id} proposes recorded serving pattern {fingerprint}; exact review required"
        prior = next((a for a in ctx.session.scalars(select(m.Approval).where(
            m.Approval.kind == "confirm_record", m.Approval.status == "pending"))
            if confirmations.valid(a, ctx.clock.now()) and a.payload.get("record_id") == person.id
            and a.payload.get("record") == "Volunteer" and a.payload.get("before") == before
            and a.payload.get("after") == after and a.payload.get("reason") == reason
            and a.payload.get("admin_change_source") == source
            and a.payload.get("transport") == ctx.session.info.get("conversation_origin", "mock_or_twilio")
            and a.payload.get("session_id") == (selected.id if selected else None)), None)
        review = prior or planning_patterns.stage_pattern_review(ctx.session, person, report["proposal"],
            ctx.clock.now(), reason=reason)
        if not prior:
            review.payload = {**review.payload, "admin_change_source": source}
            review.payload = {**review.payload, "content_hash": confirmations.digest(review.payload)}
        if review.id not in created:
            created.append(review.id)
        return {"approval_ids": [review.id], "record": "Volunteer", "volunteer_id": person.id,
            "calendar_patterns": report["proposal"], "pattern_labels": pattern_labels(report['proposal']),
            "applied": False, "state": "pending_exact_review"}

    def pair_facts(person):
        from app.core import paired_planning
        from app.core.policies import PolicyStore
        roles = [{"record": "Role", "id": r.id, "values": confirmations.values(r)}
            for r in ctx.session.scalars(select(m.Role).order_by(m.Role.id))]
        source = {"action": "paired_preference_review", "requested_by": coordinator.id,
            "timezone": str(PolicyStore(ctx.session).church_tz()), "records": [
                {"record": "Volunteer", "id": coordinator.id, "values": confirmations.values(coordinator)},
                {"record": "Volunteer", "id": person.id, "values": confirmations.values(person)}, *roles]}
        pending = (person.preferences or {}).get("pending_constraints", [])
        if not isinstance(pending, list):
            raise ValueError("Pending scheduling constraints need human review")
        # Expose structured scheduling facts only, never phone, consent or private notes.
        report = {"volunteer_id": person.id, "volunteer_name": person.name,
            "same_day_role_pairs": paired_planning.rules(ctx.session, person)["same_day_role_pairs"],
            "pending_same_day_constraints": [{"index": i, "role_ids": item["role_ids"]}
                for i, item in enumerate(pending) if isinstance(item, dict) and item.get("kind") == "same_day"
                and isinstance(item.get("role_ids"), list)
                and all(type(ident) is int for ident in item["role_ids"])],
            "evidence_hash": paired_planning.fingerprint(source)}
        return report, source

    def read_pairs(args):
        person = planning_access(args, {"volunteer_id"})
        report, _ = pair_facts(person)
        pair_reads[person.id] = report["evidence_hash"]
        return report

    def stage_pairs(args):
        from app.core import paired_planning
        person = planning_access(args, {"volunteer_id", "evidence_hash", "pairs", "resolved_constraint_indexes"})
        report, source = pair_facts(person)
        indexes = args["resolved_constraint_indexes"]
        if (not isinstance(args["evidence_hash"], str)
                or pair_reads.get(person.id) != report["evidence_hash"]
                or args["evidence_hash"] != report["evidence_hash"]
                or not isinstance(indexes, list) or any(type(i) is not int for i in indexes)
                or len(set(indexes)) != len(indexes)):
            raise ValueError("Read unchanged paired preferences and choose exact pending indexes before review")
        review = paired_planning.stage_rules(ctx.session, ctx.clock.now(), person,
            pairs=args["pairs"], resolved_constraint_indexes=indexes, admin_change_source=source)
        if review.id not in created:
            created.append(review.id)
        return {"approval_ids": [review.id], "record": "Volunteer", "volunteer_id": person.id,
            "same_day_role_pairs": review.payload["workflow_planning_rules"]["rules"]["same_day_role_pairs"],
            "resolved_constraint_indexes": indexes, "applied": False, "state": "pending_exact_review"}

    def seasonal(args):
        from app.core import planning_patterns
        from app.core.policies import PolicyStore
        planning_access(args, {"month"})
        tz = str(PolicyStore(ctx.session).church_tz())
        report = planning_patterns.seasonal_staffing_report(ctx.session, args["month"], ctx.clock.now(), tz)
        summary_facts['active'] = True
        recommendations = []
        for item in report.get('recommendations', []):
            recommendations.append({**item, **event_evidence(ctx.session.get(m.Event, item['event_id']), tz),
                "evidence": [{**e, **event_evidence(ctx.session.get(m.Event, e['event_id']), tz)}
                    for e in item['evidence']]})
        return {**report, "recommendations": recommendations}

    def read(args):
        from app.core.policies import PolicyStore
        context_read[0] = True
        session = ctx.session
        summary_facts['dates'].add(ctx.clock.now().astimezone(PolicyStore(session).church_tz()).date().isoformat())
        events = list(session.scalars(select(m.Event).where(m.Event.starts_at >= ctx.clock.now())
            .order_by(m.Event.starts_at).limit(60)))
        shifts = list(session.scalars(select(m.Shift).where(m.Shift.event_id.in_([e.id for e in events]))))
        return {
            "church_timezone": str(PolicyStore(session).church_tz()), "now": ctx.clock.now().isoformat(),
            "events": [{"id": e.id, **event_evidence(e, str(PolicyStore(session).church_tz())),
                **{k: v for k, v in confirmations.values(e).items()
                if k != "gcal_event_id"}} for e in events],
            "event_types": [{"id": t.id, "name": t.name} for t in session.scalars(select(m.EventType))],
            "recipes": [{"id": r.id, **confirmations.values(r)} for r in session.scalars(select(m.RoleRecipe))],
            "roles": [{"id": r.id, **confirmations.values(r)} for r in session.scalars(select(m.Role))],
            "volunteers": [{"id": v.id, "name": v.name, "status": v.status,
                "paused_roles": v.preferences.get("paused_roles", [])} for v in session.scalars(select(m.Volunteer))],
            "shifts": [{"id": s.id, "event_id": s.event_id, "role_id": s.role_id} for s in shifts],
            "assignments": [{"id": a.id, "shift_id": a.shift_id, "volunteer_id": a.volunteer_id, "status": a.status}
                for a in session.scalars(select(m.Assignment).where(m.Assignment.shift_id.in_([s.id for s in shifts]),
                    m.Assignment.status.in_(("proposed", "approved", "confirmed"))))],
        }

    def proposal(args):
        if not context_read[0]:
            return {"error": "Read current context before choosing existing IDs or dates"}
        result = admin_changes.propose(ctx, coordinator, args)
        created.extend(i for i in result["approval_ids"] if i not in created)
        return result

    tools = {
        "read_context": ToolDef("read_context", "Read saved schedules, assignments, roles, recipes and people",
            {"type": "object", "properties": {}, "additionalProperties": False}, read),
        "learned_patterns": ToolDef("learned_patterns", "Read completed serving evidence, never infer annual absence or apply preferences.",
            {"type": "object", "additionalProperties": False, "properties": {
                "volunteer_id": {"type": "integer"}}, "required": ["volunteer_id"]}, learned),
        "stage_pattern_review": ToolDef("stage_pattern_review", "Stage the unchanged learned proposal for exact human record review. Never approve or assign.",
            {"type": "object", "additionalProperties": False, "properties": {
                "volunteer_id": {"type": "integer"}, "evidence_hash": {"type": "string"}},
                "required": ["volunteer_id", "evidence_hash"]}, stage_pattern),
        "read_paired_preferences": ToolDef("read_paired_preferences", "Read existing same-date role pairs and structured pending indexes before exact review.",
            {"type": "object", "additionalProperties": False, "properties": {
                "volunteer_id": {"type": "integer"}}, "required": ["volunteer_id"]}, read_pairs),
        "stage_paired_preference_review": ToolDef("stage_paired_preference_review", "Stage explicit coordinator-selected role pairs for exact human review. Never approve, qualify, book or contact anyone.",
            {"type": "object", "additionalProperties": False, "properties": {
                "volunteer_id": {"type": "integer"}, "evidence_hash": {"type": "string"},
                "pairs": {"type": "array", "maxItems": 4, "items": {"type": "object", "additionalProperties": False,
                    "properties": {"role_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}},
                    "required": ["role_ids"]}},
                "resolved_constraint_indexes": {"type": "array", "items": {"type": "integer"}, "uniqueItems": True}},
                "required": ["volunteer_id", "evidence_hash", "pairs", "resolved_constraint_indexes"]}, stage_pairs),
        "seasonal_staffing_report": ToolDef("seasonal_staffing_report", "Read verified seasonal slot evidence. Never change staffing or contact anyone.",
            {"type": "object", "additionalProperties": False, "properties": {
                "month": {"type": "string"}}, "required": ["month"]}, seasonal),
        "propose_change": ToolDef("propose_change", "Prepare exact human review. Never apply changes or contact anyone.",
            {"type": "object", "additionalProperties": False, "properties": {
                "action": {"type": "string", "enum": list(admin_changes.ACTIONS)},
                "event_id": {"type": "integer"}, "event_type_id": {"type": ["integer", "null"]},
                "role_id": {"type": "integer"}, "volunteer_id": {"type": "integer"},
                "count": {"type": "integer"}, "name": {"type": "string"}, "title": {"type": "string"},
                "starts_at": {"type": "string"}, "ends_at": {"type": "string"},
                "dates": {"type": "array", "items": {"type": "string"}}}, "required": ["action"]}, proposal),
    }
    result = run_agent(ctx.gloo, logger, model=settings.agent_model, instructions=PROMPT.read_text(),
        user_input=command, tools=tools, max_steps=settings.max_agent_steps)
    if result["outcome"] != "completed":
        # A partial tool run cannot leave its new proposals available after an outage.
        for identity in created:
            if identity not in existing_reviews:
                ctx.session.get(m.Approval, identity).status = "expired"
        ctx.session.add(m.Escalation(category="system_error", summary="Coordinator command held for Gloo review.",
            severity="normal", related_ids={"approval_ids": created}, status="open", created_at=ctx.clock.now()))
    elif narration_problem := _summary_problem(result.get('final_text'), summary_facts):
        # Keep correct exact reviews available; withhold unsupported model prose.
        result = {**result, "final_text": None, "narration_state": "held", "narration_reason": narration_problem}
    logger.step("decision", result={**result, "approval_ids": created, "applied": False})
    logger.close(result["outcome"])
    return {**result, "approval_ids": created, "applied": False}


def apply(ctx, approval):
    """Compatibility for existing mock legacy reviews, never connected use."""
    from app.sms.mock_provider import MockSMSProvider
    if confirmations.enabled(ctx.session) or not isinstance(ctx.provider, MockSMSProvider):
        raise ValueError("Use signed-in exact record review")
    if approval.status != "approved" or approval.kind != "admin_change":
        raise ValueError("Explicit coordinator approval required")
    p, session = approval.payload, ctx.session
    if p["action"] == "add_slots":
        event, role = session.get(m.Event, p["event_id"]), session.get(m.Role, p["role_id"])
        if not event or event.status != "scheduled" or event.starts_at <= ctx.clock.now() or not role:
            raise ValueError("Event or role changed; review again")
        slots = list(session.scalars(select(m.Shift.slot_index).where(m.Shift.event_id == event.id, m.Shift.role_id == role.id)))
        start = max(slots, default=-1) + 1
        for index in range(start, start + p["count"]):
            session.add(m.Shift(event_id=event.id, role_id=role.id, slot_index=index))
    elif p["action"] == "pause_role":
        volunteer, role = session.get(m.Volunteer, p["volunteer_id"]), session.get(m.Role, p["role_id"])
        if not volunteer or not role:
            raise ValueError("Volunteer or role changed")
        prefs = dict(volunteer.preferences)
        prefs["paused_roles"] = sorted(set(prefs.get("paused_roles", []) + [role.name]))
        volunteer.preferences = prefs
        session.add(m.Escalation(category="unclear", severity="normal", status="open", created_at=ctx.clock.now(),
            summary=f"Review {volunteer.name}'s existing {role.name} assignments after the approved pause.",
            related_ids={"volunteer_id": volunteer.id, "role_id": role.id}))
    elif p["action"] == "mark_unavailable":
        for d in p["dates"]:
            row = session.scalar(select(m.Availability).where(m.Availability.volunteer_id == p["volunteer_id"],
                m.Availability.month == d[:7]).order_by(m.Availability.id.desc()))
            if row is None:
                row = m.Availability(volunteer_id=p["volunteer_id"], month=d[:7], available_dates=[], unavailable_dates=[])
                session.add(row)
            row.unavailable_dates = sorted(set((row.unavailable_dates or []) + [d]))
            row.parsed_at = ctx.clock.now()
    else:
        raise ValueError("Unknown approved action")
    session.flush()
    return {"applied": p["action"]}
