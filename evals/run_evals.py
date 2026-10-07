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

def snapshot(session, original, volunteers):
    assignments=list(session.scalars(select(m.Assignment)))
    fill=session.scalar(select(m.FillRequest).order_by(m.FillRequest.id.desc()))
    outgoing=list(session.scalars(select(m.Message).where(m.Message.direction=='out')))
    availability=session.scalar(select(m.Availability).where(m.Availability.volunteer_id==1))
    notices=list(session.scalars(select(m.Notification)))
    return {'state':fill.state if fill else None,'tranche':fill.current_tranche if fill else 0,
        'cancelled':sum(a.status=='cancelled' for a in assignments),
        'filled':sum(a.source=='fill' and a.status=='confirmed' for a in assignments),
        'pending_approval':any(a.status=='pending' for a in session.scalars(select(m.Approval))),
        'outreach_sent':sum(a.purpose=='outreach' for a in outgoing),
        'responses':[a.response for a in session.scalars(select(m.Outreach)) if a.response!='none'],
        'opt_in':volunteers[1].sms_opt_in,'stop_confirms':sum(a.purpose=='stop_confirm' for a in outgoing),
        'qualification_status':session.scalar(select(m.Qualification).where(m.Qualification.volunteer_id==1)).status,
        'available':availability.available_dates if availability else [],
        'unavailable_count':len(availability.unavailable_dates) if availability else 0,'original_status':original.status,
        'protected_bookings':sum(a.volunteer_id==1 and a.status=='approved' for a in assignments),
        'cancellation_ack_count':sum(n.purpose=='signup_reply' and n.state=='sent'
            and bool((n.detail.get('conversation') or {}).get('cancellation_reply')) for n in notices),
        'cancellation_held':any(n.purpose=='cancellation_scope' and n.state=='pending' for n in notices),
        'unknown_roster_count':len(session.scalars(select(m.Volunteer).where(m.Volunteer.phone=='+15550999999')).all()),
        'unknown_outbound_count':sum(a.phone=='+15550999999' for a in outgoing),
        'recipient_2_outreach_sent':sum(a.volunteer_id==2 and a.purpose=='outreach' for a in outgoing),
        'recipient_2_filled':sum(a.volunteer_id==2 and a.source=='fill' and a.status=='confirmed' for a in assignments),
        'recipient_2_qualification':session.scalar(select(m.Qualification).where(m.Qualification.volunteer_id==2)).status}

def check_expected(session, actual, expected):
    failures=[]
    for key,value in expected.items():
        if key=='escalation':ok=any(x.category==value for x in session.scalars(select(m.Escalation)))
        elif key=='severity':ok=any(x.severity==value for x in session.scalars(select(m.Escalation)))
        elif key=='no_reply_to':ok=not session.scalar(select(m.Message.id).where(m.Message.direction=='out',m.Message.volunteer_id==value))
        elif key=='purpose':ok=bool(session.scalar(select(m.Message.id).where(m.Message.direction=='out',m.Message.purpose==value)))
        elif key=='responses':ok=all(x in actual[key] for x in value)
        else:
            if key not in actual:raise ValueError(f'Unknown evaluation criterion: {key}')
            ok=actual[key]==value
        if not ok:failures.append(f'{key}: expected {value}, got {actual.get(key)}')
    return failures

