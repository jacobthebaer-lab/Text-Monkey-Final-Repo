"""Clyde's planner mapped to durable, event-scoped application records.

Selection is code-owned; Gloo only composes the selected people's messages.
Real transport activation is separate. No response distribution is invented.
"""
from datetime import datetime, timedelta
from math import fsum

from sqlalchemy import select, or_

from app.core import clyde_algorithm as algo
from app.core import offer_windows as offers
from app.core.policies import PolicyStore
from app.db import models as m

DEFAULT_CONFIG = {"k": 100.0, "timescale_minutes": 120.0, "maximum_buffer": 0.5}
SUCCESSFUL = ("sent", "submitted", "delivered")
TERMINAL = ("yes", "no", "expired", "partial", "ineligible")


def config(session):
    raw = PolicyStore(session).get("recipient_algorithm")
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_CONFIG):
        raise ValueError("Invalid recipient algorithm settings")
    values = {**DEFAULT_CONFIG, **raw}
    if any(type(v) not in (int, float) for v in values.values()):
        raise ValueError("Recipient algorithm settings must be numbers")
    return (algo.ScoringConfig(values["k"]),
            algo.UrgencyConfig(values["timescale_minutes"], values["maximum_buffer"]))


def history(session, volunteer_id, as_of):
    """Use successful provider dispatches, never a draft/queue/uncertain send."""
    records = []
    for outreach, message in session.execute(select(m.Outreach, m.Message).join(
            m.Message, m.Outreach.message_id == m.Message.id).where(
            m.Outreach.volunteer_id == volunteer_id, m.Message.direction == "out",
            m.Message.purpose == "outreach", m.Message.status.in_(SUCCESSFUL))):
        meta = offers.metadata(session, outreach)
        stamp = meta.detail.get("dispatched_at") if meta else None
        # Legacy successful synchronous sends have the real send creation time.
        when = datetime.fromisoformat(stamp) if stamp else message.created_at
        if when <= as_of:
            records.append((outreach, when, meta))
    return records


def _measured(session, volunteer, as_of, baseline=None):
    records = history(session, volunteer.id, as_of)
    resolved = []
    replies = []
    for row, when, meta in records:
        responded = row.responded_at is not None and when <= row.responded_at <= as_of
        expired = meta and meta.expires_at is not None and meta.expires_at <= as_of
        if responded and row.response in TERMINAL or row.response == "expired" and expired:
            resolved.append(row.response)
        if responded:
            elapsed = algo.minutes_between(row.responded_at, when)
            if elapsed > 0:
                replies.append(elapsed)
    acceptance = (resolved.count("yes") / len(resolved) if resolved else
                  baseline.acceptance_rate if baseline else algo.DEFAULT_ACCEPTANCE_RATE)
    response = (fsum(replies) / len(replies) if replies else
                baseline.average_response_minutes if baseline else algo.DEFAULT_RESPONSE_MINUTES)
    return algo.Volunteer(str(volunteer.id), acceptance, response,
                          max((when for _, when, _ in records), default=None),
                          baseline.initial_scoring_reference_at if baseline else None)


