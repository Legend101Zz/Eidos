"""Task-independent validation instrument: descriptive error by input norm quartile.
A slice association is not a causal explanation. Labels are validation-only here.
"""
import numpy as np

def error_slices(x,y,p,kind):
    v=np.linalg.norm(x,axis=1);bounds=np.quantile(v,[.25,.5,.75]);bins=np.digitize(v,bounds)
    err=(y!=p).astype(float) if kind=='classification' else np.abs(y.astype(np.float64)-p.astype(np.float64))
    return {'instrument_version':'error-slices/1','scope':'validation association only','bins':[{'bin':i,'n':int((bins==i).sum()),'mean_error':float(err[bins==i].mean()) if (bins==i).any() else None} for i in range(4)],'mean_error':float(err.mean())}
