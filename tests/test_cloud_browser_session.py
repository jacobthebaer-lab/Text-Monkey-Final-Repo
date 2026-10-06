"""Manual helper preference parity; synthetic profiles only."""
import importlib.util
import json
from pathlib import Path

import pytest

source = Path(__file__).resolve().parents[1] / 'deploy/cloud/browser-login/start.py'
spec = importlib.util.spec_from_file_location('synthetic_cloud_login', source)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_native_helper_sets_standard_restoration_and_preserves_other_preferences(tmp_path):
    profile = tmp_path / 'profile'
    target = profile / 'Default/Preferences'
    target.parent.mkdir(parents=True)
    original = {'session':{'restore_on_startup':5,'other':'preserved'},'signin':{'other':'preserved'},'unrelated':{'marker':True}}
    target.write_text(json.dumps(original))
    helper.prepare_session_restoration(profile)
    assert json.loads(target.read_text()) == {**original,'session':{**original['session'],'restore_on_startup':1},'signin':{**original['signin'],'allowed_on_next_startup':False}}
    assert target.stat().st_mode & 0o777 == 0o600
    timestamp = target.stat().st_mtime_ns
    helper.prepare_session_restoration(profile)
    assert target.stat().st_mtime_ns == timestamp


def test_existing_restoration_does_not_skip_browser_profile_signin_optout(tmp_path):
    profile = tmp_path / 'profile'
    target = profile / 'Default/Preferences'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({'session':{'restore_on_startup':1},'signin':{'allowed_on_next_startup':True,'other':'preserved'}}))
    helper.prepare_session_restoration(profile)
    assert json.loads(target.read_text()) == {'session':{'restore_on_startup':1},'signin':{'allowed_on_next_startup':False,'other':'preserved'}}


@pytest.mark.parametrize('original', ['invalid JSON','[]','{"session":null}','{"signin":null}'])
def test_native_helper_holds_malformed_state_unchanged(tmp_path, original):
    profile = tmp_path / 'profile'
    target = profile / 'Default/Preferences'
    target.parent.mkdir(parents=True)
    target.write_text(original)
    with pytest.raises(RuntimeError, match='profile was not reset'):
        helper.prepare_session_restoration(profile)
    assert target.read_text() == original


def test_native_helper_rejects_active_or_stale_profile_lock(tmp_path):
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'SingletonLock').symlink_to('/synthetic-stale-lock')
    with pytest.raises(RuntimeError):
        helper.prepare_session_restoration(profile)
    assert not (profile / 'Default/Preferences').exists()
