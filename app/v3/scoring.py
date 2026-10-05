from typing import Dict,List
from .utils import brier,log_loss

def score_predictions(items:List[Dict]):
    if not items: return {"n":0}
    n=len(items); acc=sum(i['prediction']==i['outcome'] for i in items)/n
    bs=sum(brier(i['probs'],i['outcome']) for i in items)/n
    ll=sum(log_loss(i['probs'],i['outcome']) for i in items)/n
    return {"n":n,"accuracy":round(acc,4),"brier":round(bs,5),"log_loss":round(ll,5)}
