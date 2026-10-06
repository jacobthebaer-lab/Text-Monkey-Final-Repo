"""Reviewed same-date role dependencies; never interprets free text or sends."""
from copy import deepcopy
import hashlib
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select
from app.db import models as m


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def normalize(session, pairs):
    if not isinstance(pairs, list) or len(pairs) > 4:
        raise ValueError('Planning rules require structured role pairs.')
    used, result = set(), []
    for pair in pairs:
        ids = pair.get('role_ids') if isinstance(pair, dict) and set(pair) == {'role_ids'} else None
        if (not isinstance(ids, list) or len(ids) != 2 or
                any(type(i) is not int or i <= 0 or session.get(m.Role, i) is None for i in ids)
                or ids[0] == ids[1] or used.intersection(ids)):
            raise ValueError('Choose disjoint pairs of two existing role IDs.')
        used.update(ids)
        result.append({'role_ids': sorted(ids)})
    return {'same_day_role_pairs': sorted(result, key=lambda p: p['role_ids'])}


def rules(session, volunteer):
    p = volunteer.preferences or {}
    return normalize(session, p.get('same_day_role_pairs', []))


def role_source(session, normalized):
    from app.core.confirmations import values
    return [{'id': i, **values(session.get(m.Role, i))}
            for i in sorted({i for p in normalized['same_day_role_pairs'] for i in p['role_ids']})]


def review_binding(session, volunteer, now):
    from datetime import timedelta
    binding = {'transport':session.info.get('conversation_origin','mock_or_twilio')}
    selected = session.info.get('mac_test_session')
    if selected:
        if not selected.active(now):
            raise ValueError('The selected exact review session expired.')
        binding.update(phone=volunteer.phone, session_id=selected.id, session_starts_at=selected.starts_at.isoformat(),
                       expires_at=selected.review_until(now).isoformat())
    return binding


def stage_rules(session, now, volunteer, *, pairs, resolved_constraint_indexes=()):
    """Coordinator chooses structured facts and exact pending items to resolve.

    Merely retaining a sender's description cannot create an executable rule.
    No consent, qualification, availability window or assignment is changed here.
    """
    from app.core import confirmations
    normalized = normalize(session, pairs)
    before = confirmations.values(volunteer)
    prefs = deepcopy(volunteer.preferences or {})
    pending = prefs.get('pending_constraints', [])
    if not isinstance(pending, list) or any(type(i) is not int or not 0 <= i < len(pending)
                                          for i in resolved_constraint_indexes):
        raise ValueError('Choose exact current pending constraint indexes for review.')
    for i in resolved_constraint_indexes:
        item = pending[i]
        ids = item.get('role_ids') if isinstance(item, dict) else None
        if (not isinstance(item, dict) or item.get('kind') != 'same_day'
                or not isinstance(ids, list) or any(type(ident) is not int for ident in ids)
                or not any(sorted(ids) == p['role_ids'] for p in normalized['same_day_role_pairs'])):
            raise ValueError('A pair review can only resolve its matching structured same-day constraint.')
    prefs['pending_constraints'] = [p for i, p in enumerate(pending) if i not in resolved_constraint_indexes]
    prefs.update(normalized)
    after = {**before, 'preferences': prefs}
    source = {'volunteer_id': volunteer.id, 'rules': normalized,
              'roles': role_source(session, normalized), 'before_hash': fingerprint(before)}
    return confirmations.stage(session, now, {'action':'record_change', 'record':'Volunteer',
        'record_id':volunteer.id, 'before':before, 'after':after,
        'reason':'Review explicit same-date role pairs; preserve all other participant facts and constraints.',
        'workflow_planning_rules': source, **review_binding(session, volunteer, now)}, record=True)


def rule_review_problem(session, approval):
    from app.core.confirmations import values
    source = approval.payload['workflow_planning_rules']
    person = session.get(m.Volunteer, source.get('volunteer_id'), populate_existing=True)
    try:
        normalized = normalize(session, source['rules']['same_day_role_pairs'])
        after = approval.payload['after']
        if (approval.payload['record'] != 'Volunteer' or approval.payload['record_id'] != person.id
                or fingerprint(values(person)) != source['before_hash']
                or role_source(session, normalized) != source['roles']
                or normalize(session, after['preferences'].get('same_day_role_pairs', [])) != normalized):
            return 'Planning rule source changed; request a fresh exact rule review.'
    except (AttributeError, KeyError, TypeError, ValueError):
        return 'Planning rule source changed; request a fresh exact rule review.'
    return None


def record_rule_receipt(session, approval):
    source = approval.payload['workflow_planning_rules']
    key = f"planning-rules:{source['volunteer_id']}"
    value = {'approval_id':approval.id, 'rules_hash':fingerprint(source['rules'])}
    receipt = session.get(m.Policy, key)
    if receipt is None:
        session.add(m.Policy(key=key, value=value))
    else:
        receipt.value = value


def rule_problem(session, volunteer):
    from app.core import confirmations
    if (volunteer.preferences or {}).get('pending_constraints'):
        return 'Stated scheduling constraints need exact coordinator review.'
    try:
        normalized = rules(session, volunteer)
    except (ValueError, TypeError):
        return 'Scheduling rules need valid existing role pairs.'
    if not normalized['same_day_role_pairs']:
        return None
    receipt = session.get(m.Policy, f'planning-rules:{volunteer.id}')
    approval = session.get(m.Approval, receipt.value.get('approval_id')) if receipt else None
    source = approval.payload.get('workflow_planning_rules', {}) if approval else {}
    if (not approval or approval.kind != 'confirm_record' or approval.status != 'approved'
            or approval.payload.get('applied_record_id') != volunteer.id
            or approval.payload.get('content_hash') != confirmations.digest(approval.payload)
            or source.get('volunteer_id') != volunteer.id or source.get('rules') != normalized
            or receipt.value.get('rules_hash') != fingerprint(normalized)
            or source.get('roles') != role_source(session, normalized)):
        return 'Scheduling rules have no current source-bound coordinator approval.'
    return None


