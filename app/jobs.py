"""Background job definitions.

Phase 4: the fill-request tick. APScheduler wiring (a BackgroundScheduler
calling these every minute on the real clock) lands with the web app in
Phase 5; tests and demo fast-forward call them directly with the fake clock.
"""

from app.agents import fill_agent


def process_pco_blockouts(session_factory, settings, config, clock, *, client_factory=None, limit=10):
    """Drain committed local availability intent without running text jobs.

    Each executor call has its own fresh guarded source transactions. The
    durable rotation cursor lets held people coexist with runnable people;
    retries/restarts never replay signup, composition or text delivery.
    """
    from sqlalchemy import select
    from app.db import models as m
    from app.core.planning_center_blockout_sources import CommittedBlockoutReader, blockout_source_factory
    from app.core.planning_center_held_preview import signing_key
    from app.integrations.planning_center import PCOClient, PlanningCenterError
    from app.integrations.planning_center_availability import _hash
    from app.integrations.planning_center_blockouts import (
        QUEUE_PREFIX, load_blockout_acceptance, sync_person_blockouts,
    )

    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Blockout worker limit must be between 1 and 20')
    enabled = getattr(settings, 'pco_blockout_write_enabled', False) is True
    key = signing_key(getattr(settings, 'pco_blockout_signing_key_path', ''))
    if key is None:
        return {'state': 'held' if enabled else 'disabled', 'reason': 'blockout_signing_key_required', 'processed': []}
    try:
        config.require_scope()
    except PlanningCenterError:
        return {'state': 'held', 'reason': 'blockout_worker_scope_required', 'processed': []}
    try:
        acceptance = load_blockout_acceptance(getattr(settings, 'pco_blockout_acceptance_path', ''), config, key)
    except PlanningCenterError:
        acceptance = None  # Missing evidence holds new writes, but not unknown recovery.
    factory = blockout_source_factory(session_factory.kw['bind'])
    cursor_key = 'pco_bcursor:' + _hash(config.organization_id)
    # Bounded queries rather than loading every pending person's source.
    with factory() as session:
        cursor = session.get(m.Policy, cursor_key)
        after = (cursor.value or {}).get('after', '') if cursor else ''
        query = select(m.Policy).where(m.Policy.key.startswith(QUEUE_PREFIX),
            m.Policy.value['organization_id'].as_string() == config.organization_id,
            m.Policy.value['state'].as_string() != 'verified').order_by(m.Policy.key)
        rows = list(session.scalars(query.where(m.Policy.key > after).limit(limit)))
        if len(rows) < limit:
            rows.extend(session.scalars(query.where(m.Policy.key <= after).limit(limit - len(rows))))
        candidates = [(row.key, row.value.get('volunteer_id')) for row in rows]
    if not candidates:
        return {'state': 'idle' if enabled else 'disabled', 'processed': []}
    outcomes = []
    with (client_factory or PCOClient)(config) as client:
        for queue_id, volunteer_id in candidates:
            if type(volunteer_id) is not int or volunteer_id <= 0:
                outcomes.append({'state': 'held', 'reason': 'blockout_queue_scope_or_revision_invalid'})
                continue
            reader = CommittedBlockoutReader(settings, config, volunteer_id=volunteer_id, clock=clock.now)
            result = sync_person_blockouts(factory, client, config, volunteer_id,
                source_reader=reader, clock=clock.now, enabled=enabled, signing_key=key,
                reconcile_only=not enabled, limit=10, acceptance=acceptance)
            outcomes.append({'volunteer_id': volunteer_id, **result})
    with factory() as session:
        cursor = session.get(m.Policy, cursor_key)
        if cursor is None:
            cursor = m.Policy(key=cursor_key)
            session.add(cursor)
        cursor.value = {'after': candidates[-1][0]}
        session.commit()
    return {'state': 'processed', 'processed': outcomes}


def process_pco_staffing(session_factory, settings, config, clock, *, client_factory=None):
    """PCO-only durable worker; never enables general automation or SMS delivery."""
    from app.integrations.planning_center_staffing import staffing_tick
    kwargs = {} if client_factory is None else {"client_factory": client_factory}
    from app.integrations.planning_center_sync import sync_metadata_tick
    metadata = sync_metadata_tick(session_factory, settings, config, clock.now(), **kwargs)
    result = staffing_tick(session_factory, settings, config, clock.now(), **kwargs)
    if settings.pco_sync_enabled:
        result['sync'] = metadata
    return result


