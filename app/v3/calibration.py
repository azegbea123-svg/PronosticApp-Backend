import math
from typing import Dict, List
from .utils import CLASSES, normalize

def temperature(p:Dict[str,float], T:float=1.0)->Dict[str,float]:
    T=max(0.35,min(3.0,T)); logits=[math.log(max(p[c],1e-9))/T for c in CLASSES]; m=max(logits); ex=[math.exp(x-m) for x in logits]; s=sum(ex)
    return {c:v/s for c,v in zip(CLASSES,ex)}

def fit_temperature(history:List[tuple[Dict[str,float],str]])->float:
    if len(history)<30: return 1.0
    best=(1.0,float('inf'))
    for i in range(35,301,5):
        T=i/100
        loss=0.0
        for p,y in history:
            q=temperature(p,T); loss-=math.log(max(q.get(y,1e-9),1e-9))
        if loss<best[1]: best=(T,loss)
    return best[0]

def reliability(history:List[tuple[Dict[str,float],str]], bins:int=10):
    out=[]
    for b in range(bins):
        lo=b/bins; hi=(b+1)/bins
        vals=[(max(p.values()),y,max(p,key=p.get)) for p,y in history if lo<=max(p.values())<hi or (b==bins-1 and max(p.values())<=hi)]
        if vals: out.append({"bin":f"{lo:.1f}-{hi:.1f}","pred":round(sum(v[0] for v in vals)/len(vals),3),"actual":round(sum(v[2]==v[1] for v in vals)/len(vals),3),"n":len(vals)})
    return out
