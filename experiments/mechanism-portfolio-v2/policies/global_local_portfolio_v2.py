# Root-authored single allowed development refinement. No task names or label lookups.
import itertools
import numpy as np
AA='ACDEFGHIKLMNPQRSTVWY'
task=context['task']; positions=task['mutable_positions']; reference=task['reference_sequence']
assert task['direction']=='maximize' and 1<=len(positions)<=4 and not task['allow_indels']
parent=''.join(reference[p-1] for p in positions)
rng=np.random.default_rng(int(task['seed'])+1009*int(context['round_index']))
completion=context.get('research_context',{}).get('plate_completion',{})
count=int(completion.get('required_new_candidates',task['batch_size']))
def short(s):return ''.join(s[p-1] for p in positions)
observations=context['observations'];seen={short(o['sequence']) for o in observations}
seen.update(short(s) for s in completion.get('exclude_sequences',[]))
valid={short(o['sequence']):float(o['value']) for o in observations if o['qc']=='valid' and o['value'] is not None and np.isfinite(o['value'])}
all_codes=[''.join(x) for x in itertools.product(AA,repeat=len(positions))]
unseen=[s for s in all_codes if s not in seen];assert len(unseen)>=count
initial=len(seen)==1 and parent in seen
global_n=local_n=uniform_n=0
if initial:
    pools=[]
    for j in range(len(positions)):
        choices=[parent[:j]+a+parent[j+1:] for a in AA if a!=parent[j]]
        pools.append([choices[i] for i in rng.permutation(len(choices))])
    chosen=[];site_order=list(rng.permutation(len(positions)))
    for k in range(count):chosen.append(pools[site_order[k%len(positions)]].pop())
    model_name='shared_balanced_single_initialization'
elif len(valid)<2:
    chosen=[unseen[int(i)] for i in rng.choice(len(unseen),size=count,replace=False)]
    uniform_n=count;model_name='uniform_unseen_insufficient_numeric_data'
else:
    codes=list(valid)
    train=np.array([[AA.index(a) for a in s] for s in codes],dtype=np.int16)
    y=np.array([valid[s] for s in codes],dtype=float);scale=float(y.std());z=y-y.mean()
    if scale>0:z/=scale
    matches=(train[:,None,:]==train[None,:,:]).sum(axis=2).astype(float)
    regularized=matches+np.eye(len(y));dual=np.linalg.solve(regularized,z)
    pool=np.array([[AA.index(a) for a in s] for s in unseen],dtype=np.int16)
    scores=np.empty(len(unseen),dtype=float)
    for start in range(0,len(unseen),2048):
        block=pool[start:start+2048]
        cross=(block[:,None,:]==train[None,:,:]).sum(axis=2).astype(float)
        scores[start:start+len(block)]=cross@dual
    assert np.isfinite(scores).all()
    # Keep the published additive control's fixed exploration construction.
    # Different histories still imply different unseen pools, not identical draws.
    uniform_n=max(1,count//4)
    explore=[int(i) for i in rng.choice(len(unseen),size=uniform_n,replace=False)]
    excluded=set(explore);tie=rng.random(len(unseen));order=np.lexsort((tie,-scores))
    global_n=(count-uniform_n)//2
    global_indices=[int(i) for i in order if int(i) not in excluded][:global_n]
    chosen=[unseen[i] for i in global_indices];excluded.update(global_indices)
    centers=sorted(valid,key=lambda s:(-valid[s],s))[:4];frontier=set()
    for center in centers:
        for j in range(len(positions)):
            for a in AA:
                s=center[:j]+a+center[j+1:]
                if s not in seen:frontier.add(s)
    local_indices=[i for i,s in enumerate(unseen) if s in frontier and i not in excluded]
    local_target=min(count-uniform_n-global_n,len(local_indices))
    if local_target:
        local_pool=pool[local_indices]
        cross=(local_pool[:,None,:]==train[None,:,:]).sum(axis=2).astype(float)
        mean=cross@dual;solved=np.linalg.solve(regularized,cross.T)
        variance=np.maximum(0,len(positions)-np.sum(cross*solved.T,axis=1))
        acquisition=mean+0.5*np.sqrt(variance)
        available=np.ones(len(local_indices),dtype=bool)
        for _ in range(local_target):
            penalty=np.zeros(len(local_indices))
            if chosen:
                selected=np.array([[AA.index(a) for a in s] for s in chosen])
                similarity=(local_pool[:,None,:]==selected[None,:,:]).sum(axis=2).max(axis=1)
                penalty=0.15*similarity/len(positions)
            local_order=np.lexsort((tie[local_indices],-(acquisition-penalty)))
            j=next(int(i) for i in local_order if available[i]);ix=local_indices[j]
            chosen.append(unseen[ix]);excluded.add(ix);available[j]=False;local_n+=1
    # Only needed when a bounded frontier has too few legal unseen identities.
    for ix in order:
        if len(chosen)>=count-uniform_n:break
        ix=int(ix)
        if ix not in excluded:chosen.append(unseen[ix]);excluded.add(ix);global_n+=1
    chosen.extend(unseen[i] for i in explore)
    model_name='fixed_global_additive_local_ucb_portfolio'
assert len(chosen)==count and len(set(chosen))==count and not set(chosen)&seen
candidates=[]
for code in chosen:
    s=list(reference)
    for p,a in zip(positions,code):s[p-1]=a
    candidates.append({'sequence':''.join(s),'predicted_value':None,'rationale':'Frozen root-authored refinement: '+model_name})
result={'candidates':candidates,'diagnostic':{'policy_mode':'global_local_portfolio_v2','model_used':model_name,'global_model_count':global_n,'local_ucb_count':local_n,'uniform_count':uniform_n,'valid_training_count':len(valid),'missing_values_imputed':False,'uncertainty_claim':'Heuristic kernel acquisition, not calibrated confidence','method_claim':'Single adaptive development refinement; not native M or prompt-W efficacy'}}
