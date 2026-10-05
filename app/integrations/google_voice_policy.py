"""Default Google Voice hold with a separate explicitly triggered demo candidate.

Jacob requires compliance with the competition/provider rules. Google's Voice
AUP forbids scripted messaging: https://support.google.com/voice/answer/9230450
Normal and scheduled transport remain held. A separately configured demo
permits explicit steps and an operator-started, expiring conversation window.
Offline tests can replace this function alongside their synthetic adapters.
"""
POLICY_HOLD_CODE = "provider_policy_hold"
POLICY_HOLD_MESSAGE = (
    "Google Voice prohibits automated texting. ID approval does not release this hold. "
    "Use Google Voice manually; registered Twilio is the planned production transport."
)


def google_voice_automation_allowed():
    return False


def google_voice_demo_allowed(settings):
    """Dedicated operator mode, separate from the disconnected UI demo flag."""
    return bool(getattr(settings, "google_voice_demo_mode", False) and
                settings.sms_provider == "google_voice" and settings.google_voice_enabled and
                not settings.demo_mode and not settings.automation_enabled and
                not settings.mac_bridge_enabled and not settings.profile_sync_enabled and
                not settings.pco_staffing_poll_enabled and not settings.pco_staffing_write_enabled and
                settings.competition_confirmation_required)


def google_voice_steps_allowed(settings):
    return google_voice_demo_allowed(settings) or google_voice_automation_allowed()
