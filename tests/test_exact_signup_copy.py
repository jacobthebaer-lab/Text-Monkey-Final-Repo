"""Original submitted copy, real inbound routing and mock-only delivery."""
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.db import models as m
from app.core.signup_copy import WELCOME, compose_welcome, ensure_exact_role_menu
from app.core.signup import finish_signup
from app.core.signup_responder import compose_signup_reply
from app.integrations.test_sessions import TestSession as RecipientSession
from app.llm.gloo_client import GlooUnavailableError
from app.config import Settings
from tests.test_concise_signup import ConciseGloo, PHONE, route

EXPECTED = [
    'Welcome to Text Monkey 🐵 Text us your FIRST and LAST name to sign up and receive scheduling texts. Message/data rates may apply🐒',
    'Thanks Alex! What would you like to help with? 1: Greeter, 2: Usher, 3: Production, 4: Coffee, 5: Child Care. Reply with names or numbers, or "Anything". Some roles need coordinator clearance.',
    'When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. You can also say "Flexible". 🐒 You can also tell me if you would like certain roles on certain dates or times. Just text me like you\'d text a person 🐵',
    "You're all set, Alex! We've saved your preferences. When a shift matches, we'll text you the details and ask if you can take it 🐵 Thanks for being willing to help out!",
]

class ExactGloo(ConciseGloo):
    def create_response(self, **kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,dict) and facts.get('exact_copy'):
            self.calls.append(facts)
            return SimpleNamespace(output_text=facts['approved_message'])
        return super().create_response(**kwargs)

@pytest.fixture
def exact(session):
    session.add(m.Policy(key='signup_exact_copy:'+PHONE,value={'value':True}))
    session.flush()
    return ExactGloo()

@pytest.mark.parametrize('name',['Alex Example','JOIN Alex Example','My name is Alex Example'])
def test_four_original_messages_name_only_no_deleted_steps(session,clock,provider,exact,name):
    assert route(session,clock,provider,'Hello',exact).routed_to=='signup_invitation'
    assert route(session,clock,provider,name,exact).routed_to=='onboarding_interests'
    assert route(session,clock,provider,'Anything',exact).routed_to=='onboarding_availability'
    assert route(session,clock,provider,'Sundays and Wednesdays all day',exact).routed_to=='onboarding_complete'
    assert [sent.body for sent in provider.sent]==EXPECTED
    assert all(all(word not in sent.body for word in ('YES','STOP','HELP')) for sent in provider.sent)
    person=session.scalar(select(m.Volunteer))
    assert person.sms_opt_in and person.preferences['consent_source']=='sms_name_reply_to_exact_invitation'
    assert person.preferences['availability_frequency_known'] is False
    assert 'max_per_month' not in person.preferences
    assert session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Qualification)) is None
    assert session.get(m.Role,5).fill_policy=='needs_approval'
    assert 'background_check' in session.get(m.Role,5).required_qualifications

@pytest.mark.parametrize('defect',['queued','uncertain','wrong_phone','stale','wrong_session','expired_session','future'])
def test_invalid_invitation_does_not_authorize_name(session,clock,provider,exact,defect):
    row=m.Message(phone=PHONE,direction='out',body=WELCOME,purpose='signup_reply',kind='ai',status='sent',created_at=clock.now())
    if defect in ('queued','uncertain'):row.status=defect
    if defect=='wrong_phone':row.phone='+12025550191'
    if defect=='stale':row.created_at=clock.now()-timedelta(hours=25)
    if defect=='future':row.created_at=clock.now()+timedelta(hours=1)
    if defect in ('wrong_session','expired_session'):
        start=clock.now()-timedelta(minutes=10)
        end=clock.now()+timedelta(minutes=10) if defect=='wrong_session' else clock.now()-timedelta(minutes=1)
        session.info['mac_test_session']=RecipientSession('a'*32,start,end)
        row.provider_sid='MAC'+'b'*32+':1'
    session.add(row);session.flush()
    assert route(session,clock,provider,'Alex Example',exact).routed_to=='signup_consent_pending'
    person=session.scalar(select(m.Volunteer))
    assert not person.sms_opt_in and person.preferences['consent_pending']
    assert all(sent.body==EXPECTED[0] for sent in provider.sent)

@pytest.mark.parametrize('status',['sent','submitted'])
def test_recorded_app_invitation_accepts_real_name_reply(session,clock,provider,exact,status):
    session.info['mac_test_session']=RecipientSession('a'*32,clock.now()-timedelta(minutes=1),clock.now()+timedelta(minutes=10))
    session.add(m.Message(phone=PHONE,direction='out',body=WELCOME,purpose='signup_reply',kind='ai',status=status,
        provider_sid='MAC'+'a'*32+':1',created_at=clock.now()))
    session.flush()
    assert route(session,clock,provider,'Alex Example',exact).routed_to=='onboarding_interests'
    assert provider.sent[-1].body==EXPECTED[1]

