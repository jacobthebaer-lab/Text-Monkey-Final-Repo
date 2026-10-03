"""Explicit synthetic test sessions; IDs identify fixtures, never real people."""
import hashlib
import json
from datetime import timedelta


def session_id(phone):
    return hashlib.sha256(("synthetic-test:"+phone).encode()).hexdigest()[:32]


def session_specs(phones, now):
    return {p: {"id": session_id(p), "starts_at": (now-timedelta(minutes=1)).isoformat(),
                "expires_at": (now+timedelta(minutes=59)).isoformat()} for p in phones}


def session_json(phones, now):
    return json.dumps(session_specs(phones, now))
