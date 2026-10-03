"""Transport provenance shared by durable connectors and exact reviews."""


def transport_name(provider):
    return getattr(provider, "transport_name", "mac_messages" if hasattr(provider, "allows") else "mock_or_twilio")


def session_transport(provider):
    return transport_name(provider) in {"mac_messages", "google_voice"}


def queue_result(provider):
    return {"mac_messages": "queued_for_mac", "google_voice": "queued_for_google_voice"}.get(
        transport_name(provider), "simulated")
