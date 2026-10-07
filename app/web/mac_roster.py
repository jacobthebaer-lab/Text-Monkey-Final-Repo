"""Connector-only two-phase roster scope, explicitly enabled by an operator policy."""
from datetime import datetime
from uuid import uuid4
from fastapi import APIRouter,Depends,HTTPException,Request
from pydantic import BaseModel,Field,ConfigDict
from sqlalchemy import select
from app.db import models as m
from app.core.offer_windows import begin_decision
from app.integrations import mac_roster as r
from app.integrations.mac_ongoing import verify,_digest
from app.web.mac_messages import authorized,claim_lock

router=APIRouter(prefix='/mac/roster',dependencies=[Depends(authorized)])


class Poll(BaseModel):
    model_config=ConfigDict(extra='forbid')
    journal_id:str=Field(pattern=r'^[0-9a-f]{32}$')


class Intent(BaseModel):
    model_config=ConfigDict(extra='forbid')
    intent_id:str=Field(pattern=r'^[0-9a-f]{32}$')


class Prepare(Intent):
    journal:dict


class Adopted(Intent):
    journal_id:str=Field(pattern=r'^[0-9a-f]{32}$')
    configuration_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    checkpoint_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')


def checked(session,state,intent_id):
    value=r.intent(session,intent_id)
    if not value: raise HTTPException(409,'Unknown roster enrollment intent')
    current=r.accepted(session,state)
    if value['phase']=='committed':
        if value['journal_id']!=current['journal_id']: raise HTTPException(409,'Stale enrollment acknowledgment')
        return value,current
    if value['phase']=='cancelled': return value,current
    if value['base_journal_id']!=current['journal_id']:
        raise HTTPException(409,'Enrollment base revision changed')
    pending=session.get(m.Policy,r.PENDING)
    if not pending or pending.value.get('intent_id')!=intent_id:
        raise HTTPException(409,'Another enrollment owns this base revision')
    return value,current


def still_eligible(session,value):
    person=session.scalar(select(m.Volunteer).where(m.Volunteer.id==value['volunteer_id']).with_for_update())
    session.scalar(select(m.Policy).where(m.Policy.key==r.POLICY).with_for_update())
    permission=r.policy(session)
    return bool(permission and permission['actor'].strip()==value['actor'] and r.eligible(session,person)
                and person.phone==value['phone'] and person.name==value['name'])


@router.post('/poll')
def poll(data:Poll,request:Request):
    state=request.app.state
    with claim_lock,state.session_factory() as session:
        begin_decision(session)
        permission=r.policy(session)
        if not permission: return {'status':'disabled'}
        try: current=r.accepted(session,state)
        except (ValueError,KeyError,TypeError): return {'status':'held','reason':'An approved ongoing Mac route is required.'}
        if data.journal_id!=current['journal_id']: raise HTTPException(409,'Worker scope revision changed')
        if current['route']['input_mode']!='natural': return {'status':'held','reason':'The ongoing natural route is required.'}
        if r.unsettled(session): return {'status':'held','reason':'Native claims require reconciliation.'}
        pending=session.get(m.Policy,r.PENDING)
        if pending:
            value=r.intent(session,pending.value['intent_id'])
            if value and value['phase']=='prepared': return {'status':'held','reason':'A staged enrollment must be recovered.'}
            if value and value['phase']=='offered' and still_eligible(session,value):
                return {'status':'offer',**value}
            if value: r.cancel(session,value,'Roster eligibility changed.')
            else: session.delete(pending)
            session.flush()
        people=session.scalars(select(m.Volunteer).where(m.Volunteer.status=='active',m.Volunteer.sms_opt_in.is_(True)).order_by(m.Volunteer.id)).all()
        person=next((p for p in people if p.phone not in current['sessions'] and r.eligible(session,p)),None)
        if person is None:
            session.commit();return {'status':'idle'}
        value={'intent_id':uuid4().hex,'phase':'offered','base_journal_id':current['journal_id'],
               'volunteer_id':person.id,'phone':person.phone,'name':person.name,'actor':permission['actor'].strip(),
               'offered_at':state.mac_delivery_clock.now().isoformat()}
        if not session.get(m.Policy,r.SCOPE): r.put(session,r.SCOPE,{'journal':current,'intent_id':None})
        r.put(session,r.INTENT+value['intent_id'],value);r.put(session,r.PENDING,{'intent_id':value['intent_id']})
        session.commit();return {'status':'offer',**value}


@router.post('/status')
def status(data:Intent,request:Request):
    with claim_lock,request.app.state.session_factory() as session:
        begin_decision(session)
        value,current=checked(session,request.app.state,data.intent_id)
        return r.receipt(value)


