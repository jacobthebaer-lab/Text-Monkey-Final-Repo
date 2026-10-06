"""One explicitly approved Mac ongoing transition, never an automatic renewal."""
import hashlib
import hmac
import json
from copy import deepcopy
from datetime import datetime,timezone
from uuid import uuid4
from app.integrations.test_sessions import parse_sessions


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _sign(value,token):
    return hmac.new(token.encode(),json.dumps(value,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()


def route(config):
    return {key:config.get(key,default) for key,default in [('backend_url',''),('phones',[]),('receiving_number',''),
        ('services',['iMessage']),('input_mode','marked'),('competition_confirmation_required',False)]}


def authorize(config, checkpoint_bytes, *, phone, guid, row_id, body_hash, received_at, actor, operator_confirmed, now):
    """Trusted operator only. Returns config/journal; does not write any file/DB."""
    if operator_confirmed is not True or not isinstance(actor,str) or not actor.strip():raise ValueError('Explicit operator approval required')
    if config.get('phones')!=[phone] or config.get('input_mode')!='natural':raise ValueError('One exact natural Mac participant required')
    original=parse_sessions(config['test_sessions'],{phone})[phone]
    state=json.loads(checkpoint_bytes)
    if (state.get('phones')!=[phone] or state.get('test_sessions')!={phone:original.spec()}
            or state.get('receiving_number')!=config.get('receiving_number')
            or state.get('services',['iMessage'])!=sorted(config.get('services',['iMessage']))
            or state.get('input_mode','marked')!='natural'):
        raise ValueError('Original checkpoint and route must match exactly')
    stamp=datetime.fromisoformat(received_at)
    if (now.tzinfo is None or stamp.tzinfo is None or not original.starts_at<=stamp<=now
            or not isinstance(guid,str) or not guid or type(row_id) is not int or row_id<=state['after']
            or not isinstance(body_hash,str) or len(body_hash)!=64 or any(c not in '0123456789abcdef' for c in body_hash)):
        raise ValueError('Approve the exact unread actual reply')
    token=config.get('token','')
    if len(token)<32:raise ValueError('Existing connector authentication required')
    current={**original.spec(),'expires_at':None,'until_stopped':True,
        'original_expires_at':original.end_iso(),'ongoing_since':now.isoformat()}
    data={'version':1,'mode':'until_stopped','journal_id':uuid4().hex,'actor':actor.strip(),'approved_at':now.isoformat(),
        'route':route(config),'previous_sessions':{phone:original.spec()},'sessions':{phone:current},
        'checkpoint_after':state['after'],'checkpoint_sha256':hashlib.sha256(checkpoint_bytes).hexdigest(),
        'target':{'phone':phone,'guid':guid,'row_id':row_id,'body_hash':body_hash,'received_at':stamp.isoformat()}}
    journal={**data,'signature':_sign(data,token)}
    return {**deepcopy(config),'test_sessions':{phone:current},'ongoing_authorization':journal}


def verify(raw,token):
    value=json.loads(raw) if isinstance(raw,str) else deepcopy(raw)
    if not isinstance(value,dict):raise ValueError('Explicit ongoing approval journal required')
    signature=value.pop('signature',None)
    if (not isinstance(signature,str) or not hmac.compare_digest(signature,_sign(value,token))
            or value.get('version')!=1 or value.get('mode')!='until_stopped' or not value.get('actor')
            or len(value.get('route',{}).get('phones',[]))!=1):
        raise ValueError('Ongoing approval journal changed or is missing')
    sessions=parse_sessions(value['sessions'],set(value['route']['phones']),allow_ongoing=True)
    previous=parse_sessions(value['previous_sessions'],set(sessions))
    approved=datetime.fromisoformat(value['approved_at'])
    target=value.get('target',{})
    if (approved.tzinfo is None or target.get('phone') not in sessions
            or type(target.get('row_id')) is not int or target['row_id']<=value['checkpoint_after']
            or not isinstance(target.get('guid'),str) or not target['guid']
            or not isinstance(target.get('body_hash'),str) or len(target['body_hash'])!=64
            or any(c not in '0123456789abcdef' for c in target['body_hash'])):
        raise ValueError('Approved ongoing source proof is invalid')
    received=datetime.fromisoformat(target['received_at'])
    if received.tzinfo is None or not previous[target['phone']].starts_at<=received<=approved:
        raise ValueError('Approved ongoing source time is invalid')
    for phone,selected in sessions.items():
        old=previous[phone]
        if (selected.expires_at is not None or selected.id!=old.id or selected.starts_at!=old.starts_at
                or selected.original_expires_at!=old.expires_at or selected.ongoing_since!=approved):
            raise ValueError('Only the same original Mac conversation can become ongoing')
    value['signature']=signature
    return value


def adopt(config,state,checkpoint_bytes,active,now):
    journal=verify(config.get('ongoing_authorization'),config.get('token',''))
    if (route(config)!=journal['route'] or config['test_sessions']!=journal['sessions']
            or now<datetime.fromisoformat(journal['approved_at'])):
        raise ValueError('Ongoing configuration differs from its approved route')
    if state.get('ongoing_journal')==journal['journal_id']:
        if state.get('test_sessions')!=journal['sessions'] or state.get('after',-1)<journal['checkpoint_after']:
            raise ValueError('Adopted ongoing checkpoint scope changed')
        return state,journal
    if (state.get('test_sessions')!=journal['previous_sessions'] or state.get('after')!=journal['checkpoint_after']
            or hashlib.sha256(checkpoint_bytes).hexdigest()!=journal['checkpoint_sha256'] or active
            or state.get('claim_response_pending') or state.get('claim_response_uncertain')
            or any(d.get('outcome') not in {'submitted','blocked'} for d in state.get('dispatches',{}).values())):
        raise ValueError('Checkpoint or unresolved native ledger changed; no ongoing transition applied')
    # Preserve every cursor, claim and receipt. Only approved scope metadata moves.
    result={**deepcopy(state),'test_sessions':journal['sessions'],'ongoing_journal':journal['journal_id']}
    return result,journal
