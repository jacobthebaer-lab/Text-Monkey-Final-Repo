"""Database-side provenance filtering for app-owned test history."""
from sqlalchemy import or_
from app.db import models as m


def inbound_scope(test_session):
    # Earlier Mac sessions stored app-owned input as generic inbound. Google
    # input always has explicit provenance, even if a session ID is reused.
    kinds = ("google_voice_test_in",) if test_session.outbound_prefix.startswith("GV") else ("mac_test_in", "inbound")
    return (m.Message.purpose == "test:" + test_session.id) & m.Message.kind.in_(kinds)


def scope(query, test_session):
    if test_session is None:
        return query
    return query.where(or_(inbound_scope(test_session),
                           m.Message.provider_sid.startswith(test_session.outbound_prefix)),
                       m.Message.created_at >= test_session.starts_at,
                       m.Message.created_at < test_session.expires_at)
