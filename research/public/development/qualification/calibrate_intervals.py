"""Synthetic calibration only; no study data or model calls."""
import os
os.environ['OMP_NUM_THREADS']='1'
os.environ['OPENBLAS_NUM_THREADS']='1'
os.environ['MKL_NUM_THREADS']='1'
import argparse, hashlib, importlib.util, json, math, time
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent/'analyze_results.py'
SEED=71107026
REPS=400
BOOT=799
TASKS=4
RUNS=3

def interval(a,rng,b=BOOT):
    # Balanced F x T x R x 2. Each occurrence of a sampled family receives
    # independent task resampling; each task occurrence independent run draws.
    f,t,r,_=a.shape
    fi=rng.integers(f,size=(b,f,1,1))
    ti=rng.integers(t,size=(b,f,t,1))
    ri=rng.integers(r,size=(b,f,t,r))
    d=a[fi,ti,ri,1]-a[fi,ti,ri,0]
    return np.quantile(d.mean(axis=(1,2,3)),[.025,.975])

def sample(s,rng):
    f=s['families']
    if s['model']=='family_shared_binary':
        # A single shared family uniform determines every task/run outcome.
        # Known marginal p2=.3, p4=.3+effect, with perfect within-family dependence.
        u=rng.random((f,1,1))
        p2=.3; p4=.3+s['effect']
        a=np.stack([u<p2,u<p4],axis=-1).astype(float)
        return np.broadcast_to(a,(f,TASKS,RUNS,2)),float((a[...,1]-a[...,0]).mean())
    # Family random intercept +- .10; independent task intercept +- .05.
    # Family treatment effects effect +- heterogeneity, each with prob 1/2.
    base=.35+rng.choice([-.10,.10],size=(f,1,1))+rng.choice([-.05,.05],size=(f,TASKS,1))
    de=s['effect']+rng.choice([-s['heterogeneity'],s['heterogeneity']],size=(f,1,1))
    p2=np.broadcast_to(base,(f,TASKS,RUNS));p4=np.broadcast_to(base+de,p2.shape)
    assert np.all((p4>=-1e-12)&(p4<=1+1e-12))
    p4=np.clip(p4,0,1)
    # Mixture of common and independent uniforms induces paired positive correlation.
    u2=rng.random(p2.shape);independent=rng.random(p2.shape)
    u4=np.where(rng.random(p2.shape)<.75,u2,independent)
    return np.stack([u2<p2,u4<p4],axis=-1).astype(float),float(de.mean())

def wilson(k,n):
    z=1.96;p=k/n;den=1+z*z/n
    mid=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [mid-half,mid+half]

scenarios=[
 {'name':'null_3_families','families':3,'effect':0.,'heterogeneity':.2,'model':'random_probability'},
 {'name':'null_8_families','families':8,'effect':0.,'heterogeneity':.2,'model':'random_probability'},
 {'name':'positive_3_families','families':3,'effect':.2,'heterogeneity':.2,'model':'random_probability'},
 {'name':'positive_8_families','families':8,'effect':.2,'heterogeneity':.2,'model':'random_probability'},
 {'name':'boundary_8_families','families':8,'effect':.1,'heterogeneity':.1,'model':'random_probability'},
 {'name':'shared_positive_3_families','families':3,'effect':.2,'model':'family_shared_binary'},
 {'name':'shared_positive_8_families','families':8,'effect':.2,'model':'family_shared_binary'},
 {'name':'shared_boundary_3_families','families':3,'effect':.1,'model':'family_shared_binary'},
]

def main(output):
    start=time.perf_counter();cpu=time.process_time()
    seedseq=np.random.SeedSequence(SEED)
    summaries=[]
    for s,ss in zip(scenarios,seedseq.spawn(len(scenarios))):
        data_ss,boot_ss=ss.spawn(2)
        data_rng=np.random.default_rng(data_ss);boot_rng=np.random.default_rng(boot_ss)
        points=[];width=[];popcover=condcover=abovezero=abovepractical=degenerate=0
        for _ in range(REPS):
            a,conditional_truth=sample(s,data_rng)
            lo,hi=interval(a,boot_rng)
            points.append(float((a[...,1]-a[...,0]).mean()))
            width.append(float(hi-lo))
            popcover+=bool(lo-1e-12<=s['effect']<=hi+1e-12)
            condcover+=bool(lo-1e-12<=conditional_truth<=hi+1e-12)
            abovezero+=bool(lo>1e-12)
            abovepractical+=bool(lo>.1+1e-12)
            degenerate+=bool(hi-lo<1e-12)
        rates={}
        for key,k in [('population_coverage',popcover),('sampled_family_conditional_coverage',condcover),('lower_bound_above_zero',abovezero),('lower_bound_above_point10',abovepractical),('zero_width_interval',degenerate)]:
            rates[key]={'count':int(k),'n':REPS,'rate':k/REPS,'monte_carlo_wilson95':wilson(k,REPS)}
        summaries.append({**s,'mean_estimate':float(np.mean(points)),'bias':float(np.mean(points)-s['effect']),'mean_estimate_monte_carlo_se':float(np.std(points,ddof=1)/np.sqrt(REPS)),'mean_interval_width':float(np.mean(width)),**rates})
    # Run the actual source on two synthetic datasets as a limited consistency
    # check, using independent bootstrap streams (not equal random draws).
    spec=importlib.util.spec_from_file_location('draft_analysis',SOURCE)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    comparisons=[]
    for i in [0,3]:
        a,_=sample(scenarios[i],np.random.default_rng(SEED+100+i))
        design=[];results=[]
        for f in range(a.shape[0]):
            for t in range(TASKS):
                for r in range(RUNS):
                    for arm in [1,2,3,4]:
                        row={'family':str(f),'task':str(t),'run':r,'arm':arm,'primary_eligible':True}
                        design.append(row)
                        results.append({**row,'status':'valid','verified_success':bool(a[f,t,r,int(arm==4)]),'billed_usd':0.})
        out=mod.analyze(design,results,bootstrap=BOOT,seed=SEED+i)
        fast=interval(a,np.random.default_rng(SEED+200+i))
        comparisons.append({'scenario':scenarios[i]['name'],'source_point':out['primary_4_minus_2'],'vectorized_point':float((a[...,1]-a[...,0]).mean()),'source_interval':out['primary_95_percentile_interval'],'independent_vectorized_interval':fast.tolist(),'note':'different random draws; two cases do not validate arbitrary/unbalanced designs'})
    receipt={'status':'synthetic calibration only; exploratory; no campaign power validation','seed':SEED,'numpy':np.__version__,'source':'development/analyze_results.py','source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'replicates_per_scenario':REPS,'bootstrap_draws':BOOT,'tasks_per_family':TASKS,'runs_per_task':RUNS,'target':'equal-weight random-family population mean contrast; conditional sampled-family contrast reported separately','interval':'paired hierarchical percentile bootstrap, families then tasks then runs; arms paired','scenarios':summaries,'limited_source_comparisons':comparisons,'wall_seconds':time.perf_counter()-start,'cpu_seconds':time.process_time()-cpu}
    Path(output).write_text(json.dumps(receipt,indent=2)+'\n')
    for s in summaries:
        print(s['name'],'coverage',round(s['population_coverage']['rate'],3),'CI lower>.10',round(s['lower_bound_above_point10']['rate'],3),'zero width',round(s['zero_width_interval']['rate'],3))
    print('cpu_seconds',receipt['cpu_seconds'],'wall_seconds',receipt['wall_seconds'])

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True);args=parser.parse_args();main(args.out)
