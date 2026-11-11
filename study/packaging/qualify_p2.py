"""Read-only public reference qualification against the packaged baseline."""
import json, sys, subprocess
from pathlib import Path
import numpy as np
import torch
from study.common import DEVELOPMENT, research_python
from study.packaging.package_card import package, TASKS
sys.path.insert(0,str(DEVELOPMENT))
from project import train_model
from data import dataset
from grade import read_predictions, score

def main(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);rows=[]
    for card,name in TASKS.items():
        if card.startswith('N'): continue  # Pilot-2 qualification retains its original four cards.
        task=root/card/'task';package(card,task)
        baseline=root/card/'baseline';baseline.mkdir()
        subprocess.run([research_python(),str(task/'project.py'),'train','--inputs',str(task),'--out',str(baseline),'--seed','11'],check=True)
        data,kind,classes=dataset(name);x,y,ids=data['test']
        inputs=root/card/'test-inputs.npz';np.savez(inputs,x=x,ids=ids)
        predictions=baseline/'test.npz'
        subprocess.run([research_python(),str(task/'project.py'),'infer','--model',str(baseline/'model.pt'),'--inputs',str(inputs),'--out',str(predictions)],check=True)
        b=score(y,read_predictions(predictions,ids,kind,classes),kind)
        values=[]
        for seed in (11,23,37):
            m,_,_,_=train_model(name,'reference',seed);m.eval()
            with torch.no_grad():
                p=m(torch.tensor(x));p=p.argmax(-1).numpy() if kind=='classification' else p[:,0].numpy()
            value=score(y,p,kind);gain=value-b if kind=='classification' else b-value
            values.append((seed,value,gain))
        bar=json.loads((DEVELOPMENT/'tasks'/f'{card}.json').read_text())['min_reference_gain']
        assert all(v[2]>=bar for v in values),(card,b,values)
        rows.append(dict(card=card,metric='accuracy' if kind=='classification' else 'rmse',baseline=b,references=values,bar=bar))
        print(json.dumps(rows[-1]),flush=True)
    (root/'results.json').write_text(json.dumps(rows,indent=2))
    output=Path(__file__).with_name('QUALIFICATION_P2.md')
    output.write_text('# Pilot 2 baseline qualification\n\nPackaged baseline seed 11; read-only public reference, fresh seeds 11/23/37. CPU, one thread.\n\n|Card|Metric|Baseline|Reference seed 11|Gain seed 11|Fresh-seed gains 23,37|Bar|\n|---|---|---:|---:|---:|---|---:|\n'+''.join(f"|{r['card']}|{r['metric']}|{r['baseline']:.9f}|{r['references'][0][1]:.9f}|{r['references'][0][2]:.9f}|{r['references'][1][2]:.9f}, {r['references'][2][2]:.9f}|{r['bar']}|\n" for r in rows)+'\nAll reference seeds clear the practical bar. No model calls. Reproduce: source ~/eidos-internal/env.sh; "$EIDOS_RESEARCH_PYTHON" -m study.packaging.qualify_p2 EMPTY_DIR\n')
if __name__=='__main__': main(sys.argv[1])