def profile(session, volunteer, now):
    """Persist enrollment estimates separately from real outreach history.

    Older imports are reconstructed from peer history available at enrollment,
    excluding later replies. The snapshot remains fixed across scheduler runs.
    """
    key = f"algorithm-enrollment:{volunteer.id}"
    row = session.get(m.Policy, key)
    if row is None:
        enrolled = volunteer.created_at
        peers = session.scalars(select(m.Volunteer).where(
            or_(m.Volunteer.created_at < enrolled,
                (m.Volunteer.created_at == enrolled) & (m.Volunteer.id < volunteer.id)),
            m.Volunteer.is_coordinator.is_(False),
            m.Volunteer.is_pastor.is_(False)).order_by(m.Volunteer.id)).all()
        pool = []
        for peer in peers:
            saved = session.get(m.Policy, f"algorithm-enrollment:{peer.id}")
            values = saved.value["value"] if saved else None
            prior = algo.Volunteer(str(peer.id),
                values["acceptance_rate"] if values else algo.DEFAULT_ACCEPTANCE_RATE,
                values["average_response_minutes"] if values else algo.DEFAULT_RESPONSE_MINUTES,
                None, datetime.fromisoformat(values["initial_scoring_reference_at"]) if values else
                peer.created_at-timedelta(minutes=algo.DEFAULT_ELAPSED_MINUTES))
            pool.append(_measured(session, peer, enrolled, prior))
        baseline = algo.create_volunteer(str(volunteer.id), pool, enrolled)
        row = m.Policy(key=key, value={"value": {
            "acceptance_rate": baseline.acceptance_rate,
            "average_response_minutes": baseline.average_response_minutes,
            "initial_scoring_reference_at": baseline.initial_scoring_reference_at.isoformat(),
            "enrolled_at": enrolled.isoformat()}})
        session.add(row)
        session.flush()
    value = row.value["value"]
    baseline = algo.Volunteer(str(volunteer.id), value["acceptance_rate"],
        value["average_response_minutes"], None,
        datetime.fromisoformat(value["initial_scoring_reference_at"]))
    return _measured(session, volunteer, now, baseline)


def contacted_for_event(session, fill):
    shift = session.get(m.Shift, fill.shift_id)
    rows = session.execute(select(m.Outreach.volunteer_id, m.Outreach.id).join(m.FillRequest).join(
        m.Shift, m.FillRequest.shift_id == m.Shift.id).where(m.Shift.event_id == shift.event_id)).all()
    contacted = {person for person, _ in rows}
    if shift.parent_shift_id is not None:
        from app.core.split_coverage import child_problem, instant
        if child_problem(session, shift) is None:
            review = session.get(m.Approval, shift.coverage_review_id)
            source, interval = review.payload['source'], review.payload['normalized']
            # Only the reviewed original partial helper may receive a fresh
            # child offer, within their interpreted interval. All other event
            # contacts, sibling reservations and same-child dedup remain held.
            person = source['volunteer_id']
            if (instant(interval['start']) <= shift.starts_at and shift.ends_at <= instant(interval['end'])
                    and not any(p == person and ident != source['outreach_id'] for p, ident in rows)):
                contacted.discard(person)
    return contacted


def receipt(session, fill, tranche=None):
    return session.get(m.Notification, f"algorithm-batch:{fill.id}:{tranche or fill.current_tranche}")


def selected_ids(session, fill, tranche=None):
    row = receipt(session, fill, tranche)
    return tuple(row.detail["volunteer_ids"]) if row else ()


def pending_declines(session, fill):
    handled = set()
    for row in session.scalars(select(m.Notification).where(m.Notification.purpose == "algorithm_batch",
            m.Notification.event_id == session.get(m.Shift, fill.shift_id).event_id)):
        if row.detail.get("fill_request_id") == fill.id:
            handled.update(row.detail.get("decline_ids", []))
    return [o.id for o in session.scalars(select(m.Outreach).where(
        m.Outreach.fill_request_id == fill.id, m.Outreach.response == "no").order_by(m.Outreach.id))
        if o.id not in handled]


def plan(session, fill, candidates, now, *, decline=False):
    score, urgency = config(session)
    shift = session.get(m.Shift, fill.shift_id)
    deadline = offers.cutoff(session, shift.starts_at)
    pool = [profile(session, c.volunteer, now) for c in candidates
            if not ask_problem(session, c.volunteer.id, now)]
    if decline:
        next_person = algo.next_after_decline(pool, (), 1, now, deadline, score)
        selected = (next_person,) if next_person else ()
        return algo.RequestPlan(selected, sum(v.acceptance_rate for v in selected), 1.0)
    # Each Shift is one vacancy. Multiple selected requests compete for that
    # vacancy; acceptance remains serialized and never creates a second booking.
    return algo.plan_initial_batch(pool, 1, now, score, deadline=deadline, urgency=urgency)


