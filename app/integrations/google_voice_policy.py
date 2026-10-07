"""Permanent Google Voice automation hold, including every historical demo mode.

Jacob requires compliance with the competition/provider rules. Google's Voice
AUP forbids scripted messaging: https://support.google.com/voice/answer/9230450
Settings, ID verification and operator approval cannot release this hold.
Disconnected tests may replace the decision only alongside synthetic adapters.
"""
POLICY_HOLD_CODE = "provider_policy_hold"
POLICY_HOLD_MESSAGE = (
    "Google Voice prohibits automated texting. ID approval does not release this hold. "
    "Use Google Voice manually; registered Twilio is the planned production transport."
)


def google_voice_automation_allowed():
    return False


def google_voice_demo_allowed(settings):
    """Historical demo settings never authorize automated Google Voice access."""
    return False


def google_voice_steps_allowed(settings):
    return google_voice_demo_allowed(settings) or google_voice_automation_allowed()
