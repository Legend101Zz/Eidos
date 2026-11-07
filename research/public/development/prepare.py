"""Materialize development inputs and explicitly exposed development test labels."""
from pathlib import Path
import argparse,hashlib,json
import numpy as np
from data import dataset,PROJECTS
p=argparse.ArgumentParser();p.add_argument('--project',choices=PROJECTS,required=True);p.add_argument('--out',required=True);a=p.parse_args()
root=Path(a.out);root.mkdir(parents=True,exist_ok=True);d,kind,n=dataset(a.project);files={}
for split,(x,y,ids) in d.items():
 path=root/(split+'.npz');np.savez_compressed(path,x=x,ids=ids,**({'y':y} if split!='test' else {}));files[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
 if split=='test':
  path=root/'PUBLIC_DEVELOPMENT_TEST_LABELS.npz';np.savez(path,ids=ids,y=y);files[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
(root/'inputs.json').write_text(json.dumps({'project':a.project,'role':'public-development','task_type':kind,'nclasses_or_outputs':n,'files':files,'warning':'Test labels are intentionally public for qualification; do not treat this package as held out.'},indent=2));print('Development inputs prepared.')
