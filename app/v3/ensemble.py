from typing import Dict
from .utils import normalize

def mix(probabilities:Dict[str,Dict[str,float]], weights:Dict[str,float]):
    out={k:0.0 for k in ('V1','NUL','V2')}
    for name,p in probabilities.items():
        w=weights.get(name,0)
        for k in out: out[k]+=w*p.get(k,0)
    return normalize(out)
