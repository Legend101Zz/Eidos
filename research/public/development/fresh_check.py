import argparse,json
from pathlib import Path
import torch,numpy as np
from project import ProjectModel,dataset
p=argparse.ArgumentParser();p.add_argument('artifact');a=p.parse_args();root=Path(a.artifact)
b=torch.load(root/'model.pt',weights_only=True);c=b['config'];m=ProjectModel(c['nin'],c['nout'],c['kind']);m.load_state_dict(b['state_dict']);m.eval()
d,kind,_=dataset(c['name']);x,y,ids=d['test']
with torch.no_grad():out=m(torch.tensor(x));out=out.argmax(-1).numpy() if kind=='classification' else out[:,0].numpy()
with np.load(root/'test_predictions.npz',allow_pickle=False) as z:assert np.array_equal(z['ids'],ids);assert np.array_equal(out,z['predictions'])
print(json.dumps({'fresh_process_export_equal':True,'project':c['name']}))
