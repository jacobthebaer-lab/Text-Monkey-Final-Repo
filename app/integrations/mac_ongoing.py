"""Explicit signed Mac transitions and additive participant enrollment.

These helpers are pure: the operator applies returned configuration/checkpoint
only after stopping the connector and checking its actual native ledger.
"""
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


def _configuration_hash(config):
    return _digest({k:v for k,v in config.items() if k!='ongoing_authorization'})


def _scope_matches(state,journal):
    scope=journal['route']
    return (state.get('phones')==sorted(scope['phones']) and state.get('test_sessions')==journal['sessions']
        and state.get('receiving_number')==scope['receiving_number']
        and state.get('services',['iMessage'])==sorted(scope['services'])
        and state.get('input_mode','marked')==scope['input_mode'])


def _settled(state,active):
    dispatches=state.get('dispatches',{})
    return (isinstance(active,list) and not active and isinstance(dispatches,dict)
        and not state.get('claim_response_pending') and not state.get('claim_response_uncertain')
        and all(isinstance(d,dict) and d.get('outcome') in {'submitted','blocked'} for d in dispatches.values()))


def enroll(config,checkpoint_bytes,*,phone,name,actor,operator_confirmed,active,now):
    """Approve one named fresh participant until stopped, with no invented reply.

    Call at the actual stopped-connector transition time with current checkpoint
    bytes and the actual active-claim list. No consent or backend record changes.
    """
    from app.sms.mac_provider import demo_phones
    if (operator_confirmed is not True or not isinstance(actor,str) or not actor.strip()
            or not isinstance(name,str) or not name.strip() or len(name.strip())>120):
        raise ValueError('Explicit named participant and operator approval required')
    if not isinstance(phone,str) or demo_phones(phone)!=frozenset({phone}):
        raise ValueError('One exact international participant phone required')
    if now.tzinfo is None:raise ValueError('Timezone-aware actual enrollment time required')
    token=config.get('token','')
    if not isinstance(token,str) or len(token)<32:raise ValueError('Existing connector authentication required')
    previous=verify(config.get('ongoing_authorization'),token)
    if (route(config)!=previous['route'] or config.get('test_sessions')!=previous['sessions']
            or previous['route']['input_mode']!='natural' or phone in previous['sessions']
            or now<datetime.fromisoformat(previous['approved_at'])):
        raise ValueError('Enrollment must add one new participant to the exact approved natural route')
    if previous['version']==2 and _configuration_hash(config)!=previous['configuration_sha256']:
        raise ValueError('Previously approved connector configuration changed')
    state=json.loads(checkpoint_bytes)
    if (not _scope_matches(state,previous) or state.get('ongoing_journal')!=previous['journal_id']
            or type(state.get('after')) is not int or state['after']<previous['checkpoint_after']
            or state.get('ongoing_target_received') is not True or not _settled(state,active)):
        raise ValueError('Current adopted checkpoint and settled native ledger required')
    selected={'id':uuid4().hex,'starts_at':now.isoformat(),'expires_at':None,'until_stopped':True,
        'ongoing_since':now.isoformat(),'enrolled_at':now.isoformat()}
    result={**deepcopy(config),'phones':sorted([*previous['sessions'],phone]),
        'test_sessions':{**deepcopy(previous['sessions']),phone:selected}}
    data={'version':2,'mode':'until_stopped','journal_id':uuid4().hex,'actor':actor.strip(),
        'approved_at':now.isoformat(),'route':route(result),'previous_authorization':previous,
        'previous_sessions':deepcopy(previous['sessions']),'sessions':deepcopy(result['test_sessions']),
        'checkpoint_after':state['after'],'checkpoint_sha256':hashlib.sha256(checkpoint_bytes).hexdigest(),
        'configuration_sha256':_configuration_hash(result),'target':deepcopy(previous['target']),
        'enrollment':{'phone':phone,'name':name.strip(),'session_id':selected['id'],'enrolled_at':now.isoformat()}}
    result['ongoing_authorization']={**data,'signature':_sign(data,token)}
    verify(result['ongoing_authorization'],token)
    return result


