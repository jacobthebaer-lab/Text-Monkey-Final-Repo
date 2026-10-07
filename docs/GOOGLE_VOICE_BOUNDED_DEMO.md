# Historical Google Voice proof internals

All automated Google Voice access is permanently held by project policy and the provider's Acceptable Use Policy. This includes scheduled transport, individually triggered steps, bounded conversation windows and continuous signup. `GOOGLE_VOICE_DEMO_MODE`, `GOOGLE_VOICE_SIGNUP_ENABLED`, ID verification, cookie import, exact-text review and superadmin approval cannot release the hold. Manual Google Voice use is separate and retains personal-phone forwarding off.

The shipped policy rejects historical demo activation. The connector client holds before HTTP or socket access, and the provider, send gate, queue dispatcher, worker and authenticated controls retain their policy checks. Existing queued messages cannot establish permission to submit a new text.

## Disconnected proof coverage

Historical internals remain for offline regression coverage: recipient and sender identity, name-reply consent, STOP, quiet hours, source freshness, exact body hashes, Gloo composition receipts, no em dashes, approval evidence, durable budgets and uncertainty barriers. Tests exercise those internals only with an explicit synthetic policy replacement, fake adapters and denied actual HTTP/socket access. Separate tests exercise the shipped policy without replacing its decision across every historical flag combination and the connector's request methods.

Passing those tests does not establish a working Google session, carrier delivery, competition certification or authorization to automate Google Voice. A queued or submitted status is not a device delivery receipt.

## Supported simulation and transport

For cloud controls without credentials or delivery, run:

```sh
python3 tools/texty_local_demo.py --port 58127
```

Open `http://127.0.0.1:58127/cloud-preview`. This disconnected simulation cannot send texts. Generic cloud backend preparation remains separate from transport activation.

Use the existing authorized Mac-connected demo for real delivery checks under its current consent, session, review and scheduling requirements. Future production transport is registered Twilio with a dedicated number per church; registration and provisioning require separate authorization. This document authorizes no runtime, account or transport change.
