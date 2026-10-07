"""Dead pre-cloud candidates, bounded renewal and unknown-process fences."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from tests.test_public_connection_supervisor import setup, tool, write_private


@pytest.fixture
def failed_candidate(setup):
    supervisor, host, config_path = setup
    host.candidate_ok = False
    with pytest.raises(tool.Hold, match='not ready'):
        supervisor.recover()
    journal = tool.private_json(supervisor.journal_path)
    original_pid = journal['candidate']['pid']
    log = Path(journal['candidate']['log'])
    log.write_text('https://failed-candidate.trycloudflare.com\n'
                   'Register tunnel error from server side error="Unauthorized: Tunnel not found"\n')
    clock = {'now': datetime.fromisoformat(journal['candidate']['started_at']) + timedelta(seconds=301)}
    host.now_utc = lambda: clock['now']
    request = host.request
    def probe(base, path, **kwargs):
        if base == 'https://failed-candidate.trycloudflare.com':
            host.calls.append(('request', base, path))
            return 0, {'dns_failure': True}
        return request(base, path, **kwargs)
    host.request = probe
    host.pid = original_pid + 1
    stopped = {'identity': None, 'after': 30, 'elapsed': 0}
    stop = host.stop
    def stop_with_grace(identity):
        result = stop(identity)
        if result and identity['pid'] == original_pid:
            stopped['identity'] = identity
        return result
    host.stop = stop_with_grace
    def sleep(seconds):
        clock['now'] += timedelta(seconds=seconds)
        if stopped['identity']:
            stopped['elapsed'] += seconds
            if stopped['elapsed'] >= stopped['after']:
                host.identities.pop(original_pid, None)
    host.sleep = sleep
    return supervisor, host, journal, log, clock, stopped, config_path


def launches(host):
    return sum(call[0] == 'launch' for call in host.calls)


def test_demonstrably_dead_candidate_replaced_once_after_actual_graceful_stop(failed_candidate):
    supervisor, host, old, log, clock, stopped, config = failed_candidate
    host.candidate_ok = True
    supervisor.recover()
    fresh = tool.private_json(supervisor.journal_path)
    assert launches(host) == 2
    assert fresh['id'] == old['id'] and fresh['candidate_replacements'] == 1
    assert fresh['candidate_history'][0]['pid'] == old['candidate']['pid']
    assert Path(fresh['candidate_history'][0]['archived_log']).is_file()
    archive = supervisor.root / ('failed-candidate-' + old['id'] + '-1.private.json')
    assert tool.private_json(archive)['journal']['candidate']['identity'] == old['candidate']['identity']
    assert stopped['elapsed'] >= 30 and host.stopped == [old['candidate']['pid'], 99]
    assert sum(c[0] == 'cli' and c[1][:2] == ['pages','deploy'] for c in host.calls) == 1
    assert fresh['phase'] == 'complete' and tool.pages_state(supervisor.config)['status'] == 'ready'


@pytest.mark.parametrize('problem', ['grace', 'no_rejection', 'new_registration', 'http503', 'policy403', 'healthy', 'invalid_age'])
def test_elapsed_time_or_generic_network_failure_never_authorizes_retirement(failed_candidate, problem):
    supervisor, host, old, log, clock, stopped, config = failed_candidate
    journal = tool.private_json(supervisor.journal_path)
    if problem == 'grace':
        clock['now'] = datetime.fromisoformat(journal['candidate']['started_at']) + timedelta(seconds=299)
    elif problem == 'no_rejection':
        log.write_text('https://failed-candidate.trycloudflare.com\nnetwork error\n')
    elif problem == 'new_registration':
        log.write_text(log.read_text() + 'Registered tunnel connection\n')
    elif problem == 'invalid_age':
        journal['candidate']['started_at'] = 'unproven';write_private(supervisor.journal_path,journal)
    else:
        probe = host.request
        def response(base, path, **kwargs):
            if base == 'https://failed-candidate.trycloudflare.com':
                if problem == 'healthy':
                    return (200, {'name':'Text Monkey','connected':True,'macBridgeConnected':True,'messagingTransport':'mac_messages'}) if path == '/api/config' else (401,{})
                return (403 if problem == 'policy403' else 503), {}
            return probe(base, path, **kwargs)
        host.request = response
    with pytest.raises(tool.Hold):supervisor.recover()
    assert launches(host) == 1 and host.stopped == []
    assert not any(c[0] == 'cli' for c in host.calls)


@pytest.mark.parametrize('problem',['generation','route','pid_identity','source_plan'])
def test_changed_provenance_holds_before_retirement(failed_candidate,problem):
    supervisor,host,old,log,clock,stopped,config=failed_candidate
    if problem=='generation':
        state=tool.private_json(supervisor.config['deployment_state_file']);state['generation']='2'*32;write_private(Path(supervisor.config['deployment_state_file']),state)
    elif problem=='route':
        write_private(Path(supervisor.config['route_file']),{'backend_url':'https://operator-change.trycloudflare.com'})
    elif problem=='pid_identity':
        pid=old['candidate']['pid'];host.identities[pid]={**host.identities[pid],'started':'reused'}
    else:
        (Path(supervisor.plan['upload'])/'index.html').write_text('unreviewed bytes')
    with pytest.raises(tool.Hold):supervisor.recover()
    assert launches(host)==1 and host.stopped==[] and not any(c[0]=='cli' for c in host.calls)


def test_one_replacement_budget_survives_restart_and_backoff(failed_candidate):
    supervisor,host,old,log,clock,stopped,config=failed_candidate
    with pytest.raises(tool.Hold,match='not ready'):supervisor.recover()
    journal=tool.private_json(supervisor.journal_path)
    assert journal['candidate_replacements']==1 and launches(host)==2
    log.write_text('https://failed-candidate.trycloudflare.com\nRegister tunnel error from server side error="Unauthorized: Tunnel not found"\n')
    clock['now']+=timedelta(seconds=301)
    restarted=tool.Supervisor(supervisor.config,supervisor.plan,supervisor.sha,host)
    before=list(host.stopped)
    for _ in range(3):
        with pytest.raises(tool.Hold,match='budget exhausted'):restarted.recover()
    assert launches(host)==2 and host.stopped==before and not any(c[0]=='cli' for c in host.calls)


def test_slow_retirement_stays_fenced_and_never_launches_or_signals_again(failed_candidate):
    supervisor,host,old,log,clock,stopped,config=failed_candidate;stopped['after']=90
    with pytest.raises(tool.Hold,match='graceful stop is pending'):supervisor.recover()
    assert launches(host)==1 and host.stopped==[old['candidate']['pid']]
    assert tool.private_json(supervisor.journal_path)['phase']=='candidate_retire_pending'
    before=list(host.calls)
    with pytest.raises(tool.Hold,match='no repeat'):supervisor.recover()
    assert host.calls==before


@pytest.mark.parametrize('replacement',[False,True])
@pytest.mark.parametrize('problem',['exception','missing_identity'])
def test_unknown_launch_outcome_is_durable_and_never_replayed(setup,failed_candidate,replacement,problem):
    if replacement:supervisor,host,*_=failed_candidate
    else:supervisor,host,_=setup
    launch=host.launch
    def uncertain(log):
        pid=launch(log)
        if problem=='exception':raise OSError('synthetic unknown Popen outcome')
        host.identities.pop(pid);return pid
    host.launch=uncertain
    with pytest.raises((tool.Hold,OSError)):supervisor.recover()
    assert tool.private_json(supervisor.journal_path)['phase']=='candidate_launch_pending'
    before=list(host.calls)
    with pytest.raises(tool.Hold,match='no repeat'):supervisor.recover()
    assert host.calls==before and not any(c[0]=='cli' for c in host.calls)


@pytest.mark.parametrize('phase',['secret_pending','secret_updated','deploy_pending','verification_pending','committed'])
def test_pre_cloud_replacement_never_runs_in_a_cloud_mutation_phase(failed_candidate,phase):
    supervisor,host,old,log,clock,stopped,config=failed_candidate
    journal=tool.private_json(supervisor.journal_path);journal['phase']=phase
    assert supervisor.candidate_failure_confirmed(journal,log,'https://failed-candidate.trycloudflare.com') is False
    assert launches(host)==1 and host.stopped==[]


def test_registered_candidate_dns_timeout_resumes_same_process_without_relaunch(setup):
    supervisor,host,_=setup
    unresolved={'value':True}
    request=host.request
    def probe(base,path,**kwargs):
        if base=='https://replacement-tunnel.trycloudflare.com' and unresolved['value']:
            host.calls.append(('request',base,path));return 0,{'dns_failure':True}
        return request(base,path,**kwargs)
    host.request=probe
    launch=host.launch
    def registered(log):
        pid=launch(log);log.write_text(log.read_text()+'Registered tunnel connection\n');return pid
    host.launch=registered
    with pytest.raises(tool.Hold,match='not ready'):supervisor.recover()
    waiting=tool.private_json(supervisor.journal_path)
    assert waiting['phase']=='candidate_started' and waiting['candidate']['pid']==100
    assert launches(host)==1 and host.stopped==[] and not any(c[0]=='cli' for c in host.calls)
    unresolved['value']=False
    supervisor.recover()
    completed=tool.private_json(supervisor.journal_path)
    assert completed['candidate']['pid']==100 and completed['phase']=='complete'
    assert launches(host)==1 and 'candidate_history' not in completed
    assert sum(c[0]=='cli' and c[1][:2]==['pages','deploy'] for c in host.calls)==1


def test_initial_dns_propagation_recovers_inside_readiness_window(setup):
    supervisor,host,_=setup;request=host.request;remaining={'count':3}
    def probe(base,path,**kwargs):
        if base=='https://replacement-tunnel.trycloudflare.com' and remaining['count']:
            remaining['count']-=1;host.calls.append(('request',base,path));return 0,{'dns_failure':True}
        return request(base,path,**kwargs)
    host.request=probe
    supervisor.recover()
    assert launches(host)==1 and remaining['count']==0
    assert tool.private_json(supervisor.journal_path)['phase']=='complete'
    assert sum(c[0]=='cli' and c[1][:2]==['pages','deploy'] for c in host.calls)==1


def test_pid_reuse_during_graceful_stop_holds_without_another_signal(failed_candidate):
    supervisor,host,old,log,clock,stopped,config=failed_candidate
    sleep=host.sleep
    def reused(seconds):
        sleep(seconds)
        if stopped['identity'] and stopped['elapsed']>=1:
            pid=old['candidate']['pid'];host.identities[pid]={**old['candidate']['identity'],'started':'another-process'}
    host.sleep=reused
    with pytest.raises(tool.Hold,match='PID identity changed'):supervisor.recover()
    assert host.stopped==[old['candidate']['pid']] and launches(host)==1
    before=list(host.calls)
    with pytest.raises(tool.Hold,match='no repeat'):supervisor.recover()
    assert host.calls==before


def test_candidate_recovers_between_confirmation_probes_and_is_not_retired(failed_candidate):
    supervisor,host,old,log,clock,stopped,config=failed_candidate;request=host.request;attempts={'count':0}
    def recovered(base,path,**kwargs):
        if base=='https://failed-candidate.trycloudflare.com':
            host.calls.append(('request',base,path))
            if path=='/api/config':
                attempts['count']+=1
                if attempts['count']==1:return 0,{'dns_failure':True}
                return 200,{'name':'Text Monkey','connected':True,'macBridgeConnected':True,'messagingTransport':'mac_messages'}
            return (200,{}) if (kwargs.get('headers') or {}).get('Authorization') else (401,{})
        return request(base,path,**kwargs)
    host.request=recovered
    supervisor.recover()
    assert launches(host)==1 and old['candidate']['pid'] not in host.stopped
    assert tool.private_json(supervisor.journal_path)['candidate']['pid']==old['candidate']['pid']
    assert tool.private_json(supervisor.journal_path)['phase']=='complete'


def test_fresh_registration_during_confirmation_retains_candidate_while_dns_propagates(failed_candidate):
    supervisor,host,old,log,clock,stopped,config=failed_candidate
    sleep=host.sleep;registered={'value':False}
    def fresh_registration(seconds):
        sleep(seconds)
        if seconds==2 and not registered['value']:
            log.write_text(log.read_text()+'Registered tunnel connection\n');registered['value']=True
    host.sleep=fresh_registration
    with pytest.raises(tool.Hold,match='not ready'):supervisor.recover()
    assert registered['value'] and launches(host)==1 and host.stopped==[]
    assert tool.private_json(supervisor.journal_path)['phase']=='candidate_started'
    assert not any(c[0]=='cli' for c in host.calls)


@pytest.mark.parametrize('boundary',['identity_check','pending_write'])
def test_fresh_registration_at_retirement_boundary_cancels_stop_and_keeps_candidate(failed_candidate,boundary):
    supervisor,host,old,log,clock,stopped,config=failed_candidate
    if boundary=='identity_check':
        checked=supervisor.require_candidate_identity;calls={'count':0}
        def fresh_after_identity(journal):
            checked(journal);calls['count']+=1
            if calls['count']==3:log.write_text(log.read_text()+'Registered tunnel connection\n')
        supervisor.require_candidate_identity=fresh_after_identity
    else:
        save=supervisor.save
        def fresh_after_pending(journal,phase):
            save(journal,phase)
            if phase=='candidate_retire_pending':log.write_text(log.read_text()+'Registered tunnel connection\n')
        supervisor.save=fresh_after_pending
    with pytest.raises(tool.Hold,match='not ready'):supervisor.recover()
    fresh=tool.private_json(supervisor.journal_path)
    assert launches(host)==1 and host.stopped==[] and fresh['candidate']['pid']==old['candidate']['pid']
    assert fresh['phase']=='candidate_started' and fresh.get('candidate_replacements',0)==0
    assert not any(c[0]=='cli' for c in host.calls)
    if boundary=='pending_write':
        assert len(fresh['candidate_retirement_deferrals'])==1
        assert Path(fresh['candidate_retirement_deferrals'][0]['archive']).is_file()
        assert not (supervisor.root/('failed-candidate-'+old['id']+'-1.private.json')).exists()
