"""Text-only profile setup: Gloo interprets; code validates and saves facts."""
import json
from datetime import date
from pathlib import Path
from sqlalchemy import select
from app.db import models as m
from app.core.signup_responder import compose_signup_reply
from app.llm.parser import _extract_json, keyword_sensitive
from app.llm.agent_loop import RunLogger
from app.llm.gloo_client import GlooUnavailableError
from app.core.care import escalate_sensitive
from app.core.onboarding_copy import DEFAULTS, copy_key, preferred_wording, render_copy, role_options
from app.core.signup_copy import exact_enabled, exact_message, ensure_exact_role_menu
from app.core.signup_delivery import intake_context, send_intake

PROMPT = Path(__file__).resolve().parents[2] / "prompts/onboarding.md"


def availability_context(session, volunteer, today):
    draft = volunteer.preferences.get('onboarding_availability_draft')
    if draft is not None:
        return dict(draft)
    prefs = volunteer.preferences
    rows = session.scalars(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id)).all()
    result = {
        'availability_known': 'availability_weekdays' in prefs,
        'frequency_known': 'max_per_month' in prefs,
        'weekdays': prefs.get('availability_weekdays', []),
        'preferred_services': prefs.get('preferred_services', []),
        'all_day': prefs.get('availability_all_day', False),
        'max_per_month': prefs.get('max_per_month'),
        'available_dates': sorted({d for row in rows for d in (row.available_dates or [])
            if 0 <= (date.fromisoformat(d)-today).days <= 366}),
        'unavailable_dates': sorted({d for row in rows for d in (row.unavailable_dates or [])
            if 0 <= (date.fromisoformat(d)-today).days <= 366}),
    }
    if 'recurring_windows' in prefs:
        result['recurring_windows']=prefs['recurring_windows']
    if 'role_frequency_caps' in prefs:
        result['role_frequency_caps']=prefs['role_frequency_caps']
    return result


def validated_availability(data, previous, today, *, roles=(), event_types=()):
    fields = ('availability_known', 'frequency_known', 'weekdays', 'preferred_services',
              'all_day', 'max_per_month', 'available_dates', 'unavailable_dates')
    merged = {**previous, **{key: data[key] for key in fields if key in data}}
    # Older stored interpreter output supplied a complete frequency value.
    if 'frequency_known' not in data and type(data.get('max_per_month')) is int:
        merged['frequency_known'] = True
    if 'availability_known' not in data and 'weekdays' in data:
        merged['availability_known'] = True
    if 'recurring_windows' in data or 'recurring_windows' in previous:
        from app.core.recurring_availability import merge_recurring_windows
        merged['recurring_windows']=merge_recurring_windows(data,previous,roles,event_types)
        if merged['recurring_windows']:
            # Checked windows own their weekday/time/role scope. Never convert
            # an old range/group service enum into guessed hours.
            merged['availability_known']=True
            merged['preferred_services']=[]
    if 'role_frequency_caps' in data or 'role_frequency_caps' in previous:
        from app.core.recurring_availability import merge_role_frequency_caps
        merged['role_frequency_caps']=merge_role_frequency_caps(data,previous,roles)
    for key in ('availability_known', 'frequency_known', 'all_day'):
        if type(merged[key]) is not bool:
            raise ValueError('Availability flags must be boolean')
    days, services = merged['weekdays'], merged['preferred_services']
    if not isinstance(days, list) or not all(type(d) is int and 0 <= d <= 6 for d in days):
        raise ValueError('Invalid weekdays')
    if not isinstance(services, list) or not all(s in {f'sun_{h}' for h in range(24)} for s in services):
        raise ValueError('Invalid service hours')
    if merged['all_day'] and services:
        raise ValueError('All-day availability cannot restrict service hours')
    if merged['frequency_known'] and merged['max_per_month'] is None:
        # A boolean claim without a value cannot establish frequency. Keep an
        # already validated sender value, otherwise leave it unknown, while
        # retaining independently checked availability/windows.
        saved_frequency=previous.get('max_per_month')
        if previous.get('frequency_known') is True and type(saved_frequency) is int and 1<=saved_frequency<=8:
            merged['max_per_month']=saved_frequency
        else:
            merged['frequency_known']=False
    if merged['max_per_month'] is not None and not (type(merged['max_per_month']) is int and 1 <= merged['max_per_month'] <= 8):
        raise ValueError('Invalid serving frequency')
    if not merged['frequency_known']:
        merged['max_per_month'] = None
    for key in ('available_dates', 'unavailable_dates'):
        dates = merged[key]
        if not isinstance(dates, list) or len(dates) > 366 or not all(
            type(d) is str and 0 <= (date.fromisoformat(d)-today).days <= 366 for d in dates
        ):
            raise ValueError('Invalid availability dates')
        merged[key] = sorted(set(dates))
    merged['weekdays'], merged['preferred_services'] = list(dict.fromkeys(days)), list(dict.fromkeys(services))
    return merged


