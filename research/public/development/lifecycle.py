"""CPU state fixtures, not an Eidos service implementation or security sandbox."""
import argparse,copy,hashlib,json,os,random,subprocess,sys
from pathlib import Path
import numpy as np
import torch
from torch import nn

torch.set_num_threads(1)
def initialize(seed):
    random.seed(seed);np.random.seed(seed);torch.manual_seed(seed)
    model=nn.Sequential(nn.Linear(5,12),nn.Dropout(.25),nn.Tanh(),nn.Linear(12,1));model.train()
    opt=torch.optim.Adam(model.parameters(),lr=.01)
    gen=torch.Generator().manual_seed(seed+100)
    return model,opt,gen,{'step':0,'examples':0,'component_calls':0}

def advance(model,opt,gen,meta,end):
    while meta['step']<end:
        x=torch.randn(32,5,generator=gen);y=x[:,0]-.5*x[:,1]
        # Exercise independent Python, NumPy, default torch dropout, data RNG state.
        loss=((model(x)[:,0]-y)**2).mean()*(.95+.02*random.random()+.02*float(np.random.rand()))
        opt.zero_grad();loss.backward();opt.step()
        meta['step']+=1;meta['examples']+=32;meta['component_calls']+=1

def capture(model,opt,gen,meta):
    return copy.deepcopy({'model':model.state_dict(),'optimizer':opt.state_dict(),'torch_rng':torch.get_rng_state(),'numpy_rng':np.random.get_state(),'python_rng':random.getstate(),'data_rng':gen.get_state(),'meta':meta,'training':model.training,'schema':1})

def restore(state):
    m,o,g,meta=initialize(0);m.load_state_dict(state['model']);o.load_state_dict(copy.deepcopy(state['optimizer']));m.train(state['training'])
    torch.set_rng_state(state['torch_rng']);np.random.set_state(state['numpy_rng']);random.setstate(state['python_rng']);g.set_state(state['data_rng'])
    return m,o,g,copy.deepcopy(state['meta'])

def equal(a,b):
    if isinstance(a,torch.Tensor):return isinstance(b,torch.Tensor) and torch.equal(a,b)
    if isinstance(a,np.ndarray):return isinstance(b,np.ndarray) and np.array_equal(a,b)
    if isinstance(a,dict):return a.keys()==b.keys() and all(equal(a[k],b[k]) for k in a)
    if isinstance(a,(tuple,list)):return type(a)==type(b) and len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b))
    return a==b

def worker(a):
    if a.resume:state=torch.load(a.resume,weights_only=False);m,o,g,meta=restore(state)
    else:m,o,g,meta=initialize(a.seed)
    advance(m,o,g,meta,a.steps)
    tmp=Path(a.output+'.tmp');torch.save(capture(m,o,g,meta),tmp);os.replace(tmp,a.output)
    if a.crash:os._exit(17) # actual abnormal worker termination after committed fixture snapshot

def qualify(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True);rows=[]
    for seed in [11,23,37]:
        root=out/str(seed);root.mkdir(exist_ok=True)
        def call(name,steps,resume=None,crash=False):
            cmd=[sys.executable,__file__,'worker','--seed',str(seed),'--steps',str(steps),'--output',str(root/name)]
            if resume:cmd+=['--resume',str(root/resume)]
            if crash:cmd+=['--crash']
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=60)
            assert r.returncode==(17 if crash else 0),(r.returncode,r.stderr)
            return torch.load(root/name,weights_only=False)
        full=call('full.pt',40);parent=call('parent.pt',17);resumed=call('resume.pt',40,'parent.pt')
        assert equal(full,resumed),'fresh-process exact restore failed'
        crash_parent=call('crash.pt',17,crash=True);after_crash=call('crash_resume.pt',40,'crash.pt');assert equal(full,after_crash)
        fingerprint=hashlib.sha256((root/'parent.pt').read_bytes()).hexdigest();snapshot=copy.deepcopy(parent)
        m,o,g,meta=restore(parent)
        with torch.no_grad():next(m.parameters()).add_(.2)
        advance(m,o,g,meta,40);branch=capture(m,o,g,meta)
        assert equal(snapshot,parent),'in-memory parent mutated'
        assert fingerprint==hashlib.sha256((root/'parent.pt').read_bytes()).hexdigest(),'on-disk parent mutated'
        assert not equal(branch['model'],full['model'])
        # Negative controls must be detected; incomplete restore cannot be called exact.
        bad=copy.deepcopy(parent);bad['torch_rng']=torch.zeros_like(bad['torch_rng'])
        # Avoid invalid RNG bytes: valid alternate RNG state provides an omitted-state control.
        torch.manual_seed(999);bad['torch_rng']=torch.get_rng_state();bad['optimizer']['state']={}
        bm,bo,bg,bmeta=restore(bad);advance(bm,bo,bg,bmeta,40);missing_detected=not equal(capture(bm,bo,bg,bmeta),full);assert missing_detected
        rows.append({'seed':seed,'fresh_process_exact_restore':True,'actual_exit_17_recovery':True,'branch_parent_unchanged':True,'branch_differs':True,'missing_rng_optimizer_detected':missing_detected,'scope':'CPU single-process training fixture; no distributed, MPS, fencing, sandbox or Eidos claims'})
    return rows

if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('qualify');q.add_argument('--out',required=True)
    q=sub.add_parser('worker');q.add_argument('--seed',type=int,default=11);q.add_argument('--steps',type=int,required=True);q.add_argument('--output',required=True);q.add_argument('--resume');q.add_argument('--crash',action='store_true')
    a=p.parse_args()
    if a.command=='worker':worker(a)
    else:print(json.dumps(qualify(a.out),indent=2))