def process_due_fill_requests(ctx: fill_agent.FillContext) -> list:
    """Advance every fill request whose tranche timer has expired."""
    from datetime import datetime
    from sqlalchemy import select
    from app.db import models as m
    from app.core.offer_windows import begin_decision
    begin_decision(ctx.session)
    for approval in ctx.session.scalars(select(m.Approval).where(m.Approval.status == "approved").with_for_update(skip_locked=True)):
        retry = approval.payload.get("retry_at")
        if not retry or datetime.fromisoformat(retry) > ctx.clock.now():
            continue
        fill = ctx.session.get(m.FillRequest, approval.payload.get("fill_request_id"))
        if not fill or fill.state != "waiting_approval":
            approval.status = "expired"
            continue
        shift = ctx.session.get(m.Shift, fill.shift_id)
        if shift.starts_at <= ctx.clock.now():
            approval.status = "expired"
            fill.state, fill.next_action_at = "in_progress", ctx.clock.now()
            continue
        result = ctx.gate.send_approved(approval)
        payload = {k: v for k, v in approval.payload.items() if k != "retry_at"}
        if result.retry_at:
            payload["retry_at"] = result.retry_at.isoformat()
        elif result.sent:
            outreach = ctx.session.get(m.Outreach, payload.get("outreach_id"))
            if outreach:
                outreach.message_id = result.message_id
            fill_agent.on_outreach_approved(ctx, fill.id)
        else:
            fill.state, fill.next_action_at = "in_progress", ctx.clock.now()
        approval.payload = payload
    outcomes = fill_agent.advance_due(ctx)
    from app.core import replacement_retry
    outcomes.extend(replacement_retry.advance_due(ctx))
    from app.core.notifications import flush_due, queue_pre_event_updates
    queue_pre_event_updates(ctx)
    flush_due(ctx)
    return outcomes


from datetime import timedelta
from sqlalchemy import select
from app.db import models as m

def process_jobs(ctx, calendar=False):
    from app.core.reminders import process
    from app.agents.planning_agent import request_collection, collect, plan_month
    from app.agents.capacity_agent import scan
    # Connected reminders now compose through Gloo and stage source-bound exact
    # reviews. Collection/planning still require an authorized parent action;
    # unrelated legacy controls retain their separate guard.
    from app.sms.mock_provider import MockSMSProvider
    if not isinstance(ctx.provider, MockSMSProvider):
        result = {"fills": process_due_fill_requests(ctx)}
        from app.core.confirmations import MODE_KEY
        missing = object()
        previous = ctx.session.info.get(MODE_KEY, missing)
        ctx.session.info[MODE_KEY] = True
        try:
            result["messages"] = process(ctx)
        finally:
            if previous is missing:
                ctx.session.info.pop(MODE_KEY, None)
            else:
                ctx.session.info[MODE_KEY] = previous
        result["collection_and_planning"] = "held_for_authorized_parent_approval"
        result["legacy_controls"] = "held_for_connected_review"
        return result
    now=ctx.clock.now();result={"fills":process_due_fill_requests(ctx),"messages":process(ctx)}
    week=f"job:capacity:{now:%G-%V}"
    if not ctx.session.get(m.Policy,week):
        result["flags"]=len(scan(ctx));ctx.session.add(m.Policy(key=week,value={"done":True}))
    next_month=(now.replace(day=1)+timedelta(days=32)).strftime("%Y-%m")
    if now.day>=15:request_collection(ctx,next_month)
    for a in list(ctx.session.scalars(select(m.Approval).where(m.Approval.kind=="collect_availability",m.Approval.status=="approved"))):
        collect(ctx,a)
        if a.decided_at and now>=a.decided_at+timedelta(days=3):
            collect(ctx,a,reminder=True)
            key=f"job:plan:{a.id}"
            if not ctx.session.get(m.Policy,key):
                result["plan"]=plan_month(ctx,a.payload["month"])
                plan = result["plan"]
                if (plan.get("state") == "pending_exact_review" or
                        (plan.get("gloo_review_outcome") == "completed" and not plan.get("violations"))):
                    ctx.session.add(m.Policy(key=key,value={"done":True}))
    if calendar:
        from app.integrations.gcal import sync
        result["calendar"]=sync(ctx)
    ctx.session.flush();return result
