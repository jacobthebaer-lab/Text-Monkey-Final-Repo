"""Evidence receipts must survive repeated runs and output-path races."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / "tools/demo_admin_status.py"
spec = importlib.util.spec_from_file_location("demo_admin_status", SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_actual_cli_writes_fresh_evidence_and_preserves_it_on_rerun(tmp_path):
    output = tmp_path / "nested" / "evidence.json"
    command = [sys.executable, str(SCRIPT), "--output", str(output)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    original = output.read_bytes()
    evidence = json.loads(original)
    assert evidence["composition_mode"] == "synthetic_scripted_NO_REAL_AI"
    assert evidence["transport"] == "in_memory_mock_NO_REAL_DELIVERY"
    assert len(evidence["scenarios"]) == 5
    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode == 2
    assert "Evidence output already exists" in second.stderr
    assert output.read_bytes() == original


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling_symlink"])
def test_existing_output_refused_before_composition(tmp_path, monkeypatch, capsys, kind):
    output = tmp_path / "receipt.json"
    target = tmp_path / "target.json"
    if kind == "file":
        output.write_bytes(b"existing evidence")
    elif kind == "directory":
        output.mkdir()
    else:
        if kind == "symlink":
            target.write_bytes(b"linked evidence")
        output.symlink_to(target)

    def forbidden(*args, **kwargs):
        pytest.fail("Existing output must be rejected before any composition")

    monkeypatch.setattr(demo, "run", forbidden)
    monkeypatch.setattr(demo, "GlooClient", forbidden)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--real-gloo", "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        demo.main()
    assert error.value.code == 2
    assert "Evidence output already exists" in capsys.readouterr().err
    if kind == "file":
        assert output.read_bytes() == b"existing evidence"
    elif kind == "directory":
        assert output.is_dir() and not list(output.iterdir())
    else:
        assert output.is_symlink() and output.readlink() == target
        if kind == "symlink":
            assert target.read_bytes() == b"linked evidence"
        else:
            assert not target.exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling_symlink"])
def test_exclusive_final_write_refuses_path_created_during_run(tmp_path, monkeypatch, capsys, kind):
    output = tmp_path / "receipt.json"
    target = tmp_path / "target.json"

    def race(_gloo):
        if kind == "file":
            output.write_bytes(b"another writer's evidence")
        else:
            if kind == "symlink":
                target.write_bytes(b"linked evidence")
            output.symlink_to(target)
        return []

    monkeypatch.setattr(demo, "run", race)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        demo.main()
    assert error.value.code == 2
    assert "Evidence output already exists" in capsys.readouterr().err
    if kind == "file":
        assert output.read_bytes() == b"another writer's evidence"
    else:
        assert output.is_symlink() and output.readlink() == target
        if kind == "symlink":
            assert target.read_bytes() == b"linked evidence"
        else:
            assert not target.exists()