def availability_question(draft):
    if draft['availability_known'] and not draft['frequency_known']:
        return 'How often would you like to serve each month?'
    if draft['frequency_known'] and not draft['availability_known']:
        return 'Which days are you available to serve?'
    return 'Which days can you serve, and how often each month?'


def save_availability_dates(session, clock, volunteer, draft, previous, body):
    available, unavailable = draft['available_dates'], draft['unavailable_dates']
    months = {d[:7] for d in available+unavailable+previous['available_dates']+previous['unavailable_dates']}
    for month in sorted(months):
        row = session.scalar(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id,
            m.Availability.month == month).order_by(m.Availability.id.desc()))
        if row is None:
            row = m.Availability(volunteer_id=volunteer.id, month=month)
            session.add(row)
        # A correction can remove a previous exclusion; unioning would retain it.
        row.available_dates = [d for d in available if d.startswith(month)]
        row.unavailable_dates = [d for d in unavailable if d.startswith(month)]
        row.raw_reply, row.parsed_at = body, clock.now()


def prompt_for(session, stage, volunteer=None):
    if volunteer and exact_enabled(session, volunteer.phone):
        return exact_message(stage,volunteer.name.split()[0])
    if stage == "interests":
        text = render_copy(DEFAULTS[stage], roles=role_options(session),
                           first_name=volunteer.name.split()[0] if volunteer else "there")
        return text
    return DEFAULTS["availability"]


def compose_reply(session, clock, gloo, approved_message, volunteer, field):
    from app.core.conversational_signup import enabled
    if enabled(session, volunteer.phone, clock.now()):
        return compose_signup_reply(session, clock, gloo, approved_message, volunteer=volunteer,
            signup_conversation=True, require_gloo=True, exact_copy=False)
    if exact_enabled(session,volunteer.phone):
        return compose_signup_reply(session,clock,gloo,approved_message,volunteer=volunteer,
            signup_conversation=True,require_gloo=True,exact_copy=True)
    return compose_signup_reply(session, clock, gloo, approved_message, volunteer=volunteer,
        signup_conversation=True, require_gloo=True,
        preferred_wording=preferred_wording(session, field, volunteer), allow_emoji=field != 'clarification')


def missing_window_hours(window):
    return not window.get('all_day') and window.get('start_time') is None and window.get('time_mode')!='event'


def missing_frequency(saved,concise):
    scoped=bool(saved.get('role_frequency_caps')) or any(w.get('time_mode')=='event' for w in saved.get('recurring_windows',[]))
    return not saved['frequency_known'] and not scoped and not concise