def _verify_enrollment(value,previous):
    enrollment=value.get('enrollment',{})
    phone=enrollment.get('phone')
    from app.sms.mac_provider import demo_phones
    if not isinstance(phone,str) or demo_phones(phone)!=frozenset({phone}):
        raise ValueError('Enrollment needs one exact participant phone')
    expected_route={**previous['route'],'phones':sorted([*previous['sessions'],phone])}
    approved=datetime.fromisoformat(value['approved_at'])
    if (phone in previous['sessions'] or value.get('route')!=expected_route
            or value.get('previous_sessions')!=previous['sessions']
            or value.get('target')!=previous['target'] or expected_route['input_mode']!='natural'
            or approved.tzinfo is None or approved<datetime.fromisoformat(previous['approved_at'])
            or not isinstance(enrollment.get('name'),str) or not enrollment['name'].strip()
            or len(enrollment['name'])>120 or type(value.get('checkpoint_after')) is not int
            or value['checkpoint_after']<previous['checkpoint_after']):
        raise ValueError('Existing participant, route or original source lineage changed')
    sessions=parse_sessions(value['sessions'],set(expected_route['phones']),allow_ongoing=True)
    fresh=sessions.get(phone)
    if (set(sessions)!=set(expected_route['phones']) or not fresh or fresh.enrolled_at!=approved
            or fresh.starts_at!=approved or fresh.ongoing_since!=approved or fresh.expires_at is not None
            or fresh.original_expires_at is not None or enrollment.get('session_id')!=fresh.id
            or enrollment.get('enrolled_at')!=value['approved_at']
            or value['sessions']!={**previous['sessions'],phone:fresh.spec()}):
        raise ValueError('Only one fresh enrollment session may be added')
    for field in ('checkpoint_sha256','configuration_sha256'):
        digest=value.get(field)
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Enrollment must bind the exact current checkpoint and configuration')


def verify(raw,token):
    if not isinstance(token,str) or len(token)<32:raise ValueError('Existing connector authentication required')
    value=json.loads(raw) if isinstance(raw,str) else deepcopy(raw)
    root=value
    lineage=[]
    # Validate the signed chain iteratively so the number of enrolled people
    # is not limited by a recursive review-depth counter.
    while isinstance(value,dict) and value.get('version')==2:
        unsigned=_verified_payload(value,token)
        lineage.append((value,unsigned))
        value=unsigned.get('previous_authorization')
    unsigned=_verified_payload(value,token)
    _verify_original(unsigned)
    previous=value
    for signed,unsigned in reversed(lineage):
        _verify_enrollment(unsigned,previous)
        previous=signed
    return root


def _verified_payload(value,token):
    if not isinstance(value,dict):raise ValueError('Explicit ongoing approval journal required')
    signature=value.get('signature')
    unsigned={key:item for key,item in value.items() if key!='signature'}
    if (not isinstance(signature,str) or not hmac.compare_digest(signature,_sign(unsigned,token))
            or value.get('version') not in (1,2) or value.get('mode')!='until_stopped'
            or not isinstance(value.get('actor'),str) or not value['actor'].strip()):
        raise ValueError('Ongoing approval journal changed or is missing')
    return unsigned


def _verify_original(value):
    if len(value.get('route',{}).get('phones',[]))!=1:
        raise ValueError('Original ongoing transition requires one participant')
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


def adopt(config,state,checkpoint_bytes,active,now):
    journal=verify(config.get('ongoing_authorization'),config.get('token',''))
    if (route(config)!=journal['route'] or config['test_sessions']!=journal['sessions']
            or now.tzinfo is None or now<datetime.fromisoformat(journal['approved_at'])
            or (journal['version']==2 and _configuration_hash(config)!=journal['configuration_sha256'])):
        raise ValueError('Ongoing configuration differs from its approved route')
    if state.get('ongoing_journal')==journal['journal_id']:
        if not _scope_matches(state,journal) or state.get('after',-1)<journal['checkpoint_after']:
            raise ValueError('Adopted ongoing checkpoint scope changed')
        return state,journal
    if journal['version']==2:
        previous=journal['previous_authorization']
        if (not _scope_matches(state,previous) or state.get('ongoing_journal')!=previous['journal_id']
                or state.get('ongoing_target_received') is not True or json.loads(checkpoint_bytes)!=state):
            raise ValueError('Enrollment must preserve the adopted original session and received source')
    if (state.get('test_sessions')!=journal['previous_sessions'] or state.get('after')!=journal['checkpoint_after']
            or hashlib.sha256(checkpoint_bytes).hexdigest()!=journal['checkpoint_sha256'] or not _settled(state,active)):
        raise ValueError('Checkpoint or unresolved native ledger changed; no ongoing transition applied')
    # Preserve every cursor, claim and receipt. Only approved scope metadata moves.
    result={**deepcopy(state),'phones':sorted(journal['route']['phones']),
        'test_sessions':journal['sessions'],'ongoing_journal':journal['journal_id']}
    return result,journal
