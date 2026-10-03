"""25 hand-built workflow cases. JSON is the YAML-compatible case encoding.

--live uses real Gloo for classification and tool calls, never real SMS.
Default replay uses explicit deterministic fixtures and cannot prove model quality.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
import json
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.clock import FakeClock
from app.config import Settings, get_settings
from app.db import models as m
from app.agents.fill_agent import FillContext, advance_due
from app.core.inbound import handle_inbound
from app.llm.gloo_client import GlooUnavailableError, build_gloo, NullGloo
from app.llm.parser import ParsedMessage, parse_inbound, keyword_sensitive, keyword_self_harm
from app.sms.mock_provider import MockSMSProvider

ROOT=Path(__file__).resolve().parents[1]

class ReplayGloo:
    def __init__(self):self.tokens={'input_tokens':0,'output_tokens':0,'calls':0}
    def total_usage(self):return self.tokens
    def create_response(self,*,input,**kwargs):
        if any(i.get('type')=='function_call_output' for i in input):return NS(output=[NS(type='message')],output_text='Done.',usage=NS(input_tokens=0,output_tokens=0))
        payload=json.loads(input[0]['content']);calls=[]
        for i,member in enumerate(payload.get('members',[])):
            calls.append(NS(type='function_call',call_id=f'ask{i}',name='request_send_text',arguments=json.dumps({'volunteer_id':member['volunteer_id'],'body':f"Hi {member['name']}! Could you cover this shift? Reply YES or NO. No worries if not."})))
        calls.append(NS(type='function_call',call_id='timer',name='schedule_next_tranche',arguments='{}'))
        return NS(output=calls,output_text=None,usage=NS(input_tokens=0,output_tokens=0))

def replay_parse(text):
    t=text.lower();intent='unclear'
    if 'cant' in t or "can't" in t:intent='cancel'
    elif 'only til' in t:intent='partial'
    elif t.startswith('yes'):intent='accept'
    elif t.startswith('no sorry'):intent='decline'
    elif any(x in t for x in ('sundays','not this month','same as usual')):intent='availability'
    elif t=='c':intent='confirm'
    elif 'finished the safety training' in t:intent='other'
    return ParsedMessage(intent=intent,confidence=.95,sensitive=keyword_sensitive(text),severity='urgent' if keyword_self_harm(text) else 'normal')

def execute(case, live, log_dir):
    engine=create_engine('sqlite://');m.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine,expire_on_commit=False)() as s:
        setup=case['setup'];clock=FakeClock(datetime(2026,10,1,setup.get('hour',10),tzinfo=ZoneInfo('America/Denver')))
        provider=MockSMSProvider();gloo=build_gloo() if live else ReplayGloo()
        if setup.get('gloo_failure'):gloo=NullGloo()
        ctx=FillContext(s,clock,provider,gloo,log_dir=log_dir/case['id'])
        role=m.Role(name='nursery' if setup.get('kids') else 'usher',ministry='synthetic',required_qualifications=['child_safety_training'] if setup.get('kids') else [],criticality='critical' if setup.get('kids') else 'standard',fill_policy='needs_approval' if setup.get('kids') else 'auto');s.add(role);s.flush()
        vols={}
        for i in range(1,12):
            v=m.Volunteer(id=i,name=f'Synthetic Volunteer {i}',phone=f'+1555010{i:04d}',sms_opt_in=not (i==1 and setup.get('opted_out')),status='inactive' if setup.get('no_candidates') and 2<=i<=9 else 'active',is_coordinator=i==10,is_pastor=i==11,preferences={'max_per_month':10},created_at=clock.now()-timedelta(days=100));s.add(v);s.flush();vols[i]=v
            s.add(m.Qualification(volunteer_id=i,type='child_safety_training',status='pending' if i==1 and setup.get('pending_original') else 'verified',verified_by='Synthetic Coordinator',verified_at=clock.now()-timedelta(days=30)))
        e=m.Event(title='Sunday Service',starts_at=clock.now()+timedelta(hours=23),ends_at=clock.now()+timedelta(hours=24),status='scheduled');s.add(e);s.flush()
        shift=m.Shift(event_id=e.id,role_id=role.id,slot_index=0);s.add(shift);s.flush()
        original=m.Assignment(shift_id=shift.id,volunteer_id=1,status='approved',source='planner',created_at=clock.now(),updated_at=clock.now());s.add(original);s.flush()
        if setup.get('ambiguous'):
            e2=m.Event(title='Sunday Service 11:00',starts_at=e.starts_at+timedelta(hours=3),ends_at=e.ends_at+timedelta(hours=3),status='scheduled');s.add(e2);s.flush();sh=m.Shift(event_id=e2.id,role_id=role.id,slot_index=0);s.add(sh);s.flush();s.add(m.Assignment(shift_id=sh.id,volunteer_id=1,status='approved',source='planner',created_at=clock.now(),updated_at=clock.now()))
        if setup.get('history'):
            for month in [8,9]:
                for day in ([2,16] if month==8 else [6,20]):
                    start=datetime(2026,month,day,9,tzinfo=ZoneInfo('America/Denver'));hist=m.Event(title='Historical Sunday',starts_at=start,ends_at=start+timedelta(hours=1),status='completed');s.add(hist);s.flush();sh=m.Shift(event_id=hist.id,role_id=role.id,slot_index=0);s.add(sh);s.flush();s.add(m.Assignment(shift_id=sh.id,volunteer_id=1,status='completed',source='planner',created_at=start,updated_at=start))
        if setup.get('unknown_event'):
            from app.integrations.gcal import sync
            service=NS(events=lambda:NS(list=lambda **kw:NS(execute=lambda:{'items':[{'id':'unknown','summary':'New festival','start':{'dateTime':e.starts_at.isoformat()},'end':{'dateTime':e.ends_at.isoformat()}}]})))
            sync(ctx,service,Settings(google_calendar_id='synthetic'))
        s.flush()
        traces=[]
        parser=partial(parse_inbound,gloo) if live or setup.get('gloo_failure') else replay_parse
        for message in case['inbound']:
            if 'advance_minutes' in message:clock.advance(timedelta(minutes=message['advance_minutes']));advance_due(ctx)
            elif 'expire' in message:
                q=s.scalar(select(m.Qualification).where(m.Qualification.volunteer_id==message['expire']));q.status='expired';s.flush()
            else:
                ident=message['as'];phone=vols[ident].phone if ident in vols else '+15550999999'
                result=handle_inbound(s,clock,provider,phone,message['body'],parser,ctx=ctx)
                traces.append({'body':message['body'],'route':result.routed_to,'parsed':result.parsed.intent if result.parsed else None,'notes':result.notes})
            s.flush()
        assignments=list(s.scalars(select(m.Assignment)));fr=s.scalar(select(m.FillRequest).order_by(m.FillRequest.id.desc()));esc=list(s.scalars(select(m.Escalation)));out=list(s.scalars(select(m.Message).where(m.Message.direction=='out')));av=s.scalar(select(m.Availability).where(m.Availability.volunteer_id==1))
        actual={'state':fr.state if fr else None,'tranche':fr.current_tranche if fr else 0,'cancelled':sum(a.status=='cancelled' for a in assignments),'filled':sum(a.source=='fill' and a.status=='confirmed' for a in assignments),'pending_approval':any(a.status=='pending' for a in s.scalars(select(m.Approval))),'outreach_sent':sum(a.purpose=='outreach' for a in out),'responses':[a.response for a in s.scalars(select(m.Outreach)) if a.response!='none'],'opt_in':vols[1].sms_opt_in,'stop_confirms':sum(a.purpose=='stop_confirm' for a in out),'qualification_status':s.scalar(select(m.Qualification).where(m.Qualification.volunteer_id==1)).status,'available':av.available_dates if av else [],'unavailable_count':len(av.unavailable_dates) if av else 0,'original_status':original.status}
        failures=[]
        for key,value in case['expected'].items():
            if key=='escalation':ok=any(x.category==value for x in esc)
            elif key=='severity':ok=any(x.severity==value for x in esc)
            elif key=='no_reply_to':ok=not any(x.volunteer_id==value for x in out)
            elif key=='purpose':ok=any(x.purpose==value for x in out)
            elif key=='responses':ok=all(x in actual[key] for x in value)
            else:ok=actual.get(key)==value
            if not ok:failures.append(f'{key}: expected {value}, got {actual.get(key)}')
        # Universal invariant independent of the case's expected values.
        from app.core import scheduler
        report=scheduler.validate(s,'2026-10')
        # An original pending qualification is intentionally pre-existing in self_report.
        if not setup.get('pending_original') and any(a.source=='fill' for a in assignments) and report['violations']:failures.append('hard-rule violation after assignment')
        usage=gloo.total_usage();s.commit()
        return {'id':case['id'],'passed':not failures,'failures':failures,'actual':actual,'trace':traces,'usage':usage}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--live',action='store_true');ap.add_argument('--env-file');ap.add_argument('--workers',type=int,default=1);ap.add_argument('--case');args=ap.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file,override=False);get_settings.cache_clear()
    if args.live and not get_settings().gloo_api_key:raise SystemExit('Configure GLOO_API_KEY privately before live evals.')
    cases=json.loads((ROOT/'evals/cases/workflows.yaml').read_text())
    if args.case:cases=[c for c in cases if c['id']==args.case]
    if not cases:raise SystemExit('No matching cases')
    stamp=datetime.now(ZoneInfo('America/Denver')).strftime('%Y%m%d-%H%M%S');mode='live' if args.live else 'replay';log_dir=ROOT/'evals/reports'/f'{stamp}-{mode}-logs'
    def work(c):
        try:r=execute(c,args.live,log_dir)
        except Exception as exc:r={'id':c['id'],'passed':False,'failures':[f'{type(exc).__name__}: {exc}'],'usage':{}}
        print(f"{r['id']}: {'PASS' if r['passed'] else 'FAIL'}",flush=True);return r
    with ThreadPoolExecutor(max_workers=max(1,min(args.workers,4))) as pool:results=list(pool.map(work,cases))
    passed=sum(r['passed'] for r in results);tokens={k:sum(r['usage'].get(k,0) for r in results) for k in ('input_tokens','output_tokens','calls')}
    title=f'# {mode.title()} workflow evaluation\n\n{passed}/{len(results)} passed. All data synthetic; all delivery uses MockSMSProvider.\n\n'
    title+=('Real Gloo classification and tool calls. ' if args.live else 'Deterministic fixture replay; not a model benchmark. ')+f'Token totals: {tokens}.\n\n'
    title+='| Case | Result | Failure |\n|---|---|---|\n'+''.join(f"| {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | {'; '.join(r['failures']).replace('|','/')} |\n" for r in results)
    directory=ROOT/'evals/reports';directory.mkdir(parents=True,exist_ok=True);(directory/f'{stamp}-{mode}.md').write_text(title);(directory/f'{stamp}-{mode}.json').write_text(json.dumps(results,indent=2,default=str));print(f'{passed}/{len(results)} passed; report: evals/reports/{stamp}-{mode}.md')
    raise SystemExit(0 if passed==len(results) else 1)

if __name__=='__main__':main()
