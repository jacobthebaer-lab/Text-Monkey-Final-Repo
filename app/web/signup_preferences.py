"""Signed-in saved signup draft review; stages only, never sends or schedules."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, or_, false
from app.db import models as m
from app.core import signup_preference_review as review
from app.web.routes import db
from app.web.texty import admin

router=APIRouter(prefix='/api/signup-preferences')


def review_scope(provider):
    """Select only internal record reviews for the unchanged original epoch."""
    if getattr(provider,'transport_name',None)!='mac_messages':return false()
    source=m.Approval.payload[review.KEY]['source']
    specs=[(m.Approval.payload['phone'].as_string()==phone)
        & (m.Approval.payload['session_id'].as_string()==selected.id)
        & (source['original_session']['id'].as_string()==selected.id)
        & (source['original_session']['starts_at'].as_string()==selected.starts_at.isoformat())
        & (source['original_session']['expires_at'].as_string()==selected.expires_at.isoformat())
        for phone,selected in provider.test_sessions.items()]
    return (m.Approval.kind=='confirm_record') & (m.Approval.payload['transport'].as_string()=='mac_messages') & or_(*specs) if specs else false()


def scoped(session, state, person):
    if not person or getattr(state.provider,'transport_name',None)!='mac_messages':
        raise ValueError('Saved preference review requires the selected Mac participant.')
    selected=state.provider.test_sessions.get(person.phone)
    if not selected:
        raise ValueError('The original participant session is unavailable.')
    session.info.update(mac_test_session=selected,conversation_origin='mac_messages')


@router.get('')
def drafts(request:Request,user=Depends(admin),session=Depends(db)):
    result=[];state=request.app.state
    for person in session.scalars(select(m.Volunteer).where(m.Volunteer.phone.in_(getattr(state.provider,'test_sessions',{})))):
        if not person.preferences.get('onboarding_availability_draft'):continue
        try:
            scoped(session,state,person)
            result.append(review.card(session,person,state.mac_delivery_clock.now()))
        except ValueError:
            continue
    return {'drafts':result,'texts_sent':0,'assignments_created':0}


@router.post('/{volunteer_id}/review')
async def proposal(request:Request,volunteer_id:int,user=Depends(admin),session=Depends(db)):
    import json
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>16384:raise HTTPException(413,'Saved preference request is too large.')
    try:data=json.loads(raw)
    except (ValueError,UnicodeDecodeError):raise HTTPException(422,'Use JSON preference review controls.') from None
    if not isinstance(data,dict) or set(data)!=review.FIELDS:
        raise HTTPException(422,'Use the displayed preference review controls.')
    person=session.get(m.Volunteer,volunteer_id)
    try:
        scoped(session,request.app.state,person)
        approval=review.stage(session,person,data,request.app.state.mac_delivery_clock.now())
    except (ValueError,TypeError,KeyError) as error:
        raise HTTPException(409,str(error)) from None
    return {'approval_id':approval.id,'content_hash':approval.payload['content_hash'],
        'state':'pending_exact_review','texts_sent':0,'assignments_created':0}