class ReplayGloo:
    def __init__(self):self.tokens={'input_tokens':0,'output_tokens':0,'calls':0}
    def total_usage(self):return self.tokens
    def create_response(self,*,input,**kwargs):
        if isinstance(input,str):
            facts=json.loads(input)
            return NS(output=[NS(type='message')],output_text=facts.get('approved_message',''),usage=NS(input_tokens=0,output_tokens=0))
        if any(i.get('type')=='function_call_output' for i in input):return NS(output=[NS(type='message')],output_text='Done.',usage=NS(input_tokens=0,output_tokens=0))
        payload=json.loads(input[0]['content']);calls=[]
        members=payload.get('members',payload.get('candidates',[]))[:payload.get('max_candidates',999)]
        if 'candidates' in payload:
            calls.append(NS(type='function_call',call_id='choose',name='choose_replacements',arguments=json.dumps({'volunteer_ids':[v['volunteer_id'] for v in members],'reason':'Fixture choice from the constrained pool'})))
        for i,member in enumerate(members):
            calls.append(NS(type='function_call',call_id=f'ask{i}',name='request_send_text',arguments=json.dumps({'volunteer_id':member['volunteer_id'],'body':f"Hi {member['name']}! Could you cover {payload['shift']['role']} on {payload['shift']['invitation_label']}? Reply YES or NO. No worries if not."})))
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
        # Fixed one-hour demo offer policy exercises the frozen 61-minute expiry case.
        s.add(m.Policy(key='offer_response_window',value={'value':{'max_minutes':60,'min_minutes':2,'lead_time_divisor':6,'cutoff_minutes':10}}));s.flush()
        # This isolated MockSMSProvider fixture explicitly exercises outreach.
        # It grants no connected/native transport authorization.
        s.add(m.Policy(key='algorithm_outreach_enabled',value={'value':True}));s.flush()
        provider=MockSMSProvider();gloo=build_gloo() if live else ReplayGloo()
        if setup.get('gloo_failure'):gloo=NullGloo()
        ctx=FillContext(s,clock,provider,gloo,log_dir=log_dir/case['id'])
        role=m.Role(name='nursery' if setup.get('kids') else 'usher',ministry='synthetic',required_qualifications=['child_safety_training'] if setup.get('kids') else [],criticality='critical' if setup.get('kids') else 'standard',fill_policy='needs_approval' if setup.get('kids') else 'auto');s.add(role);s.flush()
        vols={}
        for i in range(1,12):
            v=m.Volunteer(id=i,name=f'Synthetic Volunteer {i}',phone=f'+1555010{i:04d}',sms_opt_in=not (i==1 and setup.get('opted_out')),status='inactive' if setup.get('no_candidates') and 2<=i<=9 else 'active',is_coordinator=i==10,is_pastor=i==11,preferences={'max_per_month':8},created_at=clock.now()-timedelta(days=100));s.add(v);s.flush();vols[i]=v
            s.add(m.Qualification(volunteer_id=i,type='child_safety_training',status='pending' if i==1 and setup.get('pending_original') else 'verified',verified_by='Synthetic Coordinator',verified_at=clock.now()-timedelta(days=30)))
        start=clock.now()+timedelta(hours=23)
        e=m.Event(title='Community Service',starts_at=start,ends_at=start+timedelta(hours=setup.get('duration_hours',1)),status='scheduled');s.add(e);s.flush()
        shift=m.Shift(event_id=e.id,role_id=role.id,slot_index=0);s.add(shift);s.flush()
        original=m.Assignment(shift_id=shift.id,volunteer_id=1,status='approved',source='planner',created_at=clock.now(),updated_at=clock.now());s.add(original);s.flush()
        if setup.get('ambiguous'):
            e2=m.Event(title='Community Service, later shift',starts_at=e.starts_at+timedelta(hours=3),ends_at=e.ends_at+timedelta(hours=3),status='scheduled');s.add(e2);s.flush();sh=m.Shift(event_id=e2.id,role_id=role.id,slot_index=0);s.add(sh);s.flush();s.add(m.Assignment(shift_id=sh.id,volunteer_id=1,status='approved',source='planner',created_at=clock.now(),updated_at=clock.now()))
        if setup.get('history'):
            for month in [8,9]:
                for day in ([2,16] if month==8 else [6,20]):
                    start=datetime(2026,month,day,9,tzinfo=ZoneInfo('America/Denver'));hist=m.Event(title='Historical Sunday',starts_at=start,ends_at=start+timedelta(hours=1),status='completed');s.add(hist);s.flush();sh=m.Shift(event_id=hist.id,role_id=role.id,slot_index=0);s.add(sh);s.flush();s.add(m.Assignment(shift_id=sh.id,volunteer_id=1,status='completed',source='planner',created_at=start,updated_at=start))
        if setup.get('unknown_event'):
            from app.integrations.gcal import sync
            service=NS(events=lambda:NS(list=lambda **kw:NS(execute=lambda:{'items':[{'id':'unknown','summary':'New festival','start':{'dateTime':e.starts_at.isoformat()},'end':{'dateTime':e.ends_at.isoformat()}}]})))
            sync(ctx,service,Settings(google_calendar_id='synthetic'))
        if setup.get('prior_consent'):
            # Explicit mock history: START restores previously disclosed consent,
            # never invents it for an undisclosed new recipient.
            from app.core.signup_copy import WELCOME
            when=clock.now()-timedelta(minutes=2)
            s.add(m.Message(direction='out',volunteer_id=1,phone=vols[1].phone,body=WELCOME,
                purpose='signup_reply',kind='ai',status='sent',provider_sid='MOCK-HISTORY',created_at=when))
            s.add(m.Message(direction='in',volunteer_id=1,phone=vols[1].phone,body=vols[1].name,
                kind='inbound',status='received',created_at=when+timedelta(seconds=1)))
            vols[1].preferences={**vols[1].preferences,'consent_source':'sms_name_reply_to_exact_invitation',
                'consent_at':(when+timedelta(seconds=1)).isoformat()}
        s.flush()
        traces=[];failures=[];checkpoints=[]
        parser=partial(parse_inbound,gloo) if live or setup.get('gloo_failure') else replay_parse
        for message in case['inbound']:
            if 'advance_minutes' in message:clock.advance(timedelta(minutes=message['advance_minutes']));__import__('app.jobs',fromlist=['process_due_fill_requests']).process_due_fill_requests(ctx)
            elif 'expire' in message:
                q=s.scalar(select(m.Qualification).where(m.Qualification.volunteer_id==message['expire']));q.status='expired';s.flush()
            else:
                ident=message['as'];phone=vols[ident].phone if ident in vols else '+15550999999'
                source=None
                if message.get('require_delivered_offer'):
                    outreach=s.scalar(select(m.Outreach).where(m.Outreach.volunteer_id==ident,
                        m.Outreach.message_id.is_not(None)).order_by(m.Outreach.id.desc()))
                    sent=s.get(m.Message,outreach.message_id) if outreach else None
                    verified=bool(sent and sent.volunteer_id==ident and sent.phone==phone
                        and sent.direction=='out' and sent.purpose=='outreach' and sent.status=='sent'
                        and any(receipt.sid==sent.provider_sid and receipt.to==phone and receipt.body==sent.body
                            for receipt in provider.sent))
                    if not verified:failures.append(f'Reply from volunteer {ident} has no actual mock-delivered invitation')
                    source={'verified_mock_send':verified,'outreach_id':outreach.id if outreach else None,
                            'message_id':sent.id if sent else None,'volunteer_id':ident,
                            'status':sent.status if sent else None}
                result=handle_inbound(s,clock,provider,phone,message['body'],parser,ctx=ctx)
                classification=result.parsed
                traces.append({'body':message['body'],'route':result.routed_to,'parsed':classification.intent if classification else None,
                    'parsed_fields':{'shift_hint':classification.shift_hint,'partial_window':classification.partial_window,
                        'confidence':classification.confidence,'parse_error':classification.parse_error,
                        'classification_source':(classification.raw or {}).get('classification_source')} if classification else None,
                    'notes':result.notes,'offer_source':source})
            s.flush()
            # Normal timer drainage releases due STOP/START notices, using the
            # same source/gate checks as the application. It is not a send bypass.
            from app.core.notifications import flush_due
            flush_due(ctx);s.flush()
            if 'expected' in message:
                current=snapshot(s,original,vols)
                errors=check_expected(s,current,message['expected'])
                failures.extend(f'step {len(checkpoints)+1}: {error}' for error in errors)
                checkpoints.append({'expected':message['expected'],'actual':current,'failures':errors})
        assignments=list(s.scalars(select(m.Assignment)))
        actual=snapshot(s,original,vols)
        failures.extend(check_expected(s,actual,case['expected']))
        # Universal invariant independent of the case's expected values.
        from app.core import scheduler
        report=scheduler.validate(s,'2026-10')
        # An original pending qualification is intentionally pre-existing in self_report.
        if not setup.get('pending_original') and any(a.source=='fill' for a in assignments) and report['violations']:failures.append('hard-rule violation after assignment')
        usage=gloo.total_usage();s.commit()
        return {'id':case['id'],'passed':not failures,'failures':failures,'expected':case['expected'],
            'fixture':{'starts_at':e.starts_at.isoformat(),'ends_at':e.ends_at.isoformat()},
            'actual':actual,'checkpoints':checkpoints,'trace':traces,'usage':usage,
            'mock_provider_sends':len(provider.sent)}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--live',action='store_true');ap.add_argument('--env-file');ap.add_argument('--workers',type=int,default=1);ap.add_argument('--case');ap.add_argument('--corpus',choices=('current','frozen'),default='current');args=ap.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file,override=False);get_settings.cache_clear()
    if args.live and not get_settings().gloo_api_key:raise SystemExit('Configure GLOO_API_KEY privately before live evals.')
    cases=json.loads((ROOT/'evals/cases'/('workflows.yaml' if args.corpus=='current' else 'workflows-v1-frozen.yaml')).read_text())
    if args.case:cases=[c for c in cases if c['id']==args.case]
    if not cases:raise SystemExit('No matching cases')
    stamp=datetime.now(ZoneInfo('America/Denver')).strftime('%Y%m%d-%H%M%S-%f');mode='live' if args.live else 'replay';log_dir=ROOT/'evals/reports'/f'{stamp}-{mode}-logs'
    def work(c):
        try:r=execute(c,args.live,log_dir)
        except Exception as exc:r={'id':c['id'],'passed':False,'failures':[f'{type(exc).__name__}: {exc}'],'usage':{}}
        print(f"{r['id']}: {'PASS' if r['passed'] else 'FAIL'}",flush=True);return r
    with ThreadPoolExecutor(max_workers=max(1,min(args.workers,4))) as pool:results=list(pool.map(work,cases))
    passed=sum(r['passed'] for r in results);tokens={k:sum(r['usage'].get(k,0) for r in results) for k in ('input_tokens','output_tokens','calls')}
    title=f'# {mode.title()} workflow evaluation\n\n{passed}/{len(results)} passed. Corpus: {args.corpus}. All data synthetic; all delivery uses MockSMSProvider.\n\n'
    title+=('Real Gloo classification and tool calls. ' if args.live else 'Deterministic fixture replay; not a model benchmark. ')+f'Token totals: {tokens}.\n\n'
    title+='| Case | Result | Failure |\n|---|---|---|\n'+''.join(f"| {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | {'; '.join(r['failures']).replace('|','/')} |\n" for r in results)
    directory=ROOT/'evals/reports';directory.mkdir(parents=True,exist_ok=True);(directory/f'{stamp}-{mode}.md').write_text(title);(directory/f'{stamp}-{mode}.json').write_text(json.dumps(results,indent=2,default=str));print(f'{passed}/{len(results)} passed; report: evals/reports/{stamp}-{mode}.md')
    raise SystemExit(0 if passed==len(results) else 1)

if __name__=='__main__':main()
