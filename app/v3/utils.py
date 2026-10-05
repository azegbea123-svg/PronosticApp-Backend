import math
from typing import Dict, List

CLASSES = ("V1", "NUL", "V2")
EPS = 1e-9

def normalize(p: Dict[str,float]) -> Dict[str,float]:
    vals = {k:max(float(p.get(k,0.0)), EPS) for k in CLASSES}
    s=sum(vals.values())
    return {k: vals[k]/s for k in CLASSES}

def log_loss(p: Dict[str,float], outcome: str) -> float:
    return -math.log(max(normalize(p).get(outcome, EPS), EPS))

def brier(p: Dict[str,float], outcome: str) -> float:
    q=normalize(p)
    return sum((q[k]-(1.0 if k==outcome else 0.0))**2 for k in CLASSES)

def entropy(p: Dict[str,float]) -> float:
    q=normalize(p)
    return -sum(v*math.log(v) for v in q.values())/math.log(3)

def softmax(xs: List[float]) -> List[float]:
    m=max(xs); ex=[math.exp(x-m) for x in xs]; s=sum(ex)
    return [v/s for v in ex]