def reserve(session, fill, plan, now, *, decline_ids=()):
    shift = session.get(m.Shift, fill.shift_id)
    ids = [int(v.volunteer_id) for v in plan.volunteers]
    score, _ = config(session)
    signals = [{"volunteer_id": int(v.volunteer_id), "score": algo.volunteer_score(v, now, score),
                "acceptance_rate": v.acceptance_rate,
                "average_response_minutes": v.average_response_minutes,
                "elapsed_minutes": algo.elapsed_since_request(v, now),
                "last_requested_at": v.last_requested_at.isoformat() if v.last_requested_at else None}
               for v in plan.volunteers]
    row = m.Notification(key=f"algorithm-batch:{fill.id}:{fill.current_tranche}",
        event_id=shift.event_id, purpose="algorithm_batch", body="", state="planned",
        created_at=now, due_at=now, expires_at=offers.cutoff(session, shift.starts_at),
        detail={"fill_request_id": fill.id, "tranche": fill.current_tranche,
                "volunteer_ids": ids, "decline_ids": list(decline_ids), "signals": signals,
                "expected_acceptances": plan.expected_acceptances, "target": plan.target,
                "target_met": plan.target_met, "snapshot": offers.snapshot(shift),
                "config": PolicyStore(session).get("recipient_algorithm")})
    session.add(row)
    for volunteer_id in ids:
        session.add(m.Outreach(fill_request_id=fill.id, volunteer_id=volunteer_id,
                               tranche=fill.current_tranche))
    session.flush()
    return row


def valid_member(session, outreach):
    fill = session.get(m.FillRequest, outreach.fill_request_id)
    shift = session.get(m.Shift, fill.shift_id)
    row = receipt(session, fill, outreach.tranche)
    return bool(row and row.detail.get("fill_request_id") == fill.id and
        row.detail.get("tranche") == outreach.tranche and
        outreach.volunteer_id in row.detail.get("volunteer_ids", []) and
        row.detail.get("snapshot") == offers.snapshot(shift))


def conflicting_offer(session, outreach):
    """Only code-reserved members of the same fill may share its vacancy."""
    fill = session.get(m.FillRequest, outreach.fill_request_id)
    others = session.scalars(select(m.Outreach).join(m.FillRequest).where(
        m.Outreach.id != outreach.id, m.Outreach.response.in_(offers.OPEN_RESPONSES),
        m.FillRequest.state.in_(offers.OPEN_FILLS),
        ((m.FillRequest.shift_id == fill.shift_id) | (m.Outreach.volunteer_id == outreach.volunteer_id))))
    for other in others:
        if (other.volunteer_id == outreach.volunteer_id or other.fill_request_id != fill.id or
                not valid_member(session, outreach) or not valid_member(session, other)):
            return True
    return False


def ask_problem(session, volunteer_id, now, *, exclude_outreach_id=None):
    """Budgets use actual outreach dispatch time, including delayed native queues."""
    policies = PolicyStore(session)
    times = [when for row, when, _ in history(session, volunteer_id, now)
             if row.id != exclude_outreach_id]
    if any(when > now-timedelta(hours=int(policies.get("outreach_cooldown_hours"))) for when in times):
        return "outreach cooldown reached"
    local = now.astimezone(policies.church_tz())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Availability asks have no offer window; preserve their existing accounting.
    other = session.scalars(select(m.Message.created_at).where(m.Message.volunteer_id == volunteer_id,
        m.Message.direction == "out", m.Message.purpose == "availability_ask",
        m.Message.status.in_(SUCCESSFUL), m.Message.created_at >= start, m.Message.created_at <= now)).all()
    if sum(when >= start for when in times)+len(other) >= policies.ask_budget():
        return "monthly ask budget reached"
    return None
