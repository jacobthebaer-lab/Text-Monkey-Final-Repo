"""Google Voice is a manual-only demo number, never a live scripted transport.

Jacob requires compliance with the competition/provider rules. Google's Voice
AUP forbids scripted messaging: https://support.google.com/voice/answer/9230450
There is deliberately no environment or administrator override for this hold.
Offline tests can replace this function alongside their synthetic adapters.
"""
POLICY_HOLD_CODE = "provider_policy_hold"
POLICY_HOLD_MESSAGE = (
    "Google Voice prohibits automated texting. ID approval does not release this hold. "
    "Use Google Voice manually; registered Twilio is the planned production transport."
)


def google_voice_automation_allowed():
    return False
