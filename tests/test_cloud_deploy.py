"""Private disconnected setup validation, with no real Docker or API calls."""
import base64
import json
import stat
import subprocess
from types import SimpleNamespace

import pytest

from tools import cloud_deploy as deploy


def configured():
    return {"VOICE_EXPECTED_EMAIL": "owner@example.test", "VOICE_EXPECTED_NUMBER": "+15555550100",
            "ADMIN_EMAIL_ALLOWLIST": "owner@example.test,coordinator@example.test",
            "SUPERADMIN_EMAIL_ALLOWLIST": "OWNER@example.test", "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_" + "a" * 32,
            "ADMIN_SITE_URL": "https://experiment.example.test", "BACKEND_URL": "https://backend.example.test",
            "GLOO_API_KEY": "synthetic-private-gloo-key"}


@pytest.fixture
def private_env(tmp_path):
    path = tmp_path / ".env"
    deploy.initialize(path)
    return path


def test_init_is_private_disconnected_and_does_not_inherit_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOO_API_KEY", "existing-live-key-never-copy")
    monkeypatch.setenv("GOOGLE_VOICE_DEMO_PHONES", "+15555559999")
    path = tmp_path / ".env"
    deploy.initialize(path)
    values = deploy.read_env(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(values[key] == "false" for key in deploy.OFF_FLAGS)
    assert all(values[key] == "" for key in deploy.EMPTY_SCOPE)
    assert values["GLOO_API_KEY"] == values["VOICE_EXPECTED_NUMBER"] == ""
    assert len({values[key] for key in deploy.SECRET_FIELDS}) == 3
    assert all(len(values[key]) >= 32 for key in deploy.SECRET_FIELDS)
    report = deploy.check(path, skip_docker=True)
    assert report["scaffold_valid"]
    assert not report["configuration_complete"] and not report["runtime_verified"]
    assert "GLOO_API_KEY" in report["missing_config"]


def test_init_never_overwrites_existing_file_or_symlink(private_env, tmp_path):
    before = private_env.read_bytes()
    with pytest.raises(deploy.SetupError, match="already exists"):
        deploy.initialize(private_env)
    link = tmp_path / "linked.env"
    link.symlink_to(private_env)
    with pytest.raises(deploy.SetupError, match="already exists"):
        deploy.initialize(link)
    assert private_env.read_bytes() == before


def test_explicit_inputs_complete_configuration_without_claiming_connection(tmp_path):
    path = tmp_path / ".env"
    deploy.initialize(path, configured())
    report = deploy.check(path, skip_docker=True)
    assert report["scaffold_valid"] and report["configuration_complete"]
    assert not report["runtime_verified"] and not report["delivery_enabled"]
    assert report["provider_policy_hold"] and not report["live_ready"]


@pytest.mark.parametrize("updates,expected", [
    ({"LIVE_SMS": "true"}, "LIVE_SMS"),
    ({"COMPETITION_CONFIRMATION_REQUIRED": "false"}, "COMPETITION_CONFIRMATION_REQUIRED"),
    ({"GOOGLE_VOICE_TEST_SESSIONS": "private-session-value"}, "GOOGLE_VOICE_TEST_SESSIONS"),
    ({"SUPABASE_URL": "http://project.supabase.co"}, "SUPABASE_URL"),
    ({"ADMIN_SITE_URL": "https://localhost"}, "ADMIN_SITE_URL"),
    ({"ADMIN_SITE_URL": "https://127.0.0.1"}, "ADMIN_SITE_URL"),
    ({"BACKEND_URL": "https://private:secret@example.test"}, "BACKEND_URL"),
    ({"BACKEND_URL": "https://example.test/private?secret=value"}, "BACKEND_URL"),
    ({"BACKEND_URL": "https://8.8.8.8"}, "DNS hostname"),
    ({"BACKEND_URL": "https://[2606:4700:4700::1111]"}, "DNS hostname"),
    ({"VOICE_EXPECTED_NUMBER": "+44555550100"}, "VOICE_EXPECTED_NUMBER"),
    ({"SUPERADMIN_EMAIL_ALLOWLIST": "different@example.test"}, "subset"),
    ({"SUPABASE_PUBLISHABLE_KEY": "sb_secret_private-value"}, "SUPABASE_PUBLISHABLE_KEY"),
    ({"VOICE_API_TOKEN": "too-short"}, "VOICE_API_TOKEN"),
    ({"CLOUD_BACKEND_PORT": "80"}, "CLOUD_BACKEND_PORT"),
    ({"GLOO_ENDPOINT": "unrestricted"}, "GLOO_ENDPOINT"),
])
def test_invalid_configuration_fails_sanitized(private_env, updates, expected):
    values = deploy.read_env(private_env)
    values.update(configured())
    values.update(updates)
    errors, _ = deploy.validate(values)
    assert any(expected in message for message in errors)
    output = json.dumps(errors)
    for key in deploy.SECRET_FIELDS:
        if len(values[key]) >= 32:
            assert values[key] not in output
    assert "private-value" not in output and "secret=value" not in output


def test_shared_secrets_and_service_role_jwt_are_rejected(private_env):
    values = deploy.read_env(private_env)
    values["BACKEND_BRIDGE_KEY"] = values["VOICE_API_TOKEN"]
    encoded = base64.urlsafe_b64encode(json.dumps({"role": "service_role"}).encode()).decode().rstrip("=")
    values["SUPABASE_PUBLISHABLE_KEY"] = "e30." + encoded + ".synthetic"
    errors, _ = deploy.validate(values)
    assert any("different" in error for error in errors)
    assert any("SUPABASE_PUBLISHABLE_KEY" in error for error in errors)
    encoded = base64.urlsafe_b64encode(json.dumps({"role": "anon"}).encode()).decode().rstrip("=")
    assert deploy.publishable_key("e30." + encoded + ".synthetic")


def test_generated_backend_hostname_is_accepted_without_contacting_dns(tmp_path):
    inputs = configured()
    inputs["BACKEND_URL"] = "https://text-monkey.8-8-8-8.sslip.io"
    path = tmp_path / ".env"
    deploy.initialize(path, inputs)
    report = deploy.check(path, skip_docker=True)
    assert report["scaffold_valid"] and report["configuration_complete"]
    assert not report["runtime_verified"] and not report["delivery_enabled"]


def test_loose_permissions_are_reported_without_rewriting(private_env):
    private_env.chmod(0o644)
    report = deploy.check(private_env, skip_docker=True)
    assert not report["scaffold_valid"]
    assert "0600" in report["errors"][0]
    assert stat.S_IMODE(private_env.stat().st_mode) == 0o644


def test_inputs_cannot_enable_transport_or_inject_env_lines(tmp_path):
    for inputs in ({"LIVE_SMS": "true"}, {"GLOO_API_KEY": "secret\nLIVE_SMS=true"}):
        with pytest.raises(deploy.SetupError):
            deploy.initialize(tmp_path / ".env", inputs)
        assert not (tmp_path / ".env").exists()


def test_docker_checks_are_read_only_sanitized_and_bounded():
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs == {"capture_output": True, "timeout": 20, "check": False}
        return SimpleNamespace(returncode=0, stdout=b"private output", stderr=b"private error")
    report = deploy.docker_checks(fake_run)
    assert set(report.values()) == {"available"}
    assert all(not {"up", "start", "build", "pull", "login"}.intersection(command) for command in calls)
    assert "--quiet" in calls[-1] and "--no-interpolate" in calls[-1]
    assert "private" not in json.dumps(report)
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 20, output="private timeout output")
    assert set(deploy.docker_checks(unavailable).values()) == {"unavailable"}


def test_cli_never_prints_credentials_and_missing_config_is_optional(tmp_path, capsys):
    path = tmp_path / ".env"
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(configured()))
    assert deploy.main(["init", "--env-file", str(path), "--inputs-file", str(inputs)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["configuration_complete"]
    for value in deploy.read_env(path).values():
        if value and (len(value) >= 32 or value == configured()["GLOO_API_KEY"]):
            assert value not in output
    empty = tmp_path / "empty.env"
    assert deploy.main(["init", "--env-file", str(empty)]) == 0
    assert deploy.main(["check", "--env-file", str(empty), "--skip-docker", "--require-config"]) == 2