def recover_preferences(session,clock,gate,gloo,volunteer,body,stage,saved,roles):
    from app.core.signup_recovery import redirect
    from app.core.conversational_signup import enabled, missing_facts
    if enabled(session, volunteer.phone, clock.now()):
        missing = missing_facts(saved, concise=volunteer.preferences.get('signup_minimal_texts') is True) if stage=='availability' else ['interests']
        coordinator = stage=='availability' and bool(saved.get('pending_constraints')) and not missing
        question = ('Please clarify the unresolved service times or group schedule.' if stage=='availability'
            else 'Which volunteer roles would you like to help with?')
        return redirect(session,clock,gate,gloo,phone=volunteer.phone,body=body,stage=stage,
            missing=[] if coordinator else missing or [stage],question='' if coordinator else question,
            saved=saved,volunteer=volunteer,conversational=True,needs_coordinator=coordinator)
    if stage=='interests':
        names=', '.join(role.name for role in roles)
        question=f'Which volunteer role would you like: {names}? You can also say "Anything".'
        missing=['interests']
    else:
        missing=[]
        if not saved['availability_known']:
            missing.append('availability')
        if missing_frequency(saved,volunteer.preferences.get('signup_minimal_texts') is True):
            missing.append('frequency')
        windows=saved.get('recurring_windows',[])
        unspecified=[w for w in windows if missing_window_hours(w)]
        if unspecified:
            missing.insert(0,'window_times')
        if 'window_times' in missing:
            weekdays=('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
            details=', '.join(f"{w.get('role_label') or 'volunteering'} on {weekdays[w['weekday']]}" for w in unspecified)
            question=f'What times can you help with {details}'
            if 'frequency' in missing:
                question+=', and how often would you like to serve each month'
            question+='?'
        elif missing==['frequency']:
            question='How often would you like to serve each month?'
        elif 'availability' in missing:
            question='Which days or dates can you serve? You can also say "Flexible".'
        else:
            # A malformed correction must not overwrite the already saved facts.
            missing=['availability_correction']
            question='What would you like to change about your availability?'
    return redirect(session,clock,gate,gloo,phone=volunteer.phone,body=body,stage=stage,
        missing=missing,question=question,saved=saved,volunteer=volunteer)


def start(session, clock, gate, volunteer, gloo, *, copy_owner=None):
    from app.core.confirmations import authorize_sender_fields
    authorize_sender_fields(session, volunteer, {"preferences"})
    if exact_enabled(session, volunteer.phone):
        ensure_exact_role_menu(session)
    volunteer.preferences = {**volunteer.preferences, "onboarding_stage": "interests", "signup_minimal_texts": True}
    if copy_owner is not None:
        # Only a verified administrator caller may supply this server-side ID.
        volunteer.preferences = {**volunteer.preferences, "onboarding_copy_owner": copy_key(copy_owner).removeprefix("onboarding_copy:")}
    else:
        # A fresh unbound start must not inherit another admin’s earlier copy.
        volunteer.preferences = {k: v for k, v in volunteer.preferences.items() if k != "onboarding_copy_owner"}
    return send_intake(session, clock, gate,compose=lambda: compose_reply(session, clock, gloo, prompt_for(session, "interests", volunteer), volunteer, "interests"),
              purpose="signup_reply", volunteer=volunteer,
              conversation=intake_context(session,volunteer.phone,'interests',['interests']))


def handle(session, clock, gate, volunteer, body, gloo, *, recorded_step_id=None):
    stage = volunteer.preferences.get("onboarding_stage")
    if stage not in {"interests", "availability"}:
        return None
    from app.core import conversational_signup as natural
    conversational = natural.enabled(session, volunteer.phone, clock.now())
    if conversational:
        original = natural.source(session, volunteer, gate.reply_to_message_id, clock.now())
        if original is None or original.body != body:
            return 'onboarding_review'
        if session.get(m.Notification, 'onboarding-turn:' + str(original.id)) is not None:
            if not session.info.get('mac_followup_recovery_key'):
                return 'onboarding_suppressed'
            from app.core.mac_followup_recovery import context_valid
            if not context_valid(session,volunteer,original.id,clock.now()):
                return 'onboarding_suppressed'
    if keyword_sensitive(body):
        escalate_sensitive(session, gate, volunteer, body, clock.now())
        return "escalated_sensitive"
    from app.core.signup_recovery import PRIVACY, redirect, reset_attempts, privacy_hold
    if privacy_hold(session,volunteer.phone,volunteer):
        return 'onboarding_review'
    if exact_enabled(session,volunteer.phone) and PRIVACY.search(body):
        return redirect(session,clock,gate,gloo,phone=volunteer.phone,body=body,
            stage=stage,missing=[],question='',volunteer=volunteer)
    roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
    from app.core.confirmations import authorize_sender_fields
    authorize_sender_fields(session, volunteer, {"preferences"})
    session.info["sender_profile_instruction"] = True
    logger = RunLogger(session, clock, agent="onboarding", trigger=f"Profile {stage}",
                       model=gloo.settings.parser_model if hasattr(gloo, "settings") else None)
    from app.config import get_settings
    settings = getattr(gloo, "settings", get_settings())
    previous = availability_context(session, volunteer, clock.now().date()) if stage == 'availability' else None
    event_types=session.scalars(select(m.EventType)).all() if stage=='availability' else []
    if conversational and stage=='availability' and session.info.get('onboarding_repair'):
        # The donor is a verified interpretation of this SAME actual input.
        # Preserve its independently valid restrictions while Gloo repairs the
        # malformed window. Never force a volunteer to repeat December/caps.
        previous = natural.partial_availability(session.info['onboarding_repair']['original_extraction'],
            previous,clock.now().date(),roles,event_types,actual_body=body)
    instructions=PROMPT.read_text()
    if stage=='availability':
        from app.core.recurring_availability import WINDOW_SCHEMA_INSTRUCTIONS
        instructions+='\n\n'+WINDOW_SCHEMA_INSTRUCTIONS
    context = natural.church_context(session, volunteer) if conversational else {}
    if conversational:
        instructions += '''\n\nThis is a conversational preference draft, not scheduling authorization.
Use verified_church_context for service ordinals: first/second service never
mean 1AM/2AM. Do not invent an end time or mapped group. Preserve exclusions
and role-specific caps. Return pending_constraints as a complete merged list
of {"kind":"same_day"|"service_time"|"event_mapping","description":"sender's unresolved restriction","role_ids":[known IDs]}.
Production on the same days as greeting MUST retain a same_day constraint;
the scheduler cannot yet represent that dependency, so leave it pending.
Unknown group day or event mapping MUST remain pending, never guess Sunday.
Retain prior pending restrictions unless this actual reply resolves or removes
them. This pending draft will be acknowledged naturally and clarified.
Historical invalid model proposals are evidence to repair, not facts to copy.
No assignments, PCO updates or qualifications have happened.'''
    try:
        if gloo is None:
            raise GlooUnavailableError('Gloo is required to interpret signup preferences')
        if recorded_step_id is not None:
            from app.core.conversation import scope
            source=session.get(m.AgentStep,recorded_step_id)
            incoming=session.scalar(scope(select(m.Message),session.info.get('mac_test_session')).where(
                m.Message.id==gate.reply_to_message_id,m.Message.direction=='in',
                m.Message.status=='received',m.Message.phone==volunteer.phone,m.Message.body==body))
            # This Python-only hook requires a trusted operator to verify the
            # private Gloo/native audit binding first. It has no HTTP/LLM tool.
            binding=session.info.get('verified_onboarding_source')
            if (incoming is None or source is None or source.run.agent!='onboarding'
                    or source.type!='decision' or not isinstance(source.result,dict)
                    or source.result.get('stage')!=stage
                    or not isinstance(source.result.get('extraction'),dict)
                    or binding!={'incoming_id':incoming.id,'step_id':source.id}):
                raise GlooUnavailableError('Recorded extraction lacks verified same-sender input binding')
            data=dict(source.result['extraction'])
            logger.step('decision',arguments={'source_step_id':source.id,'incoming_message_id':incoming.id},
                result={'stage':stage,'extraction':data})
        else:
            response = gloo.create_response(model=settings.parser_model, instructions=instructions,
            input=json.dumps({"stage": stage, "body": body, "today": clock.now().date().isoformat(),
                              "roles": [{"id": r.id, "name": r.name, "ministry": r.ministry} for r in roles],
                                  "saved_availability": previous,
                                  "saved_availability_source":('draft' if 'onboarding_availability_draft' in volunteer.preferences else 'saved_profile'),
                              "selected_roles":volunteer.preferences.get('interested_roles',[]),
                              "any_role":volunteer.preferences.get('any_role',False),
                              "event_types":[{'id':e.id,'name':e.name} for e in event_types],
                              **({'verified_church_context': context,
                                  'sender_history': natural.sender_history(session,volunteer,clock.now()),
                                  'repair_evidence': session.info.get('onboarding_repair')} if conversational else {})}))
            logger.add_usage(getattr(response, "usage", None))
            data = _extract_json(getattr(response, "output_text", "") or "") or {}
        # Some Gloo models wrap their result in the requested stage. Only that
        # known stage is read, and all fields still undergo the same validation.
        if isinstance(data.get(stage), dict):
            data = data[stage]
        if recorded_step_id is None:
            logger.step("decision", result={"stage": stage, "extraction": data,
                **({'incoming_message_id':gate.reply_to_message_id,
                    'repair_donor':session.info.get('onboarding_repair')} if conversational else {})})
        valid = data.get("understood") is True
        prefs = {**volunteer.preferences}
        if data.get("sensitive") is True:
            escalate_sensitive(session, gate, volunteer, body, clock.now(), severity=data.get("severity"))
            logger.close("sensitive")
            return "escalated_sensitive"
        if stage == "interests":
            ids = data.get("role_ids", [])
            valid = valid and isinstance(ids, list) and all(type(i) is int and i in {r.id for r in roles} for i in ids)
            valid = valid and (bool(ids) or data.get("any_role") is True)
            if valid:
                chosen = [r for r in roles if r.id in ids]
                prefs.update(interested_roles=[r.name for r in chosen],
                             any_role=data.get('any_role') is True,
                             preferred_ministry=", ".join(sorted({r.ministry for r in chosen})) or "Flexible",
                             onboarding_stage="availability")
        else:
            if valid:
                draft = (natural.partial_availability(data, previous, clock.now().date(), roles, event_types,actual_body=body)
                    if conversational else validated_availability(data,previous,clock.now().date(),roles=roles,event_types=event_types))
                if conversational:
                    draft = natural.retain_source_restrictions(draft,
                        [row['body'] for row in natural.sender_history(session,volunteer,clock.now())])
                if conversational and session.info.get('onboarding_repair'):
                    draft['unavailable_dates'] = sorted(set(draft['unavailable_dates']) | set(previous['unavailable_dates']))
                    caps = {cap['role_id']:cap for cap in draft.get('role_frequency_caps',[])}
                    caps.update({cap['role_id']:cap for cap in previous.get('role_frequency_caps',[])})
                    if caps:
                        draft['role_frequency_caps'] = list(caps.values())
                if body.strip().upper() in {'FLEXIBLE', 'SKIP'}:
                    # These commands relax recurring restrictions, not explicit exclusions.
                    draft['unavailable_dates'] = previous['unavailable_dates']
                # For new concise signups, frequency is optional. Preserve it as
                # unknown rather than inventing a preference or asking again.
                concise = prefs.get('signup_minimal_texts') is True
                windows=draft.get('recurring_windows',[])
                if windows:
                    # A newly stated role interest is still only an interest.
                    chosen_ids={role_id for window in windows for role_id in window['role_ids']}
                    prefs['interested_roles']=list(dict.fromkeys(prefs.get('interested_roles',[])+
                        [role.name for role in roles if role.id in chosen_ids]))
                if draft!=previous:
                    reset_attempts(session,volunteer.phone,stage)
                missing_times=any(missing_window_hours(w) for w in windows)
                if (not draft['availability_known'] or missing_frequency(draft,concise) or missing_times
                        or draft.get('pending_constraints')):
                    prefs.update(onboarding_availability_draft=draft)
                    prefs.pop('onboarding_clarifications', None)
                    volunteer.preferences = prefs
                    session.flush()
                    if conversational:
                        step = session.scalar(select(m.AgentStep).where(m.AgentStep.run_id==logger.run.id,
                            m.AgentStep.type=='decision').order_by(m.AgentStep.id.desc()))
                        natural.bind_turn(session,clock,volunteer,gate,stage,draft,step.id)
                    logger.close('partial_saved')
                    if conversational or exact_enabled(session,volunteer.phone):
                        return recover_preferences(session,clock,gate,gloo,volunteer,body,stage,draft,roles)
                    send_intake(session, clock, gate,compose=lambda: compose_reply(session, clock, gloo,
                        availability_question(draft), volunteer, 'clarification'),
                        purpose='signup_reply', volunteer=volunteer,
                        conversation=intake_context(session,volunteer.phone,stage,
                            ['availability'] if not draft['availability_known'] else ['frequency'],draft))
                    return 'onboarding_clarify'
                prefs.pop('onboarding_availability_draft', None)
                if 'recurring_windows' in draft:
                    prefs['recurring_windows']=draft['recurring_windows']
                if 'role_frequency_caps' in draft:
                    prefs['role_frequency_caps']=draft['role_frequency_caps']
                unmapped=[w for w in windows if w.get('time_mode')=='event' and not w['event_context']['event_type_ids']]
                if unmapped:
                    existing=session.scalars(select(m.Escalation).where(m.Escalation.category=='unknown_event',
                        m.Escalation.status.in_(('open','acknowledged')))).all()
                    if not any(row.related_ids.get('volunteer_id')==volunteer.id and row.related_ids.get('reason')=='event_availability_mapping' for row in existing):
                        session.add(m.Escalation(category='unknown_event',severity='normal',
                            summary='Event-following availability needs a coordinator to match the group schedule.',
                            related_ids={'volunteer_id':volunteer.id,'reason':'event_availability_mapping'},
                            status='open',created_at=clock.now()))
                prefs.update(availability_weekdays=draft['weekdays'], preferred_services=draft['preferred_services'],
                             availability_all_day=draft['all_day'], availability_frequency_known=draft['frequency_known'],
                             availability_note=body[:500], onboarding_stage="complete", onboarding_completed_at=clock.now().isoformat())
                if draft['frequency_known']:
                    prefs['max_per_month'] = draft['max_per_month']
                else:
                    prefs.pop('max_per_month', None)
                save_availability_dates(session, clock, volunteer, draft, previous, body)
        if not valid:
            raise ValueError("Profile extraction incomplete or invalid")
    except GlooUnavailableError:
        logger.close('gloo_unavailable')
        session.add(m.Escalation(category='system_error', severity='normal',
            summary=f'{volunteer.name} needs help finishing text signup because Gloo is unavailable.',
            related_ids={'volunteer_id': volunteer.id}, status='open', created_at=clock.now()))
        return 'onboarding_review'
    except (ValueError, TypeError) as exc:
        prefs = {**volunteer.preferences}
        attempts = prefs.get("onboarding_clarifications", 0)+1
        prefs["onboarding_clarifications"] = attempts
        volunteer.preferences = prefs
        logger.close("needs_clarification")
        if conversational:
            # Keep a failed interpretation auditable, but never treat it as a
            # validated scheduling fact. Its specific error owns the question.
            saved = dict(previous) if previous is not None else dict(prefs)
            saved['pending_constraints'] = [*saved.get('pending_constraints', []),
                {'kind':'validation', 'reason': str(exc)}]
            if stage=='availability':
                saved = natural.retain_source_restrictions(saved,
                    [row['body'] for row in natural.sender_history(session,volunteer,clock.now())])
            if stage=='availability':
                # logger.close flushed the previous JSON assignment. Assign a
                # fresh dictionary so the stored draft matches the bound turn
                # when a later native-delivery transaction reloads it.
                prefs = {**prefs, 'onboarding_availability_draft': saved}
            volunteer.preferences = prefs
            session.flush()
            step = session.scalar(select(m.AgentStep).where(m.AgentStep.run_id==logger.run.id,
                m.AgentStep.type=='decision').order_by(m.AgentStep.id.desc()))
            if step is None:
                return 'onboarding_review'
            natural.bind_turn(session,clock,volunteer,gate,stage,
                saved if stage=='availability' else volunteer.preferences,step.id)
            return recover_preferences(session,clock,gate,gloo,volunteer,body,stage,saved,roles)
        if exact_enabled(session,volunteer.phone):
            return recover_preferences(session,clock,gate,gloo,volunteer,body,stage,
                previous or {'interested_roles':prefs.get('interested_roles',[])},roles)
        if attempts > 1:
            if not prefs.get("onboarding_review_requested"):
                session.add(m.Escalation(category="unclear", severity="normal", summary=f"{volunteer.name} needs help finishing text signup.",
                    related_ids={"volunteer_id": volunteer.id}, status="open", created_at=clock.now()))
                volunteer.preferences = {**prefs, "onboarding_review_requested": True}
            return "onboarding_review"
        question = availability_question(previous) if stage == 'availability' else prompt_for(session, stage, volunteer)
        send_intake(session, clock, gate,compose=lambda: compose_reply(session, clock, gloo, question, volunteer,
            "clarification" if stage == "availability" else "interests"), purpose="signup_reply", volunteer=volunteer,
            conversation=intake_context(session,volunteer.phone,stage,[stage],previous))
        return "onboarding_clarify"
    prefs.pop("onboarding_clarifications", None)
    reset_attempts(session,volunteer.phone,stage)
    volunteer.preferences = prefs
    session.flush()
    logger.close("profile_saved")
    # The latest delivery policy completes preferences silently. Keep the
    # approved completion copy stored for editing, not automatic delivery.
    if stage == 'availability':
        if conversational:
            step = session.scalar(select(m.AgentStep).where(m.AgentStep.run_id==logger.run.id,
                m.AgentStep.type=='decision').order_by(m.AgentStep.id.desc()))
            natural.bind_turn(session,clock,volunteer,gate,'complete',volunteer.preferences,step.id)
            from app.core.signup_recovery import redirect
            return redirect(session,clock,gate,gloo,phone=volunteer.phone,body=body,stage=stage,
                missing=[],question='',saved=volunteer.preferences,volunteer=volunteer,
                conversational=True,complete=True)
        return 'onboarding_complete'
    reply = prompt_for(session, "availability", volunteer)
    send_intake(session, clock, gate,compose=lambda: compose_reply(session, clock, gloo, reply, volunteer,
        "availability"), purpose="signup_reply", volunteer=volunteer,
        conversation=intake_context(session,volunteer.phone,'availability',['availability']))
    return "onboarding_availability"
