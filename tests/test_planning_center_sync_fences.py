"""Independent repros on disposable synthetic databases, no external calls."""
from datetime import timedelta
import sqlite3
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from app.core.eligibility import check
from app.core.ranking import rank_candidates
from app.db.models import Volunteer,Event,FillRequest,Message,Policy,Shift,Role
from app.integrations.planning_center import PCOBase,PCOVolunteerPerson,PCOEventLink
from app.integrations.planning_center_staffing import refresh_staffing,process_staffing_outbox
from app.integrations.planning_center_sync import PREFIX,PROFILE_PREFIX,sync_linked_events,sync_mapped_names
from tests.test_planning_center_sync import linked,ProfileAPI
from tests.test_planning_center_staffing import setup_assignment,enqueue
from tests.test_planning_center import CONFIG


def test_event_pull_does_not_overwrite_edit_committed_during_remote_read(session,clock,linked):
 event,api,factory=linked;api.title='Native renamed event'
 old=api.request; edited=False
 def request(method,path,**kw):
  nonlocal edited
  if method=='GET' and not edited:
   edited=True
   with factory() as editor:
    editor.get(Event,event.id).title='Concurrent local title';editor.commit()
  return old(method,path,**kw)
 api.request=request
 report=sync_linked_events(factory,api,CONFIG,clock.now(),write_enabled=True)
 session.expire_all()
 assert event.title=='Concurrent local title',{'actual':event.title,'report':report}
 assert report['held']==1


def test_profile_pull_does_not_overwrite_edit_committed_during_remote_read(session,clock,make_volunteer):
 PCOBase.metadata.create_all(session.get_bind());person=make_volunteer(name='Fictional Person')
 session.add(PCOVolunteerPerson(organization_id='10',volunteer_id=person.id,person_id='70',created_at=clock.now()));session.commit()
 api=ProfileAPI(person);factory=sessionmaker(bind=session.get_bind(),expire_on_commit=False)
 assert sync_mapped_names(factory,api,CONFIG,clock.now())['baselined']==1
 api.name='Native Rename';old=api.request;edited=False
 def request(method,path,**kw):
  nonlocal edited
  if method=='GET' and path=='/people/v2/people/70' and not edited:
   edited=True
   with factory() as editor:
    editor.get(Volunteer,person.id).name='Concurrent Local';editor.commit()
  return old(method,path,**kw)
 api.request=request
 report=sync_mapped_names(factory,api,CONFIG,clock.now(),write_enabled=True)
 session.expire_all()
 assert person.name=='Concurrent Local',{'actual':person.name,'report':report}
 assert report['held']==1


def test_native_decline_excludes_original_sender_from_later_interval_ranking(session,clock,make_volunteer):
 PCOBase.metadata.create_all(session.get_bind())
 person,assignment,api,factory=setup_assignment(session,clock,make_volunteer)
 enqueue(session,assignment,clock);process_staffing_outbox(factory,api,CONFIG,clock.now(),enabled=True)
 session.expire_all();api.rows=[api.member(status='D')]
 refresh_staffing(session,api,CONFIG,clock.now(),service_type_id='20',plan_id='40')
 session.flush()
 assert assignment.status=='cancelled' and len(list(session.scalars(select(FillRequest))))==1
 assert not list(session.scalars(select(Message)))
 result=check(session,person,assignment.shift)
 assert not result,{'eligible':bool(result),'reasons':result.reasons,'ranked':[x.volunteer.id for x in rank_candidates(session,assignment.shift,clock.now())]}


