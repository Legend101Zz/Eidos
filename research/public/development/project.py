"""Ordinary PyTorch development projects; no Eidos dependency.
All datasets and references here are known DEVELOPMENT material.
"""
from pathlib import Path
import argparse, hashlib, json, time
import numpy as np
import torch
from torch import nn

torch.set_num_threads(1)
from data import PROJECTS, dataset

class ProjectModel(nn.Module):
    def __init__(self,nin,nout,kind='linear',mean=None,scale=None):
        super().__init__(); self.kind=kind
        self.register_buffer('mean',torch.zeros(nin) if mean is None else mean)
        self.register_buffer('scale',torch.ones(nin) if scale is None else scale)
        d=1 if kind=='pool' else nin
        self.net=nn.Sequential(nn.Linear(d,48),nn.Tanh(),nn.Linear(48,nout)) if kind=='mlp' else nn.Linear(d,nout)
    def forward(self,x):
        x=(x-self.mean)/self.scale
        if self.kind=='last':return x[:,-1:]
        if self.kind=='constant':return torch.zeros((len(x),2))
        if self.kind=='pool':x=x.mean(-1,keepdim=True)
        if self.kind=='center':x=x-x.mean(-1,keepdim=True)
        return self.net(x)

def train_model(name,variant,seed):
    data,task,nout=dataset(name);x,y,_=data['train'];torch.manual_seed(seed)
    x=torch.tensor(x);y=torch.tensor(y);nin=x.shape[1]
    mean=torch.zeros(nin);scale=torch.ones(nin);kind='linear';steps=100;lr=.02
    if name in ['iris','weights']:
        mean=x.mean(0);scale=x.std(0).clamp_min(.001);steps=100
    if name=='digits':kind='mlp' if variant=='reference' else 'linear';steps=100 if variant=='reference' else 8
    if name=='digits_shift':kind='center' if variant=='reference' else 'linear';steps=100
    if name=='conditioning':
        if variant=='reference':mean=x.mean(0);scale=x.std(0).clamp_min(.001)
        steps=160
    if name=='forecast':
        steps=120;kind='linear' if variant=='reference' else 'last'
    if name=='null':kind='constant'
    if name=='order':kind='linear' if variant=='reference' else 'pool';steps=60
    if name=='synthetic':
        kind='mlp';steps=180
        if variant=='reference':
            g=torch.Generator().manual_seed(seed+777);x=2*torch.rand(1536,2,generator=g)-1;y=(x[:,0]*x[:,1]>0).long()
    model=ProjectModel(nin,nout,kind,mean,scale)
    opt=torch.optim.Adam(model.parameters(),lr=lr)
    for _ in range(0 if kind in ['last','constant'] else steps):
        pred=model(x);loss=nn.functional.cross_entropy(pred,y) if task=='classification' else nn.functional.mse_loss(pred[:,0],y)
        opt.zero_grad();loss.backward();opt.step()
    # Known, attributable checkpoint corruption. Other repairs remain valid.
    if name=='weights':
        with torch.no_grad():
            model.net.weight.copy_(model.net.weight.roll(1,0));model.net.bias.copy_(model.net.bias.roll(1,0))
            if variant=='reference':
                model.net.weight.copy_(model.net.weight.roll(-1,0));model.net.bias.copy_(model.net.bias.roll(-1,0))
    return model,data,task,{'name':name,'variant':variant,'seed':seed,'kind':kind,'nin':nin,'nout':nout,'steps':0 if kind in ['last','constant'] else steps,'train_examples':len(y),'examples_seen':0 if kind in ['last','constant'] else steps*len(y),'synthetic_generated':len(y) if name=='synthetic' and variant=='reference' else 0}

def run(name,variant,seed,out):
    start=time.perf_counter();out=Path(out);out.mkdir(parents=True,exist_ok=True)
    model,data,task,config=train_model(name,variant,seed);model.eval()
    torch.save({'config':config,'state_dict':model.state_dict()},out/'model.pt')
    with torch.no_grad():
        for split,(x,y,ids) in data.items():
            pred=model(torch.tensor(x))
            pred=pred.argmax(-1).numpy() if task=='classification' else pred[:,0].numpy()
            np.savez(out/f'{split}_predictions.npz',ids=ids,predictions=pred)
    receipt={'config':config,'task_type':task,'elapsed_seconds':time.perf_counter()-start,'torch':torch.__version__,'device':'cpu','threads':1,'model_sha256':hashlib.sha256((out/'model.pt').read_bytes()).hexdigest(),'status':'development reference execution; no autonomous investigator'}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2));return receipt

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--project',choices=PROJECTS,required=True);p.add_argument('--variant',choices=['baseline','reference'],required=True);p.add_argument('--seed',type=int,default=11);p.add_argument('--out',required=True);a=p.parse_args();print(json.dumps(run(a.project,a.variant,a.seed,a.out)))
