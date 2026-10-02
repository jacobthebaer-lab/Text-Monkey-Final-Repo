from app.config import Settings, settings_from_env


def test_defaults_are_safe():
    s = Settings()
    assert s.sms_provider == "mock"
    assert s.live_sms is False
    assert s.sms_is_live is False
    assert s.gloo_endpoint == "guarded"
    assert s.max_agent_steps == 15


def test_sms_is_live_requires_both_flags():
    assert Settings(sms_provider="twilio", live_sms=False).sms_is_live is False
    assert Settings(sms_provider="mock", live_sms=True).sms_is_live is False
    assert Settings(sms_provider="twilio", live_sms=True).sms_is_live is True


def test_gloo_base_url_follows_endpoint_family():
    assert Settings().gloo_base_url == "https://platform.ai.gloo.com/ai/v2/guarded"
    assert (
        Settings(gloo_endpoint="direct").gloo_base_url
        == "https://platform.ai.gloo.com/ai/v2/direct"
    )


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    monkeypatch.setenv("LIVE_SMS", "true")
    monkeypatch.setenv("MAX_AGENT_STEPS", "5")
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("MAC_MESSAGE_SERVICES", "SMS")

    s = settings_from_env()
    assert s.sms_provider == "twilio"
    assert s.live_sms is True
    assert s.max_agent_steps == 5
    assert s.demo_mode is False
    assert s.mac_message_services == "SMS"


def test_env_bool_parsing(monkeypatch):
    for raw, expected in [("true", True), ("1", True), ("YES", True), ("false", False), ("0", False), ("", False)]:
        monkeypatch.setenv("LIVE_SMS", raw)
        assert settings_from_env().live_sms is expected, raw
