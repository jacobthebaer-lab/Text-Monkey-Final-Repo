"""One connector, durable enrollment recovery before any native read or dispatch."""
import base64
import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime,timezone
from app.integrations.mac_ongoing import enroll,adopt,_settled,_digest,_configuration_hash,verify
from app.integrations.test_sessions import parse_sessions


def _hash(raw): return hashlib.sha256(raw).hexdigest()


def _atomic_bytes(path,raw):
    temporary=path.with_suffix('.enrollment-restore.tmp')
    with temporary.open('wb') as stream:
        os.chmod(temporary,0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def finish_cancelled_boot(config,config_path):
    from pathlib import Path
    from app.integrations.mac_messages import atomic_json
    path=Path(config['state_path']).expanduser()
    pending=path.with_suffix('.enrollment')
    if not pending.exists(): return config
    transaction=json.loads(pending.read_text())
    if transaction.get('phase')!='cancelled': return config
    previous=transaction['previous_config'];proposed=transaction['config']
    raw=base64.b64decode(transaction['previous_checkpoint'],validate=True)
    journal=verify(proposed['ongoing_authorization'],proposed['token'])
    original=json.loads(raw)
    adopted_state,_=adopt(proposed,original,raw,[],datetime.now(timezone.utc))
    if (_digest(journal['previous_authorization'])!=_digest(previous['ongoing_authorization'])
            or _digest(config) not in {_digest(previous),_digest(proposed)}
            or _digest(json.loads(path.read_text())) not in {_digest(original),_digest(adopted_state)}
            or _digest(adopted_state)!=_digest(transaction['adopted_state'])):
        raise ValueError('Cancelled enrollment files or native ledger changed')
    atomic_json(config_path,previous);_atomic_bytes(path,raw);pending.unlink()
    return previous


def _active(worker):
    return json.loads(worker.active_path.read_text()) if worker.active_path.exists() else []


def _apply(worker,config,state):
    worker.config=deepcopy(config);worker.state=deepcopy(state)
    worker.ongoing_journal=verify(config['ongoing_authorization'],config['token'])
    worker.test_sessions=parse_sessions(config['test_sessions'],set(config['phones']),allow_ongoing=True)
    worker.phones=frozenset(config['phones'])
    worker.reader.phones=tuple(sorted(worker.phones))
    worker.reader.test_sessions=worker.test_sessions
    worker.reader.ongoing_journal=worker.ongoing_journal


def _response(worker,path,data,intent_id):
    result=worker.post(path,data)
    if result.get('intent_id')!=intent_id or result.get('phase') not in {'offered','prepared','committed','cancelled','held'}:
        raise ValueError('Roster enrollment response does not match the durable intent')
    return result


def resume(worker,transaction):
    from app.integrations.mac_messages import atomic_json
    intent_id=transaction['intent_id'];previous=transaction['previous_config'];config=transaction['config']
    raw=base64.b64decode(transaction['previous_checkpoint'],validate=True)
    original=json.loads(raw)
    if (_digest(json.loads(worker.config_path.read_text())) not in {_digest(previous),_digest(config)}
            or _digest(worker.state) not in {_digest(original),_digest(transaction['adopted_state'])}):
        raise ValueError('Enrollment configuration, cursor or native ledger changed outside its transaction')
    result=_response(worker,'/mac/roster/status',{'intent_id':intent_id},intent_id)
    if result['phase']=='offered':
        result=_response(worker,'/mac/roster/prepare',{'intent_id':intent_id,'journal':config['ongoing_authorization']},intent_id)
    if result['phase']=='held': return False
    if result['phase']=='prepared':
        active=_active(worker)
        if not _settled(worker.state,active): return False
        adopted_state,_=adopt(config,original,raw,active,datetime.now(timezone.utc))
        if _digest(adopted_state)!=_digest(transaction['adopted_state']):
            raise ValueError('The prepared enrollment checkpoint no longer matches')
        # Intent is already fsynced. Config first allows existing adopt() to finish
        # the exact checkpoint after a crash between these two atomic writes.
        atomic_json(worker.config_path,config)
        atomic_json(worker.state_path,adopted_state)
        # Do not extend the live reader until the backend durably accepts adoption.
        worker.state=deepcopy(adopted_state)
        ack={'intent_id':intent_id,'journal_id':config['ongoing_authorization']['journal_id'],
             'configuration_sha256':_configuration_hash(json.loads(worker.config_path.read_text())),
             'checkpoint_sha256':_hash(worker.state_path.read_bytes())}
        result=_response(worker,'/mac/roster/adopted',ack,intent_id)
    if result['phase']=='held': return False
    if result['phase']=='cancelled':
        # Nothing native ran during the staged transition. Restore only its exact
        # original files; never discard a later cursor, claim or manual change.
        transaction['phase']='cancelled';atomic_json(worker.enrollment_path,transaction)
        atomic_json(worker.config_path,previous)
        _atomic_bytes(worker.state_path,raw)
        _apply(worker,previous,original)
        worker.enrollment_path.unlink()
        return True
    if result['phase']=='committed':
        if result.get('journal_id')!=config['ongoing_authorization']['journal_id']:
            raise ValueError('Backend committed a different roster revision')
        adopted_state=transaction['adopted_state']
        if _digest(json.loads(worker.state_path.read_text()))!=_digest(adopted_state):
            raise ValueError('Committed enrollment requires its durable adopted checkpoint')
        _apply(worker,config,adopted_state)
        worker.enrollment_path.unlink()
        return True
    raise ValueError('Roster enrollment is in an unexpected phase')


def synchronize(worker):
    from app.integrations.mac_messages import atomic_json
    if worker.enrollment_path.exists(): return resume(worker,json.loads(worker.enrollment_path.read_text()))
    if not worker.ongoing_journal or worker.input_mode!='natural': return True
    active=_active(worker)
    if not active and (worker.state.get('claim_response_uncertain') or worker.state.get('claim_response_pending')):
        raw=worker.state_path.read_bytes();digest=_hash(raw)
        result=worker.post('/mac/roster/ledger',{'journal_id':worker.ongoing_journal['journal_id'],
            'checkpoint_sha256':digest,'dispatches':worker.state.get('dispatches',{})})
        if (result.get('settled') is True and result.get('journal_id')==worker.ongoing_journal['journal_id']
                and result.get('checkpoint_sha256')==digest
                and result.get('dispatches_sha256')==_digest(worker.state.get('dispatches',{}))
                and _hash(worker.state_path.read_bytes())==digest):
            worker.state.pop('claim_response_uncertain',None);worker.state.pop('claim_response_pending',None);worker.save()
    if not _settled(worker.state,active): return True
    result=worker.post('/mac/roster/poll',{'journal_id':worker.ongoing_journal['journal_id']})
    if result.get('status') in {'disabled','held','idle'}: return True
    if (result.get('status')!='offer' or result.get('base_journal_id')!=worker.ongoing_journal['journal_id']
            or not isinstance(result.get('intent_id'),str)):
        raise ValueError('Roster offer does not match the current worker authority')
    raw=worker.state_path.read_bytes()
    if json.loads(raw)!=worker.state: raise ValueError('Actual worker checkpoint changed')
    config=enroll(worker.config,raw,phone=result['phone'],name=result['name'],actor=result['actor'],
        operator_confirmed=True,active=active,now=datetime.now(timezone.utc))
    adopted_state,_=adopt(config,worker.state,raw,active,datetime.now(timezone.utc))
    transaction={'intent_id':result['intent_id'],'previous_config':deepcopy(worker.config),
        'previous_checkpoint':base64.b64encode(raw).decode(),'config':config,'adopted_state':adopted_state}
    atomic_json(worker.enrollment_path,transaction)
    return resume(worker,transaction)
