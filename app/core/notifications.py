"""Durable delivery with dedupe, quiet-hour retry, and event-level summaries."""
from datetime import datetime, timedelta, timezone
import hashlib
from sqlalchemy import select, func
from app.db import models as m
from app.core.send_gate import SendStatus
from app.core.signup_responder import compose_signup_reply
from app.llm.gloo_client import GlooUnavailableError

PRE_EVENT_LEAD = timedelta(hours=3)


def pre_event_source(session, notification, *, snapshot=None, fills=None, approvals=None):
    """Code-owned facts used by the exact pre-event copy, never model authority."""
    from app.core.policies import PolicyStore
    event = session.get(m.Event, notification.event_id, populate_existing=True)
    recipient = session.get(m.Volunteer, notification.volunteer_id, populate_existing=True)
    if not event or not recipient:
        return None
    snapshot = dict(snapshot if snapshot is not None else staffing_snapshot(session, event))
    snapshot['starts_at'] = event.starts_at.astimezone(timezone.utc).isoformat()
    fills = fills if fills is not None else session.scalars(select(m.FillRequest).join(m.Shift).where(
        m.Shift.event_id == event.id).execution_options(populate_existing=True)).all()
    pending_ids = {f.id for f in fills if f.state == 'waiting_approval'}
    approvals = approvals if approvals is not None else [a for a in session.scalars(select(m.Approval).where(
        m.Approval.status == 'pending').execution_options(populate_existing=True))
        if a.payload.get('fill_request_id') in pending_ids]
    assignments = session.execute(select(m.Assignment.id, m.Assignment.shift_id,
        m.Assignment.volunteer_id, m.Assignment.status).join(m.Shift).where(
        m.Shift.event_id == event.id).order_by(m.Assignment.id)).all()
    return {'event': {'id': event.id, 'title': event.title, 'starts_at': event.starts_at.isoformat(),
                     'ends_at': event.ends_at.isoformat(), 'status': event.status},
            'recipient': {'id': recipient.id, 'name': recipient.name, 'phone': recipient.phone,
                          'is_coordinator': recipient.is_coordinator, 'status': recipient.status,
                          'sms_opt_in': recipient.sms_opt_in},
            'timezone': str(PolicyStore(session).church_tz()), 'staffing': snapshot,
            'assignments': [list(a) for a in assignments],
            'fills': [{'id': f.id, 'shift_id': f.shift_id, 'state': f.state} for f in sorted(fills, key=lambda f: f.id)],
            'approvals': [{'id': a.id, 'fill_request_id': a.payload.get('fill_request_id')}
                          for a in sorted(approvals, key=lambda a: a.id)]}


def pre_event_delivery_problem(session, notification, now, message=None, *, binding=None, body=None):
    if not notification or not notification.key.startswith("pre-event:"):
        return None
    event = session.get(m.Event, notification.event_id, populate_existing=True)
    recipient = session.get(m.Volunteer, notification.volunteer_id, populate_existing=True)
    if not event or event.status != "scheduled" or event.starts_at <= now:
        return "Event is no longer upcoming"
    try:
        saved_start = datetime.fromisoformat(notification.detail.get('event_start', ''))
    except (TypeError, ValueError):
        saved_start = None
    if saved_start != event.starts_at:
        return "Event start changed"
    if not recipient or not recipient.is_coordinator or recipient.status != "active":
        return "Admin recipient changed"
    if not recipient.sms_opt_in:
        return 'Admin no longer consents'
    if binding is not None or message is not None:
        binding = binding if binding is not None else notification.detail.get('pre_event_source')
        if (not isinstance(binding, dict) or binding.get('notification_key') != notification.key
                or binding != notification.detail.get('pre_event_source')):
            return 'Pre-event source binding is missing or changed'
        if binding.get('facts') != pre_event_source(session, notification):
            return 'Pre-event staffing, searches, event or admin changed; fresh review required'
        if message is not None:
            if (notification.state != 'sent' or notification.message_id != message.id or notification.volunteer_id != message.volunteer_id
                    or recipient.phone != message.phone or message.purpose != 'coordinator_notify'):
                return 'Pre-event message is not linked to its source'
            body = message.body
        if not isinstance(body, str) or hashlib.sha256(body.encode()).hexdigest() != binding.get('body_hash'):
            return 'Pre-event rendered body changed'
    return None


