"""Trusted public development data definitions, separate from candidate model code.
Public labels and generators are reference-only; do not provide this file to investigators.
"""
import hashlib
import numpy as np
from sklearn.datasets import load_digits, load_iris
from sklearn.model_selection import train_test_split

PROJECTS = ['iris', 'digits', 'digits_shift', 'conditioning', 'forecast', 'order', 'weights', 'synthetic', 'null']

def opaque_ids(n, namespace):
    """Stable opaque development identifiers, independent of label/row ordering.

    Public construction is not a privacy boundary; never reuse it for sealed IDs.
    """
    values = [int.from_bytes(hashlib.sha256(f"eidos-dev-id-v2:{namespace}:{i}".encode()).digest()[:8], "big") & ((1 << 63) - 1) for i in range(n)]
    assert len(set(values)) == n
    return np.asarray(values, dtype=np.int64)

def split_arrays(x,y,seed):
    idx=np.arange(len(y)); tr,rest=train_test_split(idx,test_size=.4,random_state=seed,stratify=y)
    va,te=train_test_split(rest,test_size=.5,random_state=seed+1,stratify=y[rest])
    return {k:(x[v].astype('float32'), y[v].astype('int64'), opaque_ids(len(y), f'real:{seed}')[v]) for k,v in [('train',tr),('validation',va),('test',te)]}

def dataset(name,seed=20261107):
    if name in ['iris','weights']:
        d=load_iris(); return split_arrays(d.data,d.target,seed), 'classification',3
    if name in ['digits','digits_shift']:
        d=load_digits(); out=split_arrays(d.data/16.,d.target,seed)
        if name=='digits_shift':
            # IDs split before transforms; a source image cannot occur across splits.
            for split,(x,y,ids) in list(out.items()):
                if split!='train':x=x+.35
                out[split]=(x,y,ids)
        return out,'classification',10
    out={}
    for j,(split,n) in enumerate([('train',768),('validation',256),('test',512)]):
        r=np.random.default_rng(seed+1009*j)
        if name=='conditioning':
            z=r.normal(size=(n,6)); x=z*np.array([.01,.1,1,10,100,1000]);y=z@np.array([2.,-1.,.5,1.,-2.,3.])+r.normal(0,.05,n)
        elif name=='forecast':
            # Independent 20-step series, split at series ID, predict next value.
            a=r.uniform(.5,2,n); phase=r.uniform(0,6.28,n);t=np.arange(21)
            seq=a[:,None]*np.sin(2*np.pi*t/6+phase[:,None])+r.normal(0,.02,(n,21))
            x=seq[:,:20]; y=seq[:,20]
        elif name=='order':
            y=r.integers(0,2,n); x=np.zeros((n,8)); x[np.arange(n),np.where(y==0,1,6)]=1; x[np.arange(n),np.where(y==0,6,1)]=-1
            x+=r.normal(0,.02,x.shape)
        elif name=='synthetic':
            x=r.uniform(-1,1,(n,2)); y=(x[:,0]*x[:,1]>0).astype('int64')
            if split=='train':
                # Exposed simulator is legitimate extra train-data source.
                keep=(x[:,0]>0);x=x[keep];y=y[keep];n=len(y)
        elif name=='null':
            base=r.normal(size=(n//2,4));x=np.repeat(base,2,axis=0);y=np.tile([0,1],n//2)
        else:raise ValueError(name)
        typ=np.int64 if name in ['order','synthetic','null'] else np.float32
        out[split]=(np.asarray(x,dtype=np.float32),np.asarray(y,dtype=typ),opaque_ids(n, f'{name}:{seed}:{split}'))
        if name == 'null':
            # Avoid exposing the paired target construction through serialized row order.
            permutation = np.random.default_rng(seed + 314159 + j).permutation(n)
            out[split] = tuple(a[permutation] for a in out[split])
    return out, ('classification' if name in ['order','synthetic','null'] else 'regression'),2 if name in ['order','synthetic','null'] else 1
