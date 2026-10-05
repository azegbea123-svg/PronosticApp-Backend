import math
from typing import Dict, Tuple
from .utils import normalize


def poisson_pmf(k:int, lam:float)->float:
    return math.exp(-lam) * lam**k / math.factorial(k)

def tau(x:int,y:int,l1:float,l2:float,rho:float)->float:
    if x==0 and y==0: return 1-l1*l2*rho
    if x==0 and y==1: return 1+l1*rho
    if x==1 and y==0: return 1+l2*rho
    if x==1 and y==1: return 1-rho
    return 1.0

def matrix(l1:float,l2:float,rho:float=-0.13,max_goals:int=7)->Dict[Tuple[int,int],float]:
    raw={}
    for h in range(max_goals+1):
        for a in range(max_goals+1):
            raw[(h,a)]=poisson_pmf(h,max(l1,0.05))*poisson_pmf(a,max(l2,0.05))*tau(h,a,l1,l2,rho)
    s=sum(max(v,0.0) for v in raw.values())
    return {k:max(v,0.0)/s for k,v in raw.items()}

def markets(m:Dict[Tuple[int,int],float])->Dict[str,float]:
    p1=sum(v for (h,a),v in m.items() if h>a); px=sum(v for (h,a),v in m.items() if h==a); p2=sum(v for (h,a),v in m.items() if h<a)
    btts=sum(v for (h,a),v in m.items() if h>0 and a>0)
    over={f"Over{n}5":sum(v for (h,a),v in m.items() if h+a>n) for n in (0,1,2)}
    return {"V1":p1,"NUL":px,"V2":p2,"BTTS":btts,**over}