def pre_event_approval_problem(session, approval, now, message=None):
    binding = approval.payload.get('pre_event_source')
    # Legacy pre-event proposals lack proof; never treat them as generic admin copy.
    if binding is None and not (approval.payload.get('purpose') == 'coordinator_notify'
                               and 'Pre-event update:' in approval.payload.get('body', '')):
        return None
    row = session.scalar(select(m.Notification).where(m.Notification.key == binding.get('notification_key'))
        .with_for_update().execution_options(populate_existing=True)) if isinstance(binding, dict) else None
    if (not row or not row.key.startswith('pre-event:') or row.purpose != 'coordinator_notify'
            or row.detail.get('approval_id') != approval.id
            or row.volunteer_id != approval.payload.get('volunteer_id')
            or approval.payload.get('purpose') != 'coordinator_notify'):
        return 'Pre-event exact review has no linked source'
    if message is None and (row.state != 'awaiting_approval' or row.message_id is not None):
        return 'Pre-event review source is already consumed or held'
    if message is not None and row.state != 'sent':
        return 'Pre-event delivery source is held or no longer current'
    return pre_event_delivery_problem(session, row, now, message, binding=binding, body=approval.payload['body'])


def invalidate_pre_event_review(session, approval, now, reason, message=None):
    """Preserve old exact copy/hash; only known unsent stale facts may recapture."""
    binding = approval.payload.get('pre_event_source')
    approval.status = 'expired'
    row = session.get(m.Notification, binding.get('notification_key')) if isinstance(binding, dict) else None
    if not row or row.detail.get('approval_id') != approval.id:
        return
    if row.state not in {'awaiting_approval', 'sent'}:
        # An independent hold is not permission to retry or replace its reason.
        return
    from app.integrations.mac_models import MacDeliveryClaim
    uncertain = message is not None and (
        message.status != 'queued' or session.get(MacDeliveryClaim, message.id) is not None)
    if uncertain:
        row.state = 'blocked'
        row.detail = {**row.detail, 'reason': 'Native outcome requires reconciliation; no automatic retry'}
        return
    base_problem = pre_event_delivery_problem(session, row, now)
    recipient = session.get(m.Volunteer, row.volunteer_id)
    opted_out = session.get(m.Policy, 'sms_opt_out:' + recipient.phone) if recipient else None
    row.state = 'expired' if base_problem or (opted_out and opted_out.value.get('value')) else 'pending'
    row.message_id = None
    row.due_at = now
    row.detail = {k: v for k, v in row.detail.items() if k not in {'pre_event_source', 'approval_id', 'conversation_meta'}}
    row.detail = {**row.detail, 'previous_approval_id': approval.id, 'reason': reason}


def pre_event_native_problem(session, message, now, approval=None):
    row = session.scalar(select(m.Notification).where(m.Notification.message_id == message.id,
        m.Notification.key.startswith('pre-event:')).execution_options(populate_existing=True))
    if approval is not None:
        error = pre_event_approval_problem(session, approval, now, message)
    else:
        error = pre_event_delivery_problem(session, row, now, message) if row else (
            'Pre-event message has no linked source' if message.purpose == 'coordinator_notify'
                and 'Pre-event update:' in message.body else None)
    if error and approval is not None:
        from app.core import confirmations
        if approval.status == 'approved' and confirmations.valid(approval, now):
            invalidate_pre_event_review(session, approval, now, error, message)
    return error


def link_pre_event_message(session, approval, message_id):
    binding = approval.payload.get('pre_event_source')
    if binding is None:
        return
    row = session.get(m.Notification, binding['notification_key'])
    row.message_id, row.state = message_id, 'sent'
    coalesce_pre_event_digest(session, row, approval.decided_at)


def coalesce_pre_event_digest(session, row, now):
    digest = session.scalar(select(m.Notification).where(
        m.Notification.key.startswith('staffing:'), m.Notification.event_id == row.event_id,
        m.Notification.volunteer_id == row.volunteer_id))
    if digest:
        digest.state = 'unchanged'
        digest.detail = {'last_sent_at': now.isoformat(), 'last_snapshot': row.detail['pending_snapshot']}


