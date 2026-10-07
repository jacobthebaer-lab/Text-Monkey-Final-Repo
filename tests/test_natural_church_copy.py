"""Fresh church copy uses natural labels while canonical source stays unchanged."""
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from app.core.church_labels import church_label
from app.core import onboarding, schedule_messages, reminders
from app.core.onboarding_copy import role_options
from app.core.signup_responder import compose_signup_reply
from app.config import Settings
from app.db import models as m


def test_role_question_and_parsed_reply_keep_original_catalog_ids(session,clock,gate,make_volunteer,make_shift):
    shift=make_shift('Synthetic Greeter')
    person=make_volunteer('Alex Example')
    calls=[]
    def create(**kwargs):
        facts=json.loads(kwargs['input']);calls.append(facts)
        text=json.dumps({'understood':True,'sensitive':False,'role_ids':[shift.role_id],'any_role':False}) if facts.get('stage')=='interests' else facts['approved_message']
        return SimpleNamespace(output_text=text)
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),create_response=create)
    original=(shift.role.id,shift.role.name,shift.role.required_qualifications)
    assert f'{shift.role_id}: Greeter' in role_options(session)
    onboarding.start(session,clock,gate,person,gloo)
    assert 'Synthetic' not in calls[0]['approved_message']
    assert onboarding.handle(session,clock,gate,person,'Greeter',gloo)=='onboarding_availability'
    assert person.preferences['interested_roles']==['Synthetic Greeter']
    assert (shift.role.id,shift.role.name,shift.role.required_qualifications)==original


def test_schedule_drafts_and_required_phrase_share_natural_labels(session,clock,make_shift,make_volunteer):
    shift=make_shift('Synthetic Greeter');shift.event.title='Demo: Sunday Service'
    person=make_volunteer('Alex Example')
    assignment=m.Assignment(shift_id=shift.id,volunteer_id=person.id,shift=shift,volunteer=person,status='confirmed',source='planner',created_at=clock.now(),updated_at=clock.now())
    session.add(assignment);session.flush()
    source=reminders.assignment_source(assignment,'confirmation')
    before=json.dumps(source,sort_keys=True)
    body=schedule_messages.confirmation_copy(assignment,ZoneInfo('America/Denver'))
    assert 'Synthetic' not in body and 'Demo:' not in body and 'Greeter at Sunday Service' in body
    assert 'signed up to greet tomorrow' in reminders.day_before_copy(assignment,ZoneInfo('America/Denver'))
    calls=[]
    def create(**kwargs):
        facts=json.loads(kwargs['input']);calls.append(facts)
        return SimpleNamespace(output_text=facts['approved_message'])
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),create_response=create)
    assert schedule_messages.compose(session,clock,gloo,body,person,{'assignment':source})==body
    assert calls[0]['required_phrases']==[body]
    assert json.dumps(reminders.assignment_source(assignment,'confirmation'),sort_keys=True)==before
    assert source['role_name']=='Synthetic Greeter' and source['event_title']=='Demo: Sunday Service'


def test_exact_review_body_is_not_normalized(session,clock,make_volunteer):
    person=make_volunteer();body='Exact approved test text. Synthetic Greeter.'
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),create_response=lambda **kw:SimpleNamespace(output_text=json.loads(kw['input'])['approved_message']))
    assert compose_signup_reply(session,clock,gloo,body,volunteer=person,require_gloo=True,exact_copy=True)==body
    assert church_label('Testament ministry')=='Testament ministry'
    assert church_label('Synthetic Greeter')=='Greeter'
    assert church_label('Church (Synthetic)')=='Church'


def test_natural_role_reply_keeps_cancellation_unique_and_ambiguous_aliases_held(clock):
    from app.core.cancellation_scope import explicit_target
    def booking(role):
        return SimpleNamespace(shift=SimpleNamespace(role=SimpleNamespace(name=role),starts_at=clock.now()))
    greeter,production=booking('Synthetic Greeter'),booking('Synthetic Production')
    day=clock.now().strftime('%A')
    assert explicit_target([greeter,production],f'Cancel Greeter on {day}',ZoneInfo('UTC')) is greeter
    assert explicit_target([greeter,booking('Greeter')],f'Cancel Greeter on {day}',ZoneInfo('UTC')) is None
