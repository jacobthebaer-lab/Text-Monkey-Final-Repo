"""The same 25 journeys use real guards, a scripted model and mock delivery."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pytest
from evals import run_evals as replay

HERE=Path(__file__).resolve().parents[1]/'evals/cases'
CASES=json.loads((HERE/'workflows.yaml').read_text())

@pytest.mark.parametrize('case',CASES,ids=[case['id'] for case in CASES])
def test_workflow_replay(case,tmp_path):
    result=replay.execute(case,False,tmp_path)
    assert result['passed'],result['failures']
    for trace in result['trace']:
        if trace['offer_source'] is not None:
            assert trace['offer_source']['verified_mock_send']


def test_original_expectations_are_preserved_byte_for_byte():
    assert len(CASES)==25
    assert hashlib.sha256((HERE/'workflows-v1-frozen.yaml').read_bytes()).hexdigest()==(
        '59447bad5a9b1d9c4465a1047d465bd937c02bd03649336a4ece98c774a0e3dd')
    original=json.loads((HERE/'workflows-v1-frozen.yaml').read_text())
    assert [case['id'] for case in original]==[case['id'] for case in CASES]
    assert next(case for case in original if case['id']=='quiet_hours')['expected']['no_reply_to']==1


def test_missing_source_invitation_cannot_make_acceptance_replay_pass(tmp_path,monkeypatch):
    class IncompleteGloo(replay.ReplayGloo):
        def create_response(self,**kwargs):
            response=super().create_response(**kwargs)
            response.output=[item for item in response.output if getattr(item,'name',None)!='request_send_text']
            return response
    monkeypatch.setattr(replay,'ReplayGloo',IncompleteGloo)
    case=next(case for case in CASES if case['id']=='cancel_fill')
    result=replay.execute(case,False,tmp_path)
    assert not result['passed']
    assert any('no actual mock-delivered invitation' in failure for failure in result['failures'])
    assert result['actual']['filled']==0


def test_start_without_prior_disclosure_remains_held(tmp_path):
    case=deepcopy(next(case for case in CASES if case['id']=='start'))
    case['setup'].pop('prior_consent')
    case['expected']={'opt_in':False,'filled':0}
    result=replay.execute(case,False,tmp_path)
    assert result['passed'],result['failures']
    assert result['mock_provider_sends']==0
    assert result['trace'][0]['route']=='consent_required'


def test_unknown_criterion_cannot_silently_pass(tmp_path):
    case=deepcopy(next(case for case in CASES if case['id']=='confirmation'))
    case['expected']['not_a_real_criterion']=None
    with pytest.raises(ValueError,match='Unknown evaluation criterion'):
        replay.execute(case,False,tmp_path)