def queue_pre_event_updates(ctx):
    """One durable status check per event start and saved active coordinator."""
    now = ctx.clock.now()
    coordinators = ctx.session.scalars(select(m.Volunteer).where(
        m.Volunteer.is_coordinator, m.Volunteer.status == "active",
        m.Volunteer.sms_opt_in)).all()
    if not coordinators:
        return
    events = ctx.session.scalars(select(m.Event).where(
        m.Event.status == "scheduled", m.Event.starts_at > now,
        m.Event.starts_at <= now + PRE_EVENT_LEAD
    ).with_for_update(skip_locked=True)).all()
    for event in events:
        for coordinator in coordinators:
            event_start = event.starts_at.astimezone(timezone.utc).isoformat()
            key = f"pre-event:{event.id}:{coordinator.id}:{event_start}"
            if ctx.session.get(m.Notification, key) is not None:
                continue
            ctx.session.add(m.Notification(
                key=key, event_id=event.id, volunteer_id=coordinator.id,
                purpose="coordinator_notify", body="", state="pending",
                created_at=now, due_at=now, expires_at=event.starts_at,
                detail={"event_start": event_start}))
    ctx.session.flush()


def deliver(ctx, *, key, body, purpose, volunteer, event_id=None, conversation=None):
    if volunteer is None:
        return None
    row = ctx.session.get(m.Notification, key)
    if row is not None:
        if row.state == "pending" and ctx.reply_to_message_id:
            incoming = ctx.session.get(m.Message, ctx.reply_to_message_id)
            if incoming and incoming.direction == "in" and incoming.volunteer_id == volunteer.id:
                _dispatch(ctx, row)
        return row
    row = m.Notification(key=key, volunteer_id=volunteer.id, event_id=event_id,
                         purpose=purpose, body=body, state="pending",
                         created_at=ctx.clock.now(), due_at=ctx.clock.now(),
                         expires_at=ctx.clock.now() + timedelta(days=2), detail={'conversation': conversation})
    from app.core import outbound_conversation
    meta, error = outbound_conversation.metadata(ctx.session, purpose=purpose, volunteer=volunteer,
        phone=volunteer.phone, now=ctx.clock.now(), supplied=conversation, reply_id=ctx.reply_to_message_id)
    row.detail = {**row.detail, 'conversation_meta': meta}
    if error:
        row.state = 'blocked_policy'
        row.detail = {**row.detail, 'reason': error}
    ctx.session.add(row)
    ctx.session.flush()
    _dispatch(ctx, row)
    return row


def staffing_snapshot(session, event):
    return staffing_snapshots(session, [event])[0]


def staffing_snapshots(session, events):
    """Fetch whole-event staffing in batches, including required missing slots."""
    events = list(events)
    if not events:
        return []
    shifts = session.scalars(select(m.Shift).where(m.Shift.event_id.in_([e.id for e in events]))).all()
    recipes = session.scalars(select(m.RoleRecipe).where(
        m.RoleRecipe.event_type_id.in_({e.event_type_id for e in events if e.event_type_id is not None}))).all()
    role_ids = {s.role_id for s in shifts} | {r.role_id for r in recipes}
    roles = {r.id: r for r in session.scalars(select(m.Role).where(m.Role.id.in_(role_ids)))}
    counts = dict(session.execute(select(m.Assignment.shift_id, func.count()).where(
        m.Assignment.shift_id.in_([s.id for s in shifts]),
        m.Assignment.status.in_(("approved", "confirmed"))).group_by(m.Assignment.shift_id)).all())
    from app.integrations.planning_center_staffing import verified_coverage_counts
    counts = verified_coverage_counts(session, events, counts)
    snapshots = []
    for event in events:
        event_shifts = [s for s in shifts if s.event_id == event.id]
        minima = {r.role_id: r.count for r in recipes if r.event_type_id == event.event_type_id}
        gaps = []
        covered = needed = 0
        for role_id in sorted({s.role_id for s in event_shifts} | set(minima)):
            role = roles[role_id]
            role_shifts = [s for s in event_shifts if s.role_id == role_id]
            count = sum(counts.get(s.id, 0) for s in role_shifts)
            minimum = minima.get(role_id, len(role_shifts) if role.criticality != "optional" else 0)
            needed += minimum
            covered += min(count, minimum)
            if count < minimum:
                gaps.append({"role": role.name, "open": minimum-count})
        snapshots.append({"event_id": str(event.id), "title": event.title,
                          "starts_at": event.starts_at.isoformat(), "covered": covered,
                          "required": needed, "fully_staffed": not gaps, "gaps": gaps})
    return snapshots


