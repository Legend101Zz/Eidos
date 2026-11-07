"""Public bundle controls; runs only local trusted code, no model API calls."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import numpy as np
from data import dataset
from grade import score, read_predictions
from package_task import package, TASKS
from project import run
from qualify import require_passing

def check(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    controls=[]
    for task,project in TASKS.items():
        bundle=out/task/'bundle';package(task,bundle)
        assert {p.name for p in bundle.iterdir()} == {'train.npz','validation.npz','project.py','requirements.lock','brief.md','task.json'}
        result=out/task/'run'
        subprocess.run([sys.executable,str(bundle/'project.py'),'train','--inputs',str(bundle),'--out',str(result),'--seed','11'],check=True,timeout=120)
        reference=out/task/'reference';run(project,'baseline',11,reference)
        inputs=out/task/'test_inputs.npz'; data,kind,n=dataset(project);x,y,ids=data['test'];np.savez(inputs,x=x,ids=ids)
        prediction=result/'test_predictions.npz'
        subprocess.run([sys.executable,str(bundle/'project.py'),'infer','--model',str(result/'model.pt'),'--inputs',str(inputs),'--out',str(prediction)],check=True,timeout=60)
        a=read_predictions(prediction,ids,kind,n);b=read_predictions(reference/'test_predictions.npz',ids,kind,n)
        assert np.array_equal(a,b)
        for split in ['train','validation']:
            with np.load(bundle/f'{split}.npz',allow_pickle=False) as d:
                assert set(d.files)=={'x','y','ids'}
        # Past deterministic label shortcuts must not explain these IDs.
        if project=='iris': assert not np.array_equal(ids//50,y)
        if project=='null':
            assert np.mean(ids%2==y)<.65
            assert np.mean(np.arange(len(y))%2==y)<.65
        controls.append({'task':task,'baseline_predictions_match':True,'fresh_process_inference':True,'bundle_allowlist':True,'test_labels_excluded':True})
    try: score(np.array([0.]),np.array([1e308]),'regression')
    except ValueError: pass
    else: raise AssertionError('overflow metric accepted')
    try: require_passing({'reference_results':[{'reference_pass':False}]})
    except SystemExit: pass
    else: raise AssertionError('failed reference bar returned success')
    report={'scope':'trusted local development packaging only; no sandbox isolation guarantee', 'cases':controls,'overflow_metric_rejected':True,'failed_qualification_bar_rejected':True,
            'source_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}}
    (out/'bundle-checks.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'bundle_cases_passed':len(controls),'negative_controls_passed':2}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True);args=parser.parse_args();check(args.out)
