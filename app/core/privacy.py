"""Keep recognized and durably flagged personal-care history out of model input."""
from sqlalchemy import select
from app.db import models as m
from app.llm.parser import keyword_sensitive


def safe_message_history(session, rows):
    rows = list(rows)
    if not rows:
        return []
    phones = {row.phone for row in rows}
    volunteers = {row.volunteer_id for row in rows if row.volunteer_id is not None}
    records = session.scalars(select(m.Escalation).where(m.Escalation.category == 'sensitive')).all()
    relevant = [record for record in records if record.related_ids.get('phone') in phones or
                record.related_ids.get('volunteer_id') in volunteers or
                any(record.related_ids.get('message_id') == row.id for row in rows)]
    def private(row):
        if keyword_sensitive(row.body):
            return True
        for record in relevant:
            ids = record.related_ids
            if ids.get('message_id') == row.id:
                return True
            # Old care rows lack a source ID. Conservatively omit earlier history
            # for that person; closing care does not grant data-export permission.
            if not ids.get('message_id') and row.created_at <= record.created_at and (
                    ids.get('phone') == row.phone or (ids.get('volunteer_id') is not None and ids.get('volunteer_id') == row.volunteer_id)):
                return True
        return False
    return [row for row in rows if not private(row)]
