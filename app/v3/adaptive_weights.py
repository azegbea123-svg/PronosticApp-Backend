import math
from typing import Dict, Iterable
from .utils import CLASSES

class AdaptiveWeights:
    def __init__(self, model_names:Iterable[str], eta:float=3.0, floor:float=0.05, prior:Dict[str,float]|None=None):
        self.models=list(model_names); self.eta=eta; self.floor=floor
        self.losses={m:[] for m in self.models}
        self.prior=prior or {m:1/len(self.models) for m in self.models}
    def update(self, predictions:Dict[str,Dict[str,float]], outcome:str):
        for m,p in predictions.items():
            if m in self.losses:
                q=max(min(float(p.get(outcome,1e-9)),1-1e-9),1e-9)
                self.losses[m].append(-math.log(q))
                self.losses[m]=self.losses[m][-200:]
    def weights(self)->Dict[str,float]:
        if not self.models: return {}
        scores=[]
        for m in self.models:
            ls=self.losses[m]
            avg=sum(ls[-100:])/len(ls) if ls else -math.log(max(self.prior.get(m,1e-9),1e-9))
            scores.append(-self.eta*avg)
        mx=max(scores); raw=[math.exp(s-mx) for s in scores]; s=sum(raw)
        w={m:r/s for m,r in zip(self.models,raw)}
        if self.floor*len(self.models)<1:
            rem=1-self.floor*len(self.models)
            base={m:max(w[m]-self.floor,0) for m in self.models}; bs=sum(base.values()) or 1
            return {m:self.floor+rem*base[m]/bs for m in self.models}
        return {m:1/len(self.models) for m in self.models}
    def diagnostics(self):
        return {m:{"n":len(self.losses[m]),"log_loss_recent":round(sum(self.losses[m][-100:])/max(1,len(self.losses[m][-100:])),4)} for m in self.models}
