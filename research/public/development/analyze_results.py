"""Draft family/task/run paired estimator; no real study results supplied.
Input design lists every assigned primary-eligible run. Results may be missing.
Freeze this implementation and small-cluster policy before final admission.
"""
import argparse,json,math
from pathlib import Path
import numpy as np

def analyze(design,results,bootstrap=2000,seed=71027):
    if type(bootstrap) is not int or bootstrap < 1:raise ValueError('bootstrap must be a positive integer')
    planned={};obs={};missing=0;unknown_cost=0
    for row in design:
        if type(row.get('primary_eligible')) is not bool:raise ValueError('primary_eligible must be boolean')
        if type(row.get('arm')) is not int:raise ValueError('arm must be integer')
        key=(row['family'],row['task'],row['run'],row['arm'])
        if key in planned or row['arm'] not in [1,2,3,4]:raise ValueError('duplicate or invalid assignment')
        planned[key]=row
    for row in results:
        if type(row.get('arm')) is not int:raise ValueError('arm must be integer')
        billed=row.get('billed_usd')
        if billed is not None and (type(billed) not in (int,float) or not math.isfinite(billed) or billed < 0):raise ValueError('billed_usd must be null or finite nonnegative number')
        key=(row['family'],row['task'],row['run'],row['arm'])
        if key not in planned or key in obs:raise ValueError('unexpected/duplicate result')
        if type(row['verified_success']) is not bool:raise ValueError('success must be independently graded bool')
        if row.get('status')!='valid' and row['verified_success']:raise ValueError('invalid outcome cannot succeed')
        obs[key]=row
    if not planned:raise ValueError('empty design')
    blocks={}
    for (f,t,r,a),row in planned.items():
        if not row.get('primary_eligible',False):continue
        blocks.setdefault(f,{}).setdefault(t,{}).setdefault(r,{})[a]=obs.get((f,t,r,a),{}).get('verified_success',False)
        missing+=((f,t,r,a) not in obs)
        unknown_cost+=(obs.get((f,t,r,a),{}).get('billed_usd') is None)
    if not blocks:raise ValueError('no primary-eligible assignments')
    arrays={}
    for f,tasks in blocks.items():
        arrays[f]={}
        for t,runs in tasks.items():
            if any(set(x)!=set([1,2,3,4]) for x in runs.values()):raise ValueError('design must pair all four arms')
            arrays[f][t]=np.array([[int(x[a]) for a in [1,2,3,4]] for x in runs.values()],dtype=float)
    family={f:np.mean([a.mean(0) for a in tasks.values()],axis=0) for f,tasks in arrays.items()}
    point=np.mean(list(family.values()),axis=0)
    rng=np.random.default_rng(seed);keys=list(arrays);draws=[]
    for _ in range(bootstrap):
        families=[]
        for fi in rng.integers(len(keys),size=len(keys)):
            tasks=arrays[keys[fi]];tk=list(tasks);sample=[]
            for ti in rng.integers(len(tk),size=len(tk)):
                a=tasks[tk[ti]];sample.append(a[rng.integers(len(a),size=len(a))].mean(0))
            families.append(np.mean(sample,axis=0))
        v=np.mean(families,axis=0);draws.append(v[3]-v[1])
    lo,hi=np.quantile(draws,[.025,.975])
    return {'status':'draft estimator; intervals exploratory with few families','family_balanced_success_by_arm':point.tolist(),'primary_4_minus_2':float(point[3]-point[1]),'primary_95_percentile_interval':[float(lo),float(hi)],'families':{k:v.tolist() for k,v in family.items()},'assigned_primary_runs':sum(len(rs)*4 for ts in blocks.values() for rs in ts.values()),'missing_results_scored_zero':missing,'unknown_billed_cost_entries':unknown_cost,'bootstrap':bootstrap,'seed':seed}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--design',required=True);p.add_argument('--results',required=True);p.add_argument('--out',required=True);a=p.parse_args();r=analyze(json.loads(Path(a.design).read_text()),json.loads(Path(a.results).read_text()));Path(a.out).write_text(json.dumps(r,indent=2));print('Draft analysis written; no confirmatory claim implied.')
