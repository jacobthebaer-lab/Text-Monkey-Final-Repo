"""Coherent fictional application rehearsal; forbid network and recipient ranking."""
import pytest


@pytest.fixture
def rehearsal(monkeypatch):
    import httpx
    from app.core import ranking
    from app.agents import fill_agent
    from tools import rehearse_fictional_workflow
    def forbidden(*a,**k):
        pytest.fail('Rehearsal must not call network or recipient ranking')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(ranking,'rank_candidates',forbidden)
    monkeypatch.setattr(fill_agent,'replacement_pool',forbidden)
    return rehearse_fictional_workflow


def test_signup_recovery_review_notices_cancellation_and_explicit_replacement(rehearsal):
    result=rehearsal.run()
    assert result['passed'],result['continuation']
    proof=result['continuation']
    assert proof['passed'] and not proof['ranking_called'] and proof['native_messages_sent']==0
    assert result['real_gloo_usage']['calls']==0
    assert any(row.get('mock_replies')==["What's your last name?"] for row in proof['timeline'])
    reviews=[row for row in proof['timeline'] if row['step']=='Exact synthetic admin review']
    assert len(reviews)>=5 and all(row['content_hash'] for row in reviews)
    assert any(row.get('fill_state')=='waiting_quiet' for row in proof['timeline'])
    assert all(row['native_delivery_verified'] is False for row in proof['outgoing_records'])
    purposes=[row['purpose'] for row in proof['outgoing_records']]
    assert purposes.count('confirmation')==2 and purposes.count('reminder')==1
    assert purposes.count('coordinator_notify')==1
    assert 'outreach' not in purposes and 'cancellation_ack' not in purposes
    held_ack=proof['held_cancellation_acknowledgement']
    assert held_ack['status']=='pending' and held_ack['messages_sent']==0 and held_ack['content_hash']
    assert len(result['steps'][-1]['mock_messages'])==0
    recapture=next(row for row in proof['timeline'] if row['step']=='Current status recaptured through Gloo')
    assert recapture['old_approval_id']!=recapture['new_approval_id']
    assert recapture['new_composition_calls']==1 and recapture['notification_state']=='sent'
    assert recapture['linked_mock_message_id']==next(row['id'] for row in proof['outgoing_records']
                                                    if row['purpose']=='coordinator_notify')


@pytest.mark.parametrize('outage',['identity','copy','recovery'])
def test_model_outages_remain_incomplete_without_a_fallback(rehearsal,outage):
    result=rehearsal.run(outage)
    assert not result['passed'] and not result['continuation']['passed']
    assert result['real_gloo_usage']['calls']==0 and result['real_messages_sent']==0
    if outage=='recovery':
        assert not any(row.get('mock_replies')==["What's your last name?"]
                       for row in result['continuation']['timeline'])


def test_output_collision_refuses_rehearsal_before_model_or_app_calls(rehearsal,tmp_path,monkeypatch):
    target=tmp_path/'previous';target.mkdir()
    proof=target/'proof.json';proof.write_text('keep')
    monkeypatch.setattr(rehearsal,'run',lambda:pytest.fail('must not run on collision'))
    with pytest.raises(SystemExit) as stopped:
        rehearsal.main(['--output-dir',str(target)])
    assert stopped.value.code==2 and proof.read_text()=='keep'


