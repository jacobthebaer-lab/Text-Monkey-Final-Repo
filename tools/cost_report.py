"""Calculate measured model API cost using caller-supplied current billing rates."""
import argparse
import json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('report',type=Path,help='Synthetic eval JSON report')
p.add_argument('--input-per-million',type=float,required=True)
p.add_argument('--output-per-million',type=float,required=True)
p.add_argument('--monthly-cases',type=int,default=25,help='Assumed number of equivalent workflow cases per month')
a=p.parse_args()
if min(a.input_per_million,a.output_per_million,a.monthly_cases)<0:p.error('Rates and volume must be nonnegative')
rows=json.loads(a.report.read_text())
in_tokens=sum(r.get('usage',{}).get('input_tokens',0) for r in rows)
out_tokens=sum(r.get('usage',{}).get('output_tokens',0) for r in rows)
cost=(in_tokens*a.input_per_million+out_tokens*a.output_per_million)/1_000_000
print(json.dumps({'cases':len(rows),'input_tokens':in_tokens,'output_tokens':out_tokens,'observed_api_cost':round(cost,6),'api_cost_per_case':round(cost/len(rows),6) if rows else 0,'assumed_monthly_cases':a.monthly_cases,'assumed_monthly_api_cost':round(cost*a.monthly_cases/len(rows),6) if rows else 0,'excludes':['hosting','device','subscriptions','human handling'],'warning':'Aggregate blended rates are assumptions; use per-model billing for exact invoices.'},indent=2))