def queue_staffing(ctx, event):
    """One pending event summary; each change restarts a five-minute debounce."""
    ctx.session.scalar(select(m.Event).where(m.Event.id == event.id).with_for_update())
    coordinators = ctx.session.scalars(select(m.Volunteer).where(
        m.Volunteer.is_coordinator, m.Volunteer.status == "active", m.Volunteer.sms_opt_in
    ).order_by(m.Volunteer.id)).all()
    for coordinator in coordinators:
        _queue_coordinator_staffing(ctx, event, coordinator)


def _queue_coordinator_staffing(ctx, event, coordinator):
    legacy = ctx.session.get(m.Notification, f"staffing:{event.id}")
    key = (f"staffing:{event.id}" if legacy is None or legacy.volunteer_id == coordinator.id
           else f"staffing:{event.id}:{coordinator.id}")
    row = ctx.session.scalar(select(m.Notification).where(m.Notification.key == key).with_for_update().execution_options(populate_existing=True))
    if row is None:
        row = m.Notification(key=key, event_id=event.id, volunteer_id=coordinator.id,
                             purpose="coordinator_notify", body="", detail={},
                             created_at=ctx.clock.now(), due_at=ctx.clock.now(), state="pending",
                             expires_at=event.starts_at)
        ctx.session.add(row)
    due = ctx.clock.now()+timedelta(minutes=5)
    last = (row.detail or {}).get("last_sent_at")
    if last:
        from datetime import datetime
        due = max(due, datetime.fromisoformat(last)+timedelta(minutes=15))
    row.state = "pending"
    row.due_at = min(due, event.starts_at)
    row.expires_at = event.starts_at+timedelta(minutes=5)
    ctx.session.flush()


