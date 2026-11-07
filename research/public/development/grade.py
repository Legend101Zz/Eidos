"""Independent numeric grading boundary; candidate code is never imported here.
This development CLI has public development labels. Production isolation pending.
"""
import argparse,json
from pathlib import Path
import numpy as np
from data import dataset

def read_predictions(path,ids,kind,nclasses):
    with np.load(path,allow_pickle=False) as p:
        if set(p.files)!={'ids','predictions'}:raise ValueError('unexpected or missing arrays')
        got=p['ids'];y=p['predictions']
    if got.ndim!=1 or y.shape!=ids.shape or got.shape!=ids.shape:raise ValueError('shape mismatch')
    if got.dtype.kind not in 'iu' or len(np.unique(got))!=len(got):raise ValueError('invalid/duplicate IDs')
    if set(got.tolist())!=set(ids.tolist()):raise ValueError('ID set mismatch')
    if y.dtype.kind not in 'iuf' or not np.isfinite(y).all():raise ValueError('nonfinite/nonnumeric prediction')
    if kind=='classification' and ((y!=np.floor(y)).any() or (y<0).any() or (y>=nclasses).any()):raise ValueError('invalid class')
    lookup={int(i):v for i,v in zip(got,y)}
    return np.asarray([lookup[int(i)] for i in ids])

def score(y,p,kind):
    try:
        with np.errstate(over='raise', invalid='raise'):
            value = float(np.mean(y == p)) if kind == 'classification' else float(np.sqrt(np.mean((y.astype(np.float64) - p.astype(np.float64))**2)))
    except FloatingPointError as error:
        raise ValueError('nonfinite metric from finite input') from error
    if not np.isfinite(value): raise ValueError('nonfinite metric')
    return value
def compare(project,baseline,candidate,split='test'):
    data,kind,n=dataset(project);_,y,ids=data[split]
    b=read_predictions(baseline,ids,kind,n);c=read_predictions(candidate,ids,kind,n)
    bs,cs=score(y,b,kind),score(y,c,kind)
    gain=cs-bs if kind=='classification' else bs-cs
    return {'metric':'accuracy' if kind=='classification' else 'rmse','baseline':bs,'candidate':cs,'gain':gain,'n':len(y),'split':split,'status':'numeric development result; no causal or system certification'}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--project',required=True);p.add_argument('--baseline',required=True);p.add_argument('--candidate',required=True);p.add_argument('--split',choices=['validation','test'],default='test');a=p.parse_args();print(json.dumps(compare(a.project,a.baseline,a.candidate,a.split),indent=2))