def test_optimized_interpreter_cannot_claim_assertion_free_success(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    output=(tmp_path/'evidence').resolve()
    source=Path(__file__).parents[1]/'tools/rehearse_fictional_workflow.py'
    p=subprocess.run([sys.executable,'-O',str(source),'--output-dir',str(output)],
        env={**os.environ,'GLOO_API_KEY':'','PYTHON_DOTENV_DISABLED':'1'},
        text=True,capture_output=True,timeout=15)
    assert p.returncode==1,p.stderr
    report=json.loads((output/'fictional-workflow.json').read_text())
    assert report['passed'] is False and report['error_type']=='RuntimeChecksDisabled'


@pytest.mark.parametrize('budget',[1024,4096])
def test_real_client_opt_in_is_bounded_before_http(monkeypatch,budget):
    from dataclasses import replace
    from types import SimpleNamespace
    from app.config import Settings
    from app.llm import gloo_client
    from app.llm.gloo_client import GlooUnavailableError
    from tools.rehearse_fictional_workflow import bounded_real_gloo
    calls=[]
    def response(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text='fixture',usage=None)
    monkeypatch.setattr(gloo_client,'OpenAI',lambda **k:SimpleNamespace(responses=SimpleNamespace(create=response)))
    settings=Settings(gloo_api_key='synthetic-unused',database_url='sqlite://',sms_provider='mock',
                      live_sms=False,automation_enabled=False,mac_bridge_enabled=False)
    client=bounded_real_gloo(settings,max_output_tokens=budget)
    for _ in range(24):client.create_response(model='fixture',input='{}',instructions='fixture')
    with pytest.raises(GlooUnavailableError):client.create_response(model='fixture',input='{}')
    assert len(calls)==24 and all(call['max_output_tokens']==budget for call in calls)
    byte_client=bounded_real_gloo(settings,max_output_tokens=budget)
    with pytest.raises(GlooUnavailableError):byte_client.create_response(model='fixture',input='x'*150001)
    assert byte_client.attempts==0
    with pytest.raises(ValueError):bounded_real_gloo(replace(settings,sms_provider='mac_messages'))


@pytest.mark.parametrize('value',[True,False,None,0,-1,4097,1.5,'4096'])
def test_invalid_output_limit_refused_before_client_initialization(monkeypatch,value):
    from app.config import Settings
    from app.llm import gloo_client
    from tools.rehearse_fictional_workflow import bounded_real_gloo
    monkeypatch.setattr(gloo_client,'OpenAI',lambda **k:pytest.fail('Invalid budget must not initialize SDK'))
    with pytest.raises(ValueError,match='integer from 1 to 4096'):
        bounded_real_gloo(Settings(),max_output_tokens=value)


@pytest.mark.parametrize('options,expected',[({},1024),({'max_output_tokens':1},1),({'max_output_tokens':4096},4096)])
def test_selected_output_limit_and_no_retry_override_request_options(monkeypatch,options,expected):
    from types import SimpleNamespace
    from app.config import Settings
    from app.llm import gloo_client
    from tools.rehearse_fictional_workflow import bounded_real_gloo
    initializations=[];calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text='synthetic response',usage=None)
    def initialize(**kwargs):
        initializations.append(kwargs)
        return SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(gloo_client,'OpenAI',initialize)
    settings=Settings(gloo_api_key='unused-synthetic',database_url='sqlite://',sms_provider='mock',
        live_sms=False,automation_enabled=False,mac_bridge_enabled=False)
    client=bounded_real_gloo(settings,**options)
    client.create_response(model='fixture',input='{}',max_output_tokens=99999)
    assert len(initializations)==1 and initializations[0]['max_retries']==0
    assert client.max_attempts==1 and len(calls)==1 and calls[0]['max_output_tokens']==expected


def test_real_client_protocol_does_not_require_scripted_calls_attribute(rehearsal,monkeypatch):
    """Mock SDK compatibility only, NOT evidence of an actual vendor model call."""
    from types import SimpleNamespace
    from app.config import Settings
    from app.llm import gloo_client
    scripted=rehearsal.ScriptedGloo()
    monkeypatch.setattr(gloo_client,'OpenAI',lambda **k:SimpleNamespace(
        responses=SimpleNamespace(create=scripted.create_response)))
    settings=Settings(gloo_api_key='unused-synthetic',database_url='sqlite://',sms_provider='mock',
                      automation_enabled=False,live_sms=False,mac_bridge_enabled=False)
    client=rehearsal.bounded_real_gloo(settings)
    # Explicitly larger, still bounded allowance for this entirely mocked SDK
    # lifecycle. Production defaults and separate cap-hold tests stay unchanged.
    monkeypatch.setattr(client,'MAX_INPUT_BYTES',200000)
    assert not hasattr(client,'calls')
    result=rehearsal.run(gloo=client,model_provenance='mocked_gloo_protocol')
    assert result['composition']=='mocked_gloo_protocol'
    assert result['real_gloo_usage']['calls']==0
    assert result['mocked_protocol_usage']==client.total_usage()
    assert client.total_usage()['calls']>0 and client.attempts<=24
    assert result['passed'],result['continuation']


