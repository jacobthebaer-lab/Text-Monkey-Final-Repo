import json
from pathlib import Path
import pytest
from evals.run_evals import execute

CASES=json.loads((Path(__file__).resolve().parents[1]/'evals/cases/workflows.yaml').read_text())
@pytest.mark.parametrize('case',CASES,ids=[c['id'] for c in CASES])
def test_workflow_replay(case,tmp_path):
    result=execute(case,False,tmp_path)
    assert result['passed'],result['failures']