@router.post('/prepare')
def prepare(data:Prepare,request:Request):
    state=request.app.state
    with claim_lock,state.session_factory() as session:
        begin_decision(session)
        value,current=checked(session,state,data.intent_id)
        if value['phase']=='cancelled': return r.receipt(value)
        try: proposed=verify(data.journal,state.settings.mac_bridge_token)
        except (ValueError,TypeError,KeyError,RecursionError): raise HTTPException(409,'Invalid signed roster revision')
        if value['phase'] in {'prepared','committed'}:
            if _digest(proposed)!=_digest(value['journal']): raise HTTPException(409,'Competing enrollment proposal')
            return r.receipt(value)
        enrollment=proposed.get('enrollment',{})
        offered=datetime.fromisoformat(value['offered_at'])
        approved=datetime.fromisoformat(proposed['approved_at'])
        if (proposed.get('version')!=2 or _digest(proposed.get('previous_authorization'))!=_digest(current)
                or proposed['actor']!=value['actor'] or enrollment.get('phone')!=value['phone']
                or enrollment.get('name')!=value['name'] or not offered<=approved<=state.mac_delivery_clock.now()):
            raise HTTPException(409,'Proposal does not match the current authorized roster offer')
        if not still_eligible(session,value):
            value=r.cancel(session,value,'Roster eligibility changed.');session.commit();return r.receipt(value)
        if r.unsettled(session): return {'intent_id':data.intent_id,'phase':'held','reason':'Native claims require reconciliation.'}
        value={**value,'phase':'prepared','journal':proposed,'journal_id':proposed['journal_id']}
        r.put(session,r.INTENT+data.intent_id,value);session.commit();return r.receipt(value)


@router.post('/adopted')
def adopted(data:Adopted,request:Request):
    state=request.app.state
    with claim_lock,state.session_factory() as session:
        begin_decision(session)
        value,current=checked(session,state,data.intent_id)
        if value['phase']=='cancelled': return r.receipt(value)
        proposed=value.get('journal')
        if (not proposed or data.journal_id!=proposed['journal_id']
                or data.configuration_sha256!=proposed['configuration_sha256']
                or data.checkpoint_sha256==proposed['checkpoint_sha256']):
            raise HTTPException(409,'Worker adoption does not match the staged revision')
        if value['phase']=='committed':
            if data.checkpoint_sha256!=value['adopted_checkpoint_sha256']: raise HTTPException(409,'Adopted checkpoint changed')
            return r.receipt(value)
        if value['phase']!='prepared': raise HTTPException(409,'Enrollment has not been prepared')
        if not still_eligible(session,value):
            value=r.cancel(session,value,'Consent or roster authorization changed before activation.')
            session.commit();return r.receipt(value)
        if r.unsettled(session): return {'intent_id':data.intent_id,'phase':'held','reason':'Native claims require reconciliation.'}
        value={**value,'phase':'committed','adopted_checkpoint_sha256':data.checkpoint_sha256}
        r.put(session,r.INTENT+data.intent_id,value);r.put(session,r.SCOPE,{'journal':proposed,'intent_id':data.intent_id})
        selected=proposed['sessions'][value['phone']]
        r.put(session,'conversational_signup:'+value['phone'],{'value':True,'session_id':selected['id'],
            'enrollment_intent_id':data.intent_id})
        pending=session.get(m.Policy,r.PENDING)
        session.delete(pending);session.commit()
        r.install(state,proposed)
        return r.receipt(value)


class Ledger(Poll):
    checkpoint_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    dispatches:dict


@router.post('/ledger')
def reconcile_ledger(data:Ledger,request:Request):
    from app.integrations.mac_models import MacDeliveryClaim
    state=request.app.state
    with claim_lock,state.session_factory() as session:
        begin_decision(session)
        if not r.policy(session): return {'settled':False}
        if r.accepted(session,state)['journal_id']!=data.journal_id: raise HTTPException(409,'Worker scope revision changed')
        if r.unsettled(session): return {'settled':False}
        claims={str(claim.message_id):(claim,row) for claim,row in session.execute(
            select(MacDeliveryClaim,m.Message).join(m.Message,m.Message.id==MacDeliveryClaim.message_id))}
        if set(claims)!=set(data.dispatches): return {'settled':False}
        for key,(claim,row) in claims.items():
            entry=data.dispatches[key]
            if (not isinstance(entry,dict) or entry.get('token')!=claim.token
                    or (entry.get('outcome')=='submitted' and row.status!='submitted')
                    or (entry.get('outcome')=='blocked' and not row.status.startswith('blocked'))
                    or entry.get('outcome') not in {'submitted','blocked'}):
                return {'settled':False}
        return {'settled':True,'journal_id':data.journal_id,'checkpoint_sha256':data.checkpoint_sha256,
                'dispatches_sha256':_digest(data.dispatches)}