def test_event_push_fences_edit_during_prewrite_read(session,clock,linked):
 event,api,factory=linked
 event.title='First local title';session.commit()
 old=api.request;reads=0
 def request(method,path,**kw):
  nonlocal reads
  if method=='GET':
   reads+=1
   if reads==2:
    with factory() as editor:
     editor.get(Event,event.id).title='Newer local title';editor.commit()
  return old(method,path,**kw)
 api.request=request
 assert sync_linked_events(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 session.expire_all()
 assert event.title=='Newer local title' and not api.writes
 assert session.get(Policy,PREFIX+'10:20:40:60').value['pending']['title']=='First local title'


def test_event_push_keeps_newer_local_edit_and_unknown_outcome_after_patch(session,clock,linked):
 event,api,factory=linked
 event.title='First local title';session.commit()
 old=api.request
 def request(method,path,**kw):
  result=old(method,path,**kw)
  if method=='PATCH':
   with factory() as editor:
    editor.get(Event,event.id).title='Newer local title';editor.commit()
  return result
 api.request=request
 assert sync_linked_events(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 session.expire_all()
 assert event.title=='Newer local title'
 assert session.get(Policy,PREFIX+'10:20:40:60').value['pending']['title']=='First local title'
 assert sync_linked_events(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 assert len(api.writes)==1


def test_event_pull_fences_new_baseline(session,clock,linked):
 event,api,factory=linked;api.title='Native renamed event'
 old=api.request;edited=False
 def request(method,path,**kw):
  nonlocal edited
  if method=='GET' and not edited:
   edited=True
   with factory() as editor:
    row=editor.get(Policy,PREFIX+'10:20:40:60')
    row.value={**row.value,'review_marker':'concurrent review'};editor.commit()
  return old(method,path,**kw)
 api.request=request
 assert sync_linked_events(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 session.expire_all()
 assert event.title=='Sunday Service'
 assert session.get(Policy,PREFIX+'10:20:40:60').value['review_marker']=='concurrent review'


def test_profile_push_fences_edit_during_prewrite_read(session,clock,make_volunteer):
 PCOBase.metadata.create_all(session.get_bind());person=make_volunteer(name='Fictional Person')
 mapping=PCOVolunteerPerson(organization_id='10',volunteer_id=person.id,person_id='70',created_at=clock.now())
 session.add(mapping);session.commit()
 api=ProfileAPI(person);factory=sessionmaker(bind=session.get_bind(),expire_on_commit=False)
 assert sync_mapped_names(factory,api,CONFIG,clock.now())['baselined']==1
 person.name='First Change';session.commit()
 old=api.request;reads=0
 def request(method,path,**kw):
  nonlocal reads
  if method=='GET' and path=='/people/v2/people/70':
   reads+=1
   if reads==2:
    with factory() as editor:
     editor.get(Volunteer,person.id).name='Newer Change';editor.commit()
  return old(method,path,**kw)
 api.request=request
 assert sync_mapped_names(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 session.expire_all()
 assert person.name=='Newer Change' and not api.writes
 assert session.get(Policy,PROFILE_PREFIX+str(person.id)).value['pending']=='First Change'


def test_profile_pull_fences_changed_identity(session,clock,make_volunteer):
 PCOBase.metadata.create_all(session.get_bind());person=make_volunteer(name='Fictional Person')
 mapping=PCOVolunteerPerson(organization_id='10',volunteer_id=person.id,person_id='70',created_at=clock.now())
 session.add(mapping);session.commit()
 api=ProfileAPI(person);factory=sessionmaker(bind=session.get_bind(),expire_on_commit=False)
 assert sync_mapped_names(factory,api,CONFIG,clock.now())['baselined']==1
 api.name='Native Rename';old=api.request;edited=False
 def request(method,path,**kw):
  nonlocal edited
  if method=='GET' and path=='/people/v2/people/70' and not edited:
   edited=True
   with factory() as editor:
    editor.get(PCOVolunteerPerson,mapping.id).person_id='71';editor.commit()
  return old(method,path,**kw)
 api.request=request
 assert sync_mapped_names(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 session.expire_all()
 assert person.name=='Fictional Person' and mapping.person_id=='71' and not api.writes


def test_native_refusal_persists_interval_allows_other_dates_and_exact_reversal(session,clock,make_volunteer):
 from app.core import cancellation_refusal as refusal
 PCOBase.metadata.create_all(session.get_bind())
 person,assignment,api,factory=setup_assignment(session,clock,make_volunteer)
 enqueue(session,assignment,clock);process_staffing_outbox(factory,api,CONFIG,clock.now(),enabled=True)
 session.expire_all();api.rows=[api.member(status='D')]
 refresh_staffing(session,api,CONFIG,clock.now(),service_type_id='20',plan_id='40');session.commit()
 with factory() as restarted:
  current=restarted.get(Volunteer,person.id);shift=restarted.get(Shift,assignment.shift_id)
  assert not check(restarted,current,shift)
  row=restarted.scalar(select(Policy).where(Policy.key.startswith(refusal.PREFIX)))
  assert row.value['facts']['source_kind']=='planning_center_transition'
  assert 'source_message_id' not in row.value['facts']
  proof=restarted.get(Policy,row.value['facts']['source_transition_key'])
  assert proof.value['proof']['before']['status']=='C' and proof.value['proof']['after']['status']=='D'
  event=Event(title='Other service',starts_at=shift.ends_at+timedelta(hours=1),ends_at=shift.ends_at+timedelta(hours=2),status='scheduled')
  restarted.add(event);restarted.flush()
  later=Shift(event_id=event.id,role_id=shift.role_id,slot_index=0);restarted.add(later);restarted.flush()
  assert check(restarted,current,later)
  original_start=shift.starts_at
  shift.event.starts_at+=timedelta(days=7);shift.event.ends_at+=timedelta(days=7)
  overlap=Event(title='Original interval',starts_at=original_start,ends_at=original_start+timedelta(minutes=30),status='scheduled')
  restarted.add(overlap);restarted.flush()
  sibling=Shift(event_id=overlap.id,role_id=shift.role_id,slot_index=0);restarted.add(sibling);restarted.flush()
  assert not check(restarted,current,sibling)
  restarted.info['record_authorized']=True
  refusal.revoke(restarted,row.key,expected=refusal.digest(row.value),actor='Coordinator',reason='Explicit availability correction',now=clock.now())
  assert check(restarted,current,sibling)
  assert not list(restarted.scalars(select(Message)))


@pytest.mark.parametrize('operation', ['GET', 'PATCH'])
def test_file_database_editor_can_commit_during_event_http(session,clock,linked,tmp_path,operation):
 from app.db.session import make_engine,make_session_factory
 event,api,unused=linked
 event.title='First local title';session.commit()
 path=tmp_path/'concurrent.sqlite'
 source=session.get_bind().raw_connection()
 with sqlite3.connect(path) as destination: source.driver_connection.backup(destination)
 source.close()
 engine=make_engine('sqlite:///'+str(path));factory=make_session_factory(engine)
 old=api.request;edited=False
 def request(method,url,**kwargs):
  nonlocal edited
  result=old(method,url,**kwargs)
  if method==operation and not edited:
   edited=True
   # A second physical connection must commit while HTTP is in progress.
   with sqlite3.connect(path,timeout=0.05) as writer:
    writer.execute('update events set title=? where id=?',('Concurrent committed title',event.id))
  return result
 api.request=request
 assert sync_linked_events(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 with factory() as reader: assert reader.get(Event,event.id).title=='Concurrent committed title'
 assert edited
 engine.dispose()


def test_profile_push_keeps_newer_edit_and_pending_outcome_after_patch(session,clock,make_volunteer):
 PCOBase.metadata.create_all(session.get_bind());person=make_volunteer(name='Fictional Person')
 session.add(PCOVolunteerPerson(organization_id='10',volunteer_id=person.id,person_id='70',created_at=clock.now()));session.commit()
 api=ProfileAPI(person);factory=sessionmaker(bind=session.get_bind(),expire_on_commit=False)
 sync_mapped_names(factory,api,CONFIG,clock.now())
 person.name='First Change';session.commit()
 old=api.request
 def request(method,path,**kw):
  result=old(method,path,**kw)
  if method=='PATCH':
   with factory() as editor:
    editor.get(Volunteer,person.id).name='Newer Change';editor.commit()
  return result
 api.request=request
 assert sync_mapped_names(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 session.expire_all()
 assert person.name=='Newer Change'
 assert session.get(Policy,PROFILE_PREFIX+str(person.id)).value['pending']=='First Change'
 assert sync_mapped_names(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 assert len(api.writes)==1


@pytest.mark.parametrize('target', ['mapping', 'local_identity'])
def test_event_pull_fences_changed_native_or_local_identity(session,clock,linked,target):
 event,api,factory=linked;api.title='Native renamed event'
 old=api.request;edited=False
 def request(method,path,**kw):
  nonlocal edited
  if method=='GET' and not edited:
   edited=True
   with factory() as editor:
    if target=='mapping': editor.get(PCOEventLink,'10:20:40:60').plan_id='41'
    else: editor.get(Event,event.id).gcal_event_id='local:unlinked'
    editor.commit()
  return old(method,path,**kw)
 api.request=request
 assert sync_linked_events(factory,api,CONFIG,clock.now(),write_enabled=True)['held']==1
 session.expire_all()
 assert event.title=='Sunday Service' and not api.writes


@pytest.mark.parametrize('mutation', ['proof', 'missing_time', 'state'])
def test_native_refusal_invalid_provenance_holds_for_source_review(session,clock,make_volunteer,mutation):
 from app.core import cancellation_refusal as refusal
 PCOBase.metadata.create_all(session.get_bind())
 person,assignment,api,factory=setup_assignment(session,clock,make_volunteer)
 enqueue(session,assignment,clock);process_staffing_outbox(factory,api,CONFIG,clock.now(),enabled=True)
 session.expire_all();api.rows=[api.member(status='D')]
 refresh_staffing(session,api,CONFIG,clock.now(),service_type_id='20',plan_id='40');session.flush()
 row=session.scalar(select(Policy).where(Policy.key.startswith(refusal.PREFIX)))
 source=session.get(Policy,row.value['facts']['source_transition_key'])
 if mutation=='proof':
  source.value={**source.value,'proof':{**source.value['proof'],'person_id':'999'}}
 elif mutation=='missing_time':
  source.value={k:v for k,v in source.value.items() if k!='observed_at'}
 else: row.value={**row.value,'state':'revoked'}
 session.flush()
 assert 'Prior service cancellation needs source review' in check(session,person,assignment.shift).reasons
