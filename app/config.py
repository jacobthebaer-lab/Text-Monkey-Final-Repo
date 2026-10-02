"""Environment/config loading.

All configuration comes from environment variables (loaded from .env in
development). Access settings via get_settings(); tests can build a Settings
directly or call get_settings.cache_clear() after changing the environment.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

_TRUE_VALUES = {"true", "1", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Gloo AI
    gloo_api_key: str = ""
    gloo_endpoint: str = "guarded"  # guarded | direct
    agent_model: str = "gloo-anthropic-claude-sonnet-4.6"
    parser_model: str = "gloo-openai-gpt-5-mini"
    max_agent_steps: int = 15

    # SMS — real texts require sms_provider == "twilio" AND live_sms is True
    sms_provider: str = "mock"  # mock | twilio
    live_sms: bool = False
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Google Calendar (read-only)
    google_calendar_id: str = ""
    google_service_account_json: str = ""

    # App
    church_timezone: str = "America/Denver"
    admin_password: str = ""
    demo_mode: bool = True
    public_base_url: str = ""
    database_url: str = "sqlite:///./servfrictionless.db"
    allow_text_signup: bool = False
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    admin_email_allowlist: str = ""
    backend_bridge_key: str = ""
    admin_site_url: str = "http://127.0.0.1:8000/texty"
    mac_bridge_enabled: bool = False
    mac_bridge_token: str = ""
    mac_demo_phones: str = ""
    gloo_signup_replies: bool = False
    mac_test_signup_reply_until: str = ""

    @property
    def gloo_base_url(self) -> str:
        return f"https://platform.ai.gloo.com/ai/v2/{self.gloo_endpoint}"

    @property
    def sms_is_live(self) -> bool:
        """True only when the human has explicitly enabled real SMS."""
        return self.sms_provider == "twilio" and self.live_sms


def settings_from_env() -> Settings:
    return Settings(
        gloo_api_key=_env_str("GLOO_API_KEY"),
        gloo_endpoint=_env_str("GLOO_ENDPOINT", "guarded"),
        agent_model=_env_str("AGENT_MODEL", "gloo-anthropic-claude-sonnet-4.6"),
        parser_model=_env_str("PARSER_MODEL", "gloo-openai-gpt-5-mini"),
        max_agent_steps=_env_int("MAX_AGENT_STEPS", 15),
        sms_provider=_env_str("SMS_PROVIDER", "mock"),
        live_sms=_env_bool("LIVE_SMS", False),
        twilio_account_sid=_env_str("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_env_str("TWILIO_AUTH_TOKEN"),
        twilio_from_number=_env_str("TWILIO_FROM_NUMBER"),
        google_calendar_id=_env_str("GOOGLE_CALENDAR_ID"),
        google_service_account_json=_env_str("GOOGLE_SERVICE_ACCOUNT_JSON"),
        church_timezone=_env_str("CHURCH_TIMEZONE", "America/Denver"),
        admin_password=_env_str("ADMIN_PASSWORD"),
        demo_mode=_env_bool("DEMO_MODE", True),
        public_base_url=_env_str("PUBLIC_BASE_URL"),
        database_url=_env_str("DATABASE_URL", "sqlite:///./servfrictionless.db"),
        allow_text_signup=_env_bool("ALLOW_TEXT_SIGNUP", False),
        supabase_url=_env_str("SUPABASE_URL"),
        supabase_publishable_key=_env_str("SUPABASE_PUBLISHABLE_KEY"),
        admin_email_allowlist=_env_str("ADMIN_EMAIL_ALLOWLIST"),
        backend_bridge_key=_env_str("BACKEND_BRIDGE_KEY"),
        admin_site_url=_env_str("ADMIN_SITE_URL", "http://127.0.0.1:8000/texty"),
        mac_bridge_enabled=_env_bool("MAC_BRIDGE_ENABLED", False),
        mac_bridge_token=_env_str("MAC_BRIDGE_TOKEN"),
        mac_demo_phones=_env_str("MAC_DEMO_PHONES"),
        gloo_signup_replies=_env_bool("GLOO_SIGNUP_REPLIES", False),
        mac_test_signup_reply_until=_env_str("MAC_TEST_SIGNUP_REPLY_UNTIL"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return settings_from_env()