def _dispatch(ctx, row):
    # Cancellation scope is a durable internal review hold, never an SMS job.
    if row.state != 'pending' or row.purpose == 'cancellation_scope':
        return
    now = ctx.clock.now()
    if row.expires_at and now >= row.expires_at:
        row.state = "expired"
        return
    body = row.body
    urgent = False
    pre_event = row.key.startswith("pre-event:")
    captured_source = None
    if row.key.startswith("staffing:") or pre_event:
        recent = ctx.session.scalar(select(m.Message).where(
            m.Message.volunteer_id == row.volunteer_id, m.Message.direction == "out",
            m.Message.purpose == "coordinator_notify",
            m.Message.status.in_(("sent", "queued", "dispatching", "submitted", "uncertain")))
            .order_by(m.Message.created_at.desc()).limit(1))
        if not pre_event and recent and recent.created_at+timedelta(minutes=15) > now:
            row.due_at = recent.created_at+timedelta(minutes=15)
            return
        event = ctx.session.get(m.Event, row.event_id)
        if event is None or event.status in ("cancelled", "completed"):
            row.state = "expired"
            return
        if pre_event and pre_event_delivery_problem(ctx.session, row, now):
            row.state = "expired"
            return
        snapshot = staffing_snapshot(ctx.session, event)
        urgent = not snapshot["fully_staffed"] and event.starts_at-now <= timedelta(hours=24)
        fills = ctx.session.scalars(select(m.FillRequest).join(m.Shift).where(m.Shift.event_id == event.id)).all()
        pending_ids = {f.id for f in fills if f.state == "waiting_approval"}
        approvals = [a for a in ctx.session.scalars(select(m.Approval).where(m.Approval.status == "pending"))
                     if a.payload.get("fill_request_id") in pending_ids]
        batches = {}
        for a in approvals:
            batches.setdefault(a.payload["fill_request_id"], a.id)
        attention = len([f for f in fills if f.state == "escalated"])
        if pre_event:
            captured_source = pre_event_source(ctx.session, row, snapshot=snapshot, fills=fills, approvals=approvals)
        signature = {k: snapshot[k] for k in ("covered", "required", "gaps")}
        signature.update(approval_batches=len(batches), attention=attention)
        if not pre_event and (row.detail or {}).get("last_snapshot") == signature:
            row.state = "unchanged"
            return
        when = event.starts_at.astimezone(ctx.gate.policies.church_tz()).strftime("%a %b %-d, %-I:%M%p")
        if snapshot["fully_staffed"]:
            body = f"Fully staffed: {event.title[:100]}, {when}. All {snapshot['required']} required spots are covered. The calendar is updated."
        else:
            gaps = ", ".join(f"{g['role']} ({g['open']})" for g in snapshot["gaps"][:3])
            if len(snapshot["gaps"]) > 3:
                gaps += f", and {len(snapshot["gaps"])-3} more roles"
            body = f"Still needs cover: {event.title[:100]}, {when}. {snapshot['covered']}/{snapshot['required']} required spots covered. Open: {gaps}. Check Text Monkey for search status."
        from app.core.confirmations import enabled
        if enabled(ctx.session) and batches:
            body += f" {len(approvals)} exact invitations await review in Text Monkey; sign in to review each recipient and text."
        elif len(batches) == 1:
            body += f" One restricted-role batch needs approval. Reply YES A{next(iter(batches.values()))} to send, or NO to decline."
        elif batches:
            body += f" {len(batches)} restricted-role batches await review in Text Monkey."
        if attention:
            body += f" {attention} search(es) need your help; review Text Monkey."
        if pre_event:
            active_searches = sum(f.state in ("open", "in_progress", "waiting_quiet") for f in fills)
            if snapshot["required"] == 0:
                body = f"Needs review: {event.title[:100]}, {when}. No required staffing plan is saved, so readiness cannot be confirmed. Add the required roles in Text Monkey."
            elif snapshot["fully_staffed"] and not batches and not attention:
                body = f"All set: {event.title[:100]}, {when}. All {snapshot['required']} required spots are covered. No action needed."
            elif not snapshot["fully_staffed"]:
                # Keep the exact gaps and approval instructions; report real search state.
                body = body.replace("Check Text Monkey for search status.",
                    f"Text Monkey is working on {active_searches} replacement search(es)." if active_searches else
                    "No replacement search is running; review the open spots in Text Monkey.")
                searching_slots = {f.shift_id for f in fills if f.state in ("open", "in_progress", "waiting_quiet")}
                if len(searching_slots) >= sum(g["open"] for g in snapshot["gaps"]) and not batches and not attention:
                    body += " No action needed while those searches continue."
            body = "Pre-event update: " + body
        row.detail = {**(row.detail or {}), "pending_snapshot": signature, "urgent": urgent}
    volunteer = ctx.session.get(m.Volunteer, row.volunteer_id)
    if volunteer is None:
        row.state = "blocked"
        return
    if row.purpose == 'booking_status':
        from app.core import booking_status
        # Preserve this question's session and expiry, but recapture schedule
        # facts for every retry instead of replaying a stored answer.
        meta, error = booking_status.prepare(ctx, row, volunteer, now)
        if error:
            row.state = 'blocked_policy'
            row.detail = {**row.detail, 'reason': error}
            return
        row.detail = {**row.detail, 'conversation_meta': meta}
        try:
            body = booking_status.copy_for(meta['schedule'], ctx.gate.policies.church_tz())
        except GlooUnavailableError:
            row.state = 'blocked'
            row.detail = {**row.detail, 'reason': 'Saved booking facts require review'}
            return
        from app.core.policies import in_quiet_hours, next_send_time
        policies = ctx.gate.policies
        local = now.astimezone(policies.church_tz())
        if in_quiet_hours(local, *policies.quiet_hours()):
            row.due_at = next_send_time(local, *policies.quiet_hours())
            return  # The original ten-minute question expiry still applies.
    if pre_event and (not volunteer.is_coordinator or volunteer.status != "active"):
        row.state = "blocked"
        return
    from app.core.send_gate import has_open_sensitive_escalation, BLOCKING_ESCALATION_STATUSES
    holds = ctx.session.scalars(select(m.Escalation.related_ids).where(
        m.Escalation.category == 'sensitive', m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES)))
    if has_open_sensitive_escalation(ctx.session, volunteer.id) or any(h.get('phone') == volunteer.phone for h in holds):
        row.state = 'blocked'
        row.detail = {**row.detail, 'reason': 'Personal care requires internal human follow-up'}
        return
    from app.core import outbound_conversation
    control = row.purpose in {'stop_confirm', 'start_confirm'}
    admin_check = (row.key.startswith('admin-check:') and row.purpose == 'coordinator_notify' and
        getattr(ctx.provider, 'transport_name', '') == 'google_voice')
    if admin_check:
        from app.core.admin_check_copy import binding, copy_for
        selected = getattr(ctx.provider, 'test_sessions', {}).get(volunteer.phone)
        ctx.session.info['mac_test_session'] = selected
        source = binding(ctx.session, volunteer, selected, row.key, now)
        if source is None:
            row.state = 'blocked_policy'
            return
        body = copy_for(source)
        row.detail = {**row.detail, 'conversation': {'admin_check': row.key},
            'conversation_meta': {'admin_check': source}}
    if control:
        row.detail = {**row.detail, 'conversation': {'control_key': row.key}}
    meta = row.detail.get('conversation_meta')
    if meta is None:
        meta, error = outbound_conversation.metadata(ctx.session, purpose=row.purpose, volunteer=volunteer,
            phone=volunteer.phone, now=now, supplied=row.detail.get('conversation'), reply_id=ctx.reply_to_message_id)
        row.detail = {**row.detail, 'conversation_meta': meta}
    else:
        error = None
    error = error or (None if control else outbound_conversation.problem(ctx.session, purpose=row.purpose, volunteer=volunteer,
        phone=volunteer.phone, body=body, now=now, meta=meta))
    if error:
        row.state = 'blocked_policy'
        row.detail = {**row.detail, 'reason': error}
        return
    # Gloo writes within application facts; code alone decides staffing/assignment.
    try:
        # Preserve exact approved status/counts/codes; Gloo may adjust surrounding tone.
        required = (body,)
        if row.purpose in {'confirmation', 'booking_status'}:
            from app.core import schedule_messages
            if row.purpose == 'confirmation':
                assignment = ctx.session.get(m.Assignment, meta['assignment_id'])
                body = schedule_messages.confirmation_copy(assignment, ctx.gate.policies.church_tz())
                context = {'assignment': meta['source']}
                incoming = ctx.session.get(m.Message, ctx.reply_to_message_id) if ctx.reply_to_message_id else None
                from app.core.privacy import safe_message_history
                if incoming and incoming.direction == 'in' and incoming.phone == volunteer.phone and incoming.volunteer_id == volunteer.id and safe_message_history(ctx.session, [incoming]):
                    context['question'] = incoming.body
            else:
                context = {'schedule': meta['schedule'], 'question': meta['question']}
            rendered = schedule_messages.compose(ctx.session, ctx.clock, ctx.gloo, body, volunteer, context)
        else:
            rendered = compose_signup_reply(ctx.session, ctx.clock, ctx.gloo, body, required,
                                            volunteer=volunteer, require_gloo=True, exact_copy=control or admin_check)
    except GlooUnavailableError:
        attempts = row.detail.get("gloo_attempts", 0)+1
        row.detail = {**row.detail, "gloo_attempts": attempts}
        row.due_at = now+timedelta(minutes=2)
        if attempts >= 3:
            row.state = "blocked"
            ctx.session.add(m.Escalation(category="system_error", severity="normal",
                summary="A saved notification needs review because Gloo could not compose it.",
                related_ids={"notification_key": row.key}, status="open", created_at=now))
        return
    if control:
        row.detail = {**row.detail, 'gloo_body_hash': hashlib.sha256(rendered.encode()).hexdigest()}
    if row.purpose in {'confirmation', 'booking_status'}:
        ctx.session.expire_all()
        volunteer = ctx.session.get(m.Volunteer, row.volunteer_id)
        if volunteer is None:
            row.state = 'blocked_policy'
            row.detail = {**row.detail, 'reason': 'Schedule recipient no longer exists'}
            return
        if row.purpose == 'booking_status':
            fresh, error = booking_status.prepare(ctx, row, volunteer, ctx.clock.now())
            if error:
                row.state = 'blocked_policy'
                row.detail = {**row.detail, 'reason': error}
                return
            if fresh != meta:
                row.detail = {**row.detail, 'reason': 'Schedule changed during Gloo composition'}
                row.due_at = ctx.clock.now() + timedelta(minutes=2)
                return  # Next attempt recaptures facts; never queues stale copy.
        else:
            try:
                current_body = schedule_messages.confirmation_copy(ctx.session.get(m.Assignment, meta['assignment_id']), ctx.gate.policies.church_tz())
            except GlooUnavailableError:
                current_body = None
            if body != current_body:
                row.state = 'blocked_policy'
                row.detail = {**row.detail, 'reason': 'Schedule confirmation changed during composition'}
                return
    if error := outbound_conversation.problem(ctx.session, purpose=row.purpose, volunteer=volunteer,
            phone=volunteer.phone, body=rendered, now=ctx.clock.now(), meta=meta):
        row.state = 'blocked_policy'
        row.detail = {**row.detail, 'reason': error}
        return
    if pre_event:
        if (pre_event_delivery_problem(ctx.session, row, ctx.clock.now())
                or captured_source != pre_event_source(ctx.session, row)):
            row.due_at = ctx.clock.now()+timedelta(minutes=2)
            row.detail = {**row.detail, 'reason': 'Pre-event facts changed during composition; recapture required'}
            return
        row.detail = {**row.detail, 'pre_event_source': {'notification_key': row.key,
            'facts': captured_source, 'body_hash': hashlib.sha256(rendered.encode()).hexdigest()}}
    gate = ctx.gate
    if row.purpose == 'booking_status':
        gate.reply_to_message_id = row.detail['reply_id']
    result = gate.send(body=rendered, purpose=row.purpose, volunteer=volunteer, kind="ai", urgent=urgent,
                          conversation=row.detail.get('conversation'))
    row.body = body
    if (control or row.purpose in {'confirmation', 'booking_status', 'coordinator_notify'}) and result.status == SendStatus.HELD_FOR_APPROVAL:
        row.state = 'awaiting_approval'
        row.detail = {**row.detail, 'approval_id': result.approval_id}
        if pre_event:
            from app.core import confirmations
            proposal = ctx.session.get(m.Approval, result.approval_id)
            payload = {**proposal.payload, 'pre_event_source': row.detail['pre_event_source']}
            payload['content_hash'] = confirmations.digest(payload)
            proposal.payload = payload
    elif result.status == SendStatus.HELD_QUIET_HOURS:
        row.due_at = result.retry_at
        row.state = "pending"
    elif result.sent:
        row.message_id = result.message_id
        row.state = "sent"
        if row.key.startswith("staffing:"):
            row.detail = {"last_sent_at": now.isoformat(),
                          "last_snapshot": row.detail["pending_snapshot"], "urgent": urgent}
        elif pre_event:
            # Fold an outstanding change digest into this update to avoid duplicate texts.
            coalesce_pre_event_digest(ctx.session, row, now)
    else:
        row.state = 'blocked_policy' if result.status == SendStatus.BLOCKED_POLICY else 'blocked'
        row.detail = {**row.detail, "reason": result.reason}


def flush_due(ctx):
    rows = ctx.session.scalars(select(m.Notification).where(
        m.Notification.state == "pending", m.Notification.due_at <= ctx.clock.now(),
        m.Notification.purpose != 'cancellation_scope'
    ).order_by(m.Notification.due_at).with_for_update(skip_locked=True)).all()
    for row in rows:
        _dispatch(ctx, row)
    return len(rows)