def partner_role(session, volunteer, role_id):
    for pair in rules(session, volunteer)['same_day_role_pairs']:
        if role_id in pair['role_ids']:
            return next(i for i in pair['role_ids'] if i != role_id)
    return None


def eligibility_problem(session, volunteer, shift, tz, paired_shift_ids=()):
    if problem := rule_problem(session, volunteer):
        return problem
    zone = ZoneInfo(tz)
    local_date = shift.event.starts_at.astimezone(zone).date()
    partner = partner_role(session, volunteer, shift.role_id)
    if partner is None:
        return None
    for ident in paired_shift_ids:
        other = session.get(m.Shift, ident)
        if (other and other.role_id == partner and other.event.status == 'scheduled'
                and other.event.starts_at.astimezone(zone).date() == local_date
                and (other.event.ends_at <= shift.event.starts_at or other.event.starts_at >= shift.event.ends_at)):
            return None  # The enclosing pair validator checks both complete hard-rule sets.
    from app.core import eligibility
    for row in session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
            m.Assignment.volunteer_id == volunteer.id, m.Assignment.status.in_(('approved','confirmed')),
            m.Shift.role_id == partner, m.Event.status == 'scheduled')):
        if (row.shift.event.starts_at.astimezone(zone).date() == local_date and
                (row.shift.event.ends_at <= shift.event.starts_at or row.shift.event.starts_at >= shift.event.ends_at) and
                eligibility.check(session, volunteer, row.shift, tz, _exclude_assignment_id=row.id,
                                  _paired_shift_ids=(shift.id, row.shift_id))):
            return None
    return 'Required same-date partner role is not currently eligible and assigned.'


def pair_source(session, volunteer, shifts, month, tz):
    from app.core.scheduler import planning_source
    return {'month':month, 'timezone':tz, 'volunteer_id':volunteer.id,
            'shifts':[planning_source(s, volunteer, month) for s in sorted(shifts, key=lambda s:s.id)],
            'rules_hash':fingerprint(rules(session, volunteer))}


def stage_pair(session, now, volunteer, shifts, month, tz):
    from app.core import confirmations, scheduler
    shifts = sorted(shifts, key=lambda s:s.id)
    choices = [{'shift_id':s.id, 'volunteer_id':volunteer.id} for s in shifts]
    if (len(shifts) != 2 or len({s.event.starts_at.astimezone(ZoneInfo(tz)).date() for s in shifts}) != 1
            or not any(set(p['role_ids']) == {s.role_id for s in shifts} for p in rules(session, volunteer)['same_day_role_pairs'])
            or any(s.event.starts_at <= now or scheduler.preview_problem(session, volunteer, s, choices, tz) for s in shifts)):
        raise ValueError('Required same-date pair cannot pass current eligibility and role limits.')
    return confirmations.stage(session, now, {'action':'record_change', 'record':'AssignmentPair',
        'record_id':None, 'before':None, 'after':{'assignments':choices},
        'reason':'Publish these required placements together after Gloo review: ' + '; '.join(
            f"{s.role.name}, {s.event.starts_at.astimezone(ZoneInfo(tz)).isoformat()}" for s in shifts) + '.',
        'workflow_pair_source':pair_source(session, volunteer, shifts, month, tz),
        **review_binding(session, volunteer, now)}, record=True)


def apply_pair(session, approval, now):
    """One exact review publishes both assignments in the same transaction."""
    from app.core import scheduler
    from app.core.policies import PolicyStore
    source = approval.payload.get('workflow_pair_source', {})
    try:
        session.flush(); session.expire_all()
        person = session.get(m.Volunteer, source['volunteer_id'])
        shifts = [session.get(m.Shift, s['shift_id']) for s in source['shifts']]
        choices = [{'shift_id':s.id, 'volunteer_id':person.id} for s in shifts]
        normalized = rules(session, person)
        if (approval.payload.get('action') != 'record_change' or approval.payload.get('record_id') is not None
                or approval.payload.get('before') is not None
                or len(shifts) != 2 or source['timezone'] != str(PolicyStore(session).church_tz())
                or len({s.event.starts_at.astimezone(ZoneInfo(source['timezone'])).date() for s in shifts}) != 1
                or any(s.event.starts_at <= now for s in shifts)
                or not any(set(p['role_ids']) == {s.role_id for s in shifts} for p in normalized['same_day_role_pairs'])
                or pair_source(session, person, shifts, source['month'], source['timezone']) != source
                or approval.payload.get('after') != {'assignments':choices}
                or any(s.id not in {r.id for r in scheduler.shifts_for(session, source['month'], source['timezone'])} for s in shifts)):
            raise ValueError('Same-date pair source changed; request a fresh exact proposal.')
        for shift in shifts:
            if problem := scheduler.preview_problem(session, person, shift, choices, source['timezone']):
                raise ValueError('Same-date pair held: ' + problem)
    except (AttributeError, KeyError, TypeError):
        raise ValueError('Same-date pair source is incomplete; request a fresh exact proposal.') from None
    old = session.info.get('record_authorized')
    session.info['record_authorized'] = True
    try:
        rows = [m.Assignment(**c, status='approved', source='planner', created_at=now, updated_at=now) for c in choices]
        session.add_all(rows); session.flush()
        approval.payload = {**approval.payload, 'applied_record_ids':[r.id for r in rows]}
    finally:
        session.info['record_authorized'] = old