def test_observed_gloo_join_recognition_preserves_material_signup_contract(rehearsal,monkeypatch):
    """Recorded response shape, via a mocked SDK, never another vendor call."""
    import json
    from types import SimpleNamespace
    from app.config import Settings
    from app.llm import gloo_client
    scripted=rehearsal.ScriptedGloo()
    def response(**kwargs):
        result=scripted.create_response(**kwargs)
        facts=json.loads(kwargs['input']) if kwargs['input'].startswith('[') else None
        if facts and facts[-1]['body']=='JOIN':
            result.output_text='{"signup":true,"identity_reply":false,"first_name":"","last_name":"","sensitive":false}'
        return result
    monkeypatch.setattr(gloo_client,'OpenAI',lambda **k:SimpleNamespace(
        responses=SimpleNamespace(create=response)))
    client=rehearsal.bounded_real_gloo(Settings(gloo_api_key='unused-synthetic',database_url='sqlite://',
        sms_provider='mock',automation_enabled=False,live_sms=False,mac_bridge_enabled=False))
    monkeypatch.setattr(client,'MAX_INPUT_BYTES',200000)
    result=rehearsal.run(gloo=client,model_provenance='mocked_gloo_protocol')
    assert result['passed'],result['continuation']
    assert result['steps'][0]['route']=='signup_name_needed'
    assert result['steps'][0]['pending_name_without_profile'] is True
    assert result['consent_provenance']['verified'] is True
    assert result['real_gloo_usage']['calls']==0
    assert result['mocked_protocol_usage']['calls']==23


def test_import_and_output_conflict_do_not_initialize_backend(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    source=Path(__file__).parents[1]/'tools/rehearse_fictional_workflow.py'
    private_db=tmp_path/'forbidden.db'
    env={**os.environ,'DATABASE_URL':'sqlite:///'+str(private_db),'SMS_PROVIDER':'mac_messages',
         'MAC_BRIDGE_ENABLED':'true','AUTOMATION_ENABLED':'true','GLOO_API_KEY':'synthetic-inherited',
         'PYTHON_DOTENV_DISABLED':'1'}
    code="import runpy,sys,os; runpy.run_path(sys.argv[1]); assert 'app.main' not in sys.modules; assert os.environ['GLOO_API_KEY']==''"
    p=subprocess.run([sys.executable,'-c',code,str(source)],env=env,text=True,capture_output=True,timeout=15)
    assert p.returncode==0,p.stderr
    existing=tmp_path/'previous';existing.mkdir()
    p=subprocess.run([sys.executable,str(source),'--output-dir',str(existing)],env=env,
                     text=True,capture_output=True,timeout=15)
    assert p.returncode==2 and not private_db.exists() and not list(existing.iterdir())


def test_injected_sdk_protocol_is_not_vendor_evidence(rehearsal,monkeypatch):
    from types import SimpleNamespace
    from app.config import Settings
    from app.llm import gloo_client
    from tools.check_synthetic_gloo_signup import run_signup
    scripted=rehearsal.ScriptedGloo()
    monkeypatch.setattr(gloo_client,'OpenAI',lambda **k:SimpleNamespace(
        responses=SimpleNamespace(create=scripted.create_response)))
    settings=Settings(gloo_api_key='unused-synthetic',database_url='sqlite://',sms_provider='mock',
                      automation_enabled=False,live_sms=False,mac_bridge_enabled=False)
    for declared in [None,'mocked_gloo_protocol']:
        client=rehearsal.bounded_real_gloo(settings)
        result=run_signup(gloo=client,model_provenance=declared)
        assert result['passed']
        assert result['real_gloo_usage']=={'calls':0,'input_tokens':0,'output_tokens':0}
        assert result['scripted_gloo_usage'] is None
        if declared is None:
            assert result['composition']=='injected_unverified'
            assert result['unverified_injected_usage']==client.total_usage()
        else:
            assert result['composition']=='mocked_gloo_protocol'
            assert result['mocked_protocol_usage']==client.total_usage()
