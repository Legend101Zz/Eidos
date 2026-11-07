"""Run narrow numeric conformance fixtures against pinned downloaded source.
Not full task execution; I/O for AIRS solar is explicitly replaced with tiny fixtures.
No upstream code or dataset is redistributed in this public package.
"""
import argparse,importlib.util,json,warnings
from pathlib import Path
import numpy as np,pandas as pd

def module(path,name):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def main(root):
 root=Path(root);rows=[]
 m=module(root/'MLGym/data/regressionKaggleHousePrice/evaluate.py','houses')
 rmse,r2=m.calculate_metrics(np.array([100.,200.]),np.array([100.,100.]));assert abs(rmse-np.sqrt(5000))<1e-9
 rows.append({'source':'MLGym house price','fixture':'raw price RMSE is sqrt(5000) on [100,200] versus [100,100]','passed':True,'scope':'numeric function only; no official dataset or container'})
 m=module(root/'MLGym/data/imageClassificationCifar10/evaluate.py','cifar')
 assert m.evaluate_submission(pd.DataFrame({'label':[1,2,3]}),pd.DataFrame({'label':[1,2,0]}))==2/3
 try:m.evaluate_submission(pd.DataFrame({'label':[1,2,3]}),pd.DataFrame({'label':[1]}))
 except AssertionError:pass
 else:raise AssertionError('wrong length accepted')
 rows.append({'source':'MLGym CIFAR10','fixture':'accuracy and row-count rejection','passed':True,'scope':'fabricated labels; no CIFAR task execution'})
 m=module(root/'airs-bench/airsbench/tasks/rad/TextualSimilaritySickSpearmanCorrelation/evaluate.py','sick')
 assert abs(m.evaluate([3,2,1],[1,2,3])['SpearmanCorrelation']+1)<1e-12
 with warnings.catch_warnings():
  warnings.simplefilter('ignore');v=m.evaluate([1,1,1],[1,2,3])['SpearmanCorrelation']
 assert not np.isfinite(v)
 rows.append({'source':'AIRS SICK','fixture':'reverse ranks -1; constant predictor yields nonfinite score','passed':True,'adaptation_requirement':'reject nonfinite metrics; do not credit as zero silently'})
 m=module(root/'airs-bench/airsbench/tasks/rad/TimeSeriesForecastingSolarWeeklyMAE/evaluate.py','solar')
 m.load_from_disk=lambda _: {'target':[[1.,2.]]}
 ordinary=m.evaluate(['[3,100,100,100,100]'],[[1,2,3,4,5,6,7]])['MAE']
 masked=m.evaluate(['[3,NaN,NaN,NaN,NaN]'],[[1,2,3,4,5,6,7]])['MAE']
 assert ordinary>0 and masked==0
 rows.append({'source':'AIRS solar','fixture':'partial prediction NaNs excluded from scoring by upstream valid_mask','passed':True,'ordinary_mae':ordinary,'partial_nan_mae':masked,'adaptation_requirement':'fix denominator from labels; reject candidate NaNs; label as adapted benchmark'})
 return rows
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--sources',required=True);p.add_argument('--out',required=True);a=p.parse_args();r=main(a.sources);Path(a.out).write_text(json.dumps(r,indent=2));print(json.dumps({'executed_upstream_fixtures':len(r),'output':a.out}))
