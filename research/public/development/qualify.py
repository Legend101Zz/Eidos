"""Execute reference interventions and adversarial numeric controls; no LLM calls."""
from pathlib import Path
import argparse,hashlib,json,platform,subprocess,sys,time
import numpy as np
import torch
from project import PROJECTS,run,dataset
from grade import compare,read_predictions
from instrument import error_slices
from lifecycle import qualify as qualify_lifecycle

def qualify(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);start=time.perf_counter();rows=[];checks=[]
    cards={json.loads(p.read_text())['project']:json.loads(p.read_text()) for p in (Path(__file__).parent/'tasks').glob('*.json') if json.loads(p.read_text())['min_reference_gain']>0}
    for name in PROJECTS:
        for seed in [11,23,37]:
            root=out/name/str(seed)
            for variant in ['baseline','reference']:run(name,variant,seed,root/variant)
            result=compare(name,root/'baseline/test_predictions.npz',root/'reference/test_predictions.npz')
            bar=cards.get(name,{}).get('min_reference_gain',0)
            result.update(project=name,seed=seed,qualification_bar=bar,reference_pass=result['gain']>=bar-1e-12)
            if name=='null':result['reference_pass']=abs(result['gain'])<1e-12 and result['candidate']==.5
            rows.append(result)
            with (out/'reference-results.jsonl').open('a') as log:log.write(json.dumps(result)+'\n')
            null=compare(name,root/'baseline/test_predictions.npz',root/'baseline/test_predictions.npz');assert null['gain']==0
            if seed==11:
                r=subprocess.run([sys.executable,str(Path(__file__).parent/'fresh_check.py'),str(root/'reference')],capture_output=True,text=True,timeout=60)
                checks.append({'check':'fresh process export','project':name,'passed':r.returncode==0,'detail':r.stderr.strip()})
                assert r.returncode==0,r.stderr
        data,kind,nclass=dataset(name);x,y,ids=data['test'];good=np.zeros(len(ids),dtype='int64') if kind=='classification' else np.zeros(len(ids))
        cases=[('nan',ids,np.full(len(ids),np.nan)),('inf',ids,np.full(len(ids),np.inf)),('short',ids[:-1],good[:-1]),('duplicate',np.full(len(ids),ids[0]),good),('wrong_ids',ids+9999999,good),('matrix',ids,good[:,None])]
        if kind=='classification':cases += [('invalid_class',ids,np.full(len(ids),nclass)),('fractional_class',ids,np.full(len(ids),.5))]
        for label,i,p in cases:
            file=out/'invalid.npz';np.savez(file,ids=i,predictions=p)
            try:read_predictions(file,ids,kind,nclass)
            except ValueError:checks.append({'check':label,'project':name,'passed':True})
            else:raise AssertionError((name,label,'accepted malformed predictions'))
        np.savez(out/'reordered.npz',ids=ids[::-1],predictions=good[::-1]);assert np.array_equal(read_predictions(out/'reordered.npz',ids,kind,nclass),good)
        # Every split ID disjoint; task families may intentionally share the project.
        splitsets=[set(v[2].tolist()) for v in data.values()];assert all(not a&b for j,a in enumerate(splitsets) for b in splitsets[j+1:])
    instruments=[]
    for name in ['digits','forecast']:
        data,kind,nclass=dataset(name);x,y,ids=data['validation'];p=read_predictions(out/name/'11/reference/validation_predictions.npz',ids,kind,nclass)
        report=error_slices(x,y,p,kind);assert sum(x['n'] for x in report['bins'])==len(y)
        assert abs(sum(x['n']*(x['mean_error'] or 0) for x in report['bins'])/len(y)-report['mean_error'])<1e-10
        report['project']=name;instruments.append(report)
    lifecycle=qualify_lifecycle(out/'lifecycle')
    report={'status':'development reference qualification only','environment':{'python':sys.version,'platform':platform.platform(),'torch':torch.__version__,'device':'cpu','threads':1},'reference_results':rows,'input_grader_checks':checks,'instruments':instruments,'lifecycle':lifecycle,'elapsed_seconds':time.perf_counter()-start,'source_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')},'limitations':['no Eidos integration','no LLM investigation','no hostile-code isolation','no estimated stochastic false-positive rate or power','small public development datasets; no contamination claim']}
    (out/'qualification.json').write_text(json.dumps(report,indent=2));print(json.dumps({'output':str(out/'qualification.json'),'reference_cases':len(rows),'passing':sum(r['reference_pass'] for r in rows),'failed':[r for r in rows if not r['reference_pass']],'numeric_checks':len(checks),'lifecycle_seeds':len(lifecycle),'elapsed_seconds':report['elapsed_seconds']},indent=2))
    return report
def require_passing(report):
    if not all(row['reference_pass'] for row in report['reference_results']):
        raise SystemExit('Reference qualification failed; see report for failed bars')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args();require_passing(qualify(a.out))
