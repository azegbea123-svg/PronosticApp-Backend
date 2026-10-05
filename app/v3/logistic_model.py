import math
from typing import Dict, List
from .utils import CLASSES, softmax, normalize

class MultinomialLogistic:
    """Small dependency-free online multinomial logistic model."""
    def __init__(self, lr=0.035, epochs=80, l2=0.01):
        self.lr=lr; self.epochs=epochs; self.l2=l2; self.w=[[0.0]*7 for _ in CLASSES]
    def features(self, x:Dict[str,float])->List[float]:
        return [1.0,x.get('elo_diff',0)/400,x.get('attack_diff',0),x.get('defense_diff',0),x.get('form_diff',0),x.get('home_strength',0),x.get('data_quality',0)/100]
    def fit(self, rows:List[Dict[str,float]], ys:List[str]):
        if not rows: return
        X=[self.features(r) for r in rows]
        for _ in range(self.epochs):
            grad=[[0.0]*7 for _ in CLASSES]
            for x,y in zip(X,ys):
                p=softmax([sum(a*b for a,b in zip(w,x)) for w in self.w])
                for j,c in enumerate(CLASSES):
                    e=p[j]-(1.0 if c==y else 0.0)
                    for k,v in enumerate(x): grad[j][k]+=e*v
            n=max(len(X),1)
            for j in range(3):
                for k in range(7): self.w[j][k]-=self.lr*(grad[j][k]/n+self.l2*self.w[j][k])
    def predict(self,x:Dict[str,float])->Dict[str,float]:
        f=self.features(x)
        return normalize(dict(zip(CLASSES,softmax([sum(a*b for a,b in zip(w,f)) for w in self.w]))))
