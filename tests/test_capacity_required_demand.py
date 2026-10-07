"""Capacity respects required demand and coordinator review; synthetic only."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.agents.capacity_agent import scan
from app.agents.fill_agent import FillContext
from app.config import Settings
from app.db import models as m
from app.main import create_app
from tests.test_capacity_narration import NarrationGloo, ctx

@pytest.mark.parametrize('kind,criticality,minimum,filled,extra,expected',[
 ('optional_without_demand','optional',None,False,False,False),
 ('zero_demand','standard',0,False,False,False),
 ('required_filled_with_extra_slot','standard',1,True,True,False),
 ('required_underfilled','standard',1,False,False,True),
 ('optional_explicit_required_minimum','optional',1,False,False,True),
])
def test_chronic_gaps_follow_real_required_demand(session,clock,provider,make_shift,make_volunteer,assign,tmp_path,kind,criticality,minimum,filled,extra,expected):
 typ=m.EventType(name='Fictional gathering',title_patterns=['Gathering']);session.add(typ);session.flush()
 person=make_volunteer()
 for days in (3,10,17):
  slot=make_shift('Greeter',criticality=criticality,starts=clock.now()-timedelta(days=days));slot.event.status='completed';slot.event.event_type_id=typ.id
  if extra:session.add(m.Shift(event_id=slot.event_id,role_id=slot.role_id,slot_index=1))
  if filled:assign(person,slot,status='completed')
 if minimum is not None:session.add(m.RoleRecipe(event_type_id=typ.id,role_id=slot.role_id,count=minimum))
 role_id=slot.role_id;session.commit();session.expunge_all()
 flags=scan(ctx(session,clock,provider,tmp_path));gaps=[f for f in flags if f.type=='chronic_gap' and f.evidence['role_id']==role_id]
 assert bool(gaps)==expected
 if expected:assert len(gaps)==1 and len(gaps[0].evidence['shift_ids'])==3
 assert not provider.sent and session.scalar(select(m.Message)) is None

@pytest.mark.parametrize('action,status',[('accept','accepted'),('dismiss','dismissed')])
def test_actual_flag_action_survives_unchanged_rescan_and_retains_history(clock,tmp_path,action,status):
 app=create_app(Settings(database_url='sqlite://',sms_provider='mock',demo_mode=True,automation_enabled=False,competition_confirmation_required=False,admin_password='fictional-private-password'))
 app.state.clock=clock;app.state.gloo=NarrationGloo()
 with app.state.session_factory() as s:
  s.add(m.Role(name='Greeter',ministry='Fictional',criticality='standard',fill_policy='auto'));s.commit()
  flags=scan(FillContext(s,clock,app.state.provider,app.state.gloo,log_dir=tmp_path));f=next(f for f in flags if f.type=='single_point_of_failure');identity=f.id;key=f.evidence['key'];s.commit()
 with TestClient(app) as client:
  assert client.post(f'/flags/{identity}/{action}',auth=('unauthorized','wrong-password'),follow_redirects=False).status_code==401
  with app.state.session_factory() as s:assert s.get(m.Flag,identity).status=='open'
  assert client.post(f'/flags/{identity}/{action}',auth=('admin','fictional-private-password'),follow_redirects=False).status_code==303
  page=client.get('/flags',auth=('admin','fictional-private-password'));assert page.status_code==200 and status in page.text
 with app.state.session_factory() as s:
  first=s.get(m.Flag,identity);old_status=first.status;assert old_status==status
  flags=scan(FillContext(s,clock,app.state.provider,NarrationGloo(),log_dir=tmp_path));s.commit()
  rows=s.scalars(select(m.Flag).where(m.Flag.type=='single_point_of_failure')).all()
  assert s.get(m.Flag,identity).status==status and len([r for r in rows if r.evidence['key']==key])==1
  if status=='dismissed':assert not any(f.evidence['key']==key for f in flags)
  else:assert next(f for f in flags if f.evidence['key']==key).id==identity
  assert s.scalar(select(func.count()).select_from(m.Message))==0
 assert not app.state.provider.sent


def test_dismissed_flag_only_reopens_for_changed_source_and_preserves_history(session,clock,provider,make_volunteer,make_shift,tmp_path):
 role=make_shift('Greeter').role
 first=next(f for f in scan(ctx(session,clock,provider,tmp_path)) if f.type=='single_point_of_failure')
 first.status='dismissed';session.commit();original=dict(first.evidence);identity=first.id
 clock.advance(timedelta(minutes=10))
 flags=scan(ctx(session,clock,provider,tmp_path));assert not any(f.evidence['key']==original['key'] for f in flags)
 assert session.get(m.Flag,identity).evidence==original
 make_volunteer(prefs={'interested_roles':['Greeter']});session.commit()
 flags=scan(ctx(session,clock,provider,tmp_path));new=next(f for f in flags if f.evidence['key']==original['key'])
 assert new.id!=identity and new.status=='open' and new.evidence['interested_qualified']==1
 assert session.get(m.Flag,identity).status=='dismissed' and session.get(m.Flag,identity).evidence==original
 assert not provider.sent and session.scalar(select(m.Message)) is None


def test_proactive_lookahead_exact_events_and_exclusive_eight_week_boundary_after_advance(session,clock,provider,make_volunteer,make_shift,tmp_path):
 from datetime import timezone
 clock.set_time(clock.now().astimezone(timezone.utc));start=clock.now()
 person=make_volunteer(prefs={'interested_roles':['Greeter'],'max_per_month':1})
 cases={name:make_shift('Greeter',starts=when) for name,when in [
  ('past',start-timedelta(seconds=1)),('now',start),('four',start+timedelta(weeks=4)),
  ('before_eight',start+timedelta(weeks=8,seconds=-1)),('eight',start+timedelta(weeks=8)),
  ('ten',start+timedelta(weeks=10)),('twelve',start+timedelta(weeks=12)),
  ('cancelled',start+timedelta(weeks=5))]}
 cases['cancelled'].event.status='cancelled';session.commit();session.expire_all()
 def source_for(names):
  flags=scan(ctx(session,clock,provider,tmp_path));f=next(f for f in flags if f.type=='growing_need')
  assert f.evidence['slots']==len(names) and f.evidence['capacity']==2
  assert f.evidence['future_shift_ids']==sorted(cases[n].id for n in names)
  assert f.evidence['future_event_ids']==sorted(cases[n].event_id for n in names)
  assert f.evidence['narration']['state']=='ready'
  return f
 first=source_for(['now','four','before_eight']);identity=first.id
 clock.advance(timedelta(weeks=4));second=source_for(['four','before_eight','eight','ten'])
 assert second.id==identity and second.evidence['scan_at']==clock.now().isoformat()
 person.preferences={**person.preferences,'max_per_month':2};session.commit()
 assert not any(f.type=='growing_need' for f in scan(ctx(session,clock,provider,tmp_path)))
 assert not provider.sent and session.scalar(select(m.Message)) is None and session.scalar(select(m.Approval)) is None


@pytest.mark.parametrize('complete',[False,True])
def test_chronic_gap_counts_required_split_position_once_and_keeps_uncovered_interval_ids(session,clock,provider,make_volunteer,make_shift,assign,tmp_path,complete):
 from tests.test_reviewed_split_coverage import prepare_partial,partition
 typ=m.EventType(name='Fictional split gathering',title_patterns=[]);session.add(typ);session.flush()
 uncovered=[]
 for _ in range(3):
  prepared=prepare_partial(session,clock,provider,make_shift,make_volunteer,tmp_path)
  children=partition(session,clock,prepared);prepared.parent.event.event_type_id=typ.id
  person=make_volunteer();assign(person,children[0],status='completed')
  if complete:assign(person,children[1],status='completed')
  else:uncovered.append(children[1].id)
 session.add(m.RoleRecipe(event_type_id=typ.id,role_id=prepared.parent.role_id,count=1))
 session.commit();clock.advance(timedelta(days=7));session.expunge_all()
 count=session.scalar(select(func.count()).select_from(m.Message))
 flags=scan(ctx(session,clock,provider,tmp_path));gaps=[f for f in flags if f.type=='chronic_gap']
 if complete:assert not gaps
 else:assert len(gaps)==1 and gaps[0].evidence['shift_ids']==sorted(uncovered)
 assert session.scalar(select(func.count()).select_from(m.Message))==count and not provider.sent


def test_dismissal_preserves_same_assignment_set_despite_legacy_list_order(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
 person=make_volunteer(prefs={'max_per_month':1})
 for days in (2,4,6):assign(person,make_shift('Greeter',starts=clock.now()-timedelta(days=days)),status='completed')
 flag=next(f for f in scan(ctx(session,clock,provider,tmp_path)) if f.type=='burnout')
 flag.status='dismissed';flag.evidence={**flag.evidence,'assignment_ids':list(reversed(flag.evidence['assignment_ids']))}
 session.commit();original=dict(flag.evidence);identity=flag.id;clock.advance(timedelta(minutes=10))
 assert not any(f.type=='burnout' and f.evidence['volunteer_id']==person.id for f in scan(ctx(session,clock,provider,tmp_path)))
 assert session.get(m.Flag,identity).status=='dismissed' and session.get(m.Flag,identity).evidence==original
 assert not provider.sent and session.scalar(select(m.Message)) is None
