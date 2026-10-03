"""Database-side provenance filtering for app-owned test history."""
from sqlalchemy import or_
from app.db import models as m


def scope(query, test_session):
    if test_session is None:
        return query
    return query.where(or_(m.Message.purpose == "test:"+test_session.id,
                           m.Message.provider_sid.startswith(test_session.outbound_prefix)),
                       m.Message.created_at >= test_session.starts_at,
                       m.Message.created_at < test_session.expires_at)
