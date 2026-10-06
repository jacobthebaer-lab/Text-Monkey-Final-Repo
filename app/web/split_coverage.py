"""Signed-in reviewed local split slots. Never sends or provisions transport."""
from pathlib import Path
import threading
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core import split_coverage as split
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.web.admin_setup import owner
from app.web.planning_workflows import body
from app.web.texty import admin

router = APIRouter(tags=['Reviewed partial coverage'])
_lock = threading.RLock()


def envelope(review):
    return {'id': review.id, 'kind': review.kind, 'status': review.status,
        'content_hash': review.payload['content_hash'], 'scope': review.payload,
        'sent': 0, 'delivery_enabled': False}


def configure_scope(session, state):
    session.info['split_sessions'] = getattr(state.provider, 'test_sessions', {})
    session.info['confirmation_now'] = state.clock.now()


def transaction(request, operation):
    with _lock, request.app.state.session_factory() as session:
        configure_scope(session, request.app.state)
        try:
            result = operation(session, request.app.state.clock.now())
            session.commit()
            return result
        except ValueError as error:
            raise HTTPException(409, str(error)) from error


@router.get('/split-coverage.js', include_in_schema=False)
def asset():
    return FileResponse(Path(__file__).resolve().parents[2]/'web/texty/public/split-coverage.js')


@router.get('/api/split-coverage')
async def context(request: Request, user=Depends(admin)):
    identity = owner(user)
    def operation(session, now):
        roles = session.scalars(select(m.Role).order_by(m.Role.name)).all()
        parents = session.scalars(select(m.Shift).join(m.Event).where(m.Shift.parent_shift_id.is_(None),
            m.Event.status == 'scheduled', m.Event.ends_at > now).order_by(m.Event.starts_at).limit(200)).all()
        evidence = []
        for parent in parents:
            if split.partition_problem(session, parent, now):
                continue
            for outreach in session.scalars(select(m.Outreach).join(m.FillRequest).where(
                    m.FillRequest.shift_id == parent.id, m.Outreach.response.in_(('partial', 'expired')))):
                for incoming in session.scalars(select(m.Message).where(m.Message.direction == 'in',
                        m.Message.volunteer_id == outreach.volunteer_id).order_by(m.Message.id.desc()).limit(1)):
                    try:
                        facts = split.partial_facts(session, parent.id, outreach.id, incoming.id, now)
                    except ValueError:
                        continue
                    evidence.append({'parent_id':parent.id, 'outreach_id':outreach.id, 'incoming_id':incoming.id,
                        'role':parent.role.name, 'event':parent.event.title, 'start':parent.starts_at.isoformat(),
                        'end':parent.ends_at.isoformat(), 'actual_reply':facts['body']})
        reviews = session.scalars(select(m.Approval).where(m.Approval.kind.in_((split.PARTITION, split.BOOKING)),
            m.Approval.payload['owner'].as_string() == identity).order_by(m.Approval.id.desc()).limit(50)).all()
        return {'roles':[{'id':r.id, 'name':r.name, 'allowed':split.role_enabled(session,r.id)} for r in roles],
            'evidence':evidence, 'reviews':[envelope(r) for r in reviews],
            'coverage':[value for p in parents if (value := split.coverage(session,p)) is not None],
            'delivery_enabled':False}
    return await run_in_threadpool(transaction, request, operation)


@router.post('/api/split-coverage/roles/{role_id}')
async def role_option(request: Request, role_id: int, user=Depends(admin)):
    identity = owner(user)
    data = await body(request, {'allowed'})
    if type(data.get('allowed')) is not bool:
        raise HTTPException(422, 'Choose whether this role may use reviewed split coverage.')
    def operation(session, now):
        role = session.get(m.Role, role_id)
        if role is None:
            raise HTTPException(404, 'Role not found.')
        row = session.get(m.Policy, f'split_role:{role_id}')
        if row is None:
            row = m.Policy(key=f'split_role:{role_id}', value={'value':data['allowed']}); session.add(row)
        else:
            row.value = {'value':data['allowed']}
        session.add(m.Notification(key='split_role_review:'+str(uuid4()), purpose='human_review',
            body='', state='sent', created_at=now, due_at=now,
            detail={'owner':identity,'role_id':role_id,'allowed':data['allowed']}))
        return {'role_id':role_id, 'allowed':data['allowed'], 'sent':0}
    return await run_in_threadpool(transaction, request, operation)


@router.post('/api/split-coverage/partitions')
async def prepare_partition(request: Request, user=Depends(admin)):
    identity = owner(user)
    data = await body(request, {'parent_id','outreach_id','incoming_id','request_id'})
    if any(type(data.get(k)) is not int or data[k] <= 0 for k in ('parent_id','outreach_id','incoming_id')):
        raise HTTPException(422, 'Choose an actual saved partial reply and its original slot.')
    try:
        request_id = str(UUID(data['request_id']))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, 'A stable request ID is required.') from None
    key = 'split_request:'+split.fingerprint({'owner':identity,'request_id':request_id})
    def operation():
        with _lock:
            state = request.app.state
            with state.session_factory() as session:
                configure_scope(session, state)
                previous = session.get(m.Policy, key)
                if previous:
                    if previous.value['source_ids'] != [data[k] for k in ('parent_id','outreach_id','incoming_id')]:
                        raise HTTPException(409,'This request ID belongs to a different actual reply.')
                    return envelope(session.get(m.Approval,previous.value['review_id']))
                try:
                    facts = split.partial_facts(session,data['parent_id'],data['outreach_id'],data['incoming_id'],state.clock.now())
                except ValueError as error:
                    raise HTTPException(409,str(error)) from error
                session.rollback()  # No database write lock during Gloo interpretation.
                try:
                    normalized = split.extract_partial(state.gloo,facts)
                except GlooUnavailableError as error:
                    raise HTTPException(503,'Gloo is unavailable; the split proposal remains held.') from error
                except ValueError as error:
                    raise HTTPException(409,str(error)) from error
                try:
                    review = split.stage_partition(session,identity,facts,normalized,state.clock.now())
                except ValueError as error:
                    raise HTTPException(409,str(error)) from error
                session.add(m.Policy(key=key,value={'review_id':review.id,
                    'source_ids':[data[k] for k in ('parent_id','outreach_id','incoming_id')]}))
                session.commit()
                return envelope(review)
    return await run_in_threadpool(operation)


@router.post('/api/split-coverage/{parent_id}/booking-review')
async def prepare_booking(request: Request,parent_id:int,user=Depends(admin)):
    identity = owner(user)
    await body(request,set())
    return await run_in_threadpool(transaction,request,lambda s,n:envelope(split.stage_booking(s,identity,parent_id,n)))


@router.post('/api/split-coverage/reviews/{review_id}/{decision}')
async def decide_review(request:Request,review_id:int,decision:str,user=Depends(admin)):
    identity = owner(user)
    if decision not in ('approve','reject'):
        raise HTTPException(404)
    data = await body(request,{'content_hash'})
    if type(data.get('content_hash')) is not str or not data['content_hash']:
        raise HTTPException(422,'Review the exact displayed intervals before deciding.')
    return await run_in_threadpool(transaction,request,lambda s,n:envelope(
        split.decide(s,review_id,identity,data['content_hash'],decision=='approve',n)))


@router.post('/api/split-coverage/{parent_id}/outreach')
async def start_asks(request: Request, parent_id: int, user=Depends(admin)):
    identity=owner(user)
    data=await body(request,{'content_hash'})
    return await run_in_threadpool(transaction,request,
        lambda session,now:split.start_outreach(session,parent_id,identity,data.get('content_hash'),now))