@pytest.mark.parametrize('reply',['YES','Anything','Someone Else','Alex Example said YES'])
def test_pending_does_not_invent_a_name_or_yes_reply(session,clock,provider,exact,reply):
    route(session,clock,provider,'Alex Example',exact)
    before=len(provider.sent)
    assert route(session,clock,provider,reply,exact).routed_to=='signup_consent_pending'
    assert len(provider.sent)==before
    assert not session.scalar(select(m.Volunteer)).sms_opt_in

def test_direct_finish_without_actual_inbound_record_does_not_activate(session,clock,gate,provider,exact,make_volunteer):
    person=make_volunteer(name='Alex Example',opt_in=False,status='inactive',prefs={'consent_pending':True})
    person.phone=PHONE
    session.add(m.Message(phone=PHONE,direction='out',body=WELCOME,purpose='signup_reply',kind='ai',status='sent',created_at=clock.now()))
    session.flush()
    assert finish_signup(session,clock,gate,person,'Alex Example',gloo=exact)=='signup_consent_pending'
    assert not person.sms_opt_in and not provider.sent

@pytest.mark.parametrize('change',[lambda text:text.replace('🐵','🙌'),lambda text:text+' Reply YES.',lambda text:text+' STOP for help.',lambda text:text.replace('apply🐒','apply. 🐒')])
def test_gloo_changed_literal_copy_fails_without_send(session,clock,provider,exact,change):
    exact.create_response=lambda **kwargs:SimpleNamespace(output_text=change(json.loads(kwargs['input'])['approved_message']))
    with pytest.raises(GlooUnavailableError):compose_welcome(session,clock,exact,PHONE)
    assert not provider.sent

def test_missing_gloo_cannot_fallback(session,clock,exact):
    with pytest.raises(GlooUnavailableError):compose_welcome(session,clock,None,PHONE)

@pytest.mark.parametrize('disabled',[False,True])
def test_exact_gloo_outage_never_uses_a_template(session,clock,provider,exact,disabled):
    exact.settings=Settings(gloo_signup_replies=not disabled)
    def unavailable(**kwargs):raise GlooUnavailableError('Synthetic outage')
    exact.create_response=unavailable
    with pytest.raises(GlooUnavailableError):compose_welcome(session,clock,exact,PHONE)
    assert not provider.sent

@pytest.mark.parametrize('when',['unknown','pending','active'])
def test_stop_dominates_exact_signup(session,clock,provider,exact,when):
    if when=='pending':route(session,clock,provider,'Alex Example',exact)
    if when=='active':
        route(session,clock,provider,'Hello',exact)
        route(session,clock,provider,'Alex Example',exact)
    assert route(session,clock,provider,'STOP',exact).routed_to=='stop'
    count=len(provider.sent)
    route(session,clock,provider,'Alex Example',exact)
    assert len(provider.sent)==count
    person=session.scalar(select(m.Volunteer))
    assert person is None or not person.sms_opt_in

@pytest.mark.parametrize('stage',['interests','availability'])
def test_ambiguity_goes_to_internal_review_without_deleted_clarification(session,clock,provider,exact,stage):
    route(session,clock,provider,'Hello',exact)
    route(session,clock,provider,'Alex Example',exact)
    if stage=='availability':route(session,clock,provider,'Anything',exact)
    original=exact.create_response
    def unclear(**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,dict) and 'stage' in facts:return SimpleNamespace(output_text='{"understood":false}')
        return original(**kwargs)
    exact.create_response=unclear
    count=len(provider.sent)
    assert route(session,clock,provider,'unclear',exact).routed_to=='onboarding_review'
    assert len(provider.sent)==count
    assert session.scalar(select(m.Escalation).where(m.Escalation.category=='unclear'))

def test_role_id_conflict_preserves_existing_clearance(session):
    role=m.Role(id=1,name='Music',ministry='Music',required_qualifications=['training'],fill_policy='needs_approval',criticality='standard')
    session.add(role);session.flush()
    with pytest.raises(ValueError):ensure_exact_role_menu(session)
    assert role.name=='Music' and role.required_qualifications==['training']

def test_editable_defaults_match_original_visible_copy_and_migrate_only_old_defaults():
    from app.core.onboarding_copy import DEFAULTS, PREVIOUS_DEFAULTS, INITIAL_DEFAULTS, render_copy, upgrade_saved_defaults
    assert [render_copy(DEFAULTS[key]) for key in ('welcome','interests','availability','completion')]==EXPECTED
    assert DEFAULTS['clarification']==''
    old={**PREVIOUS_DEFAULTS,'availability':'My custom availability question','clarification':''}
    upgraded=upgrade_saved_defaults(old)
    assert upgraded['interests']==DEFAULTS['interests']
    assert upgraded['availability']=='My custom availability question'
    assert upgraded['welcome']==EXPECTED[0]
    assert upgrade_saved_defaults(INITIAL_DEFAULTS)==DEFAULTS
