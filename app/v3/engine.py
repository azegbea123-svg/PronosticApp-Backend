from typing import Dict,Optional,Any,List
from .data_engine import quality,feature_row
from .dixon_coles import matrix as dc_matrix, markets as dc_markets
from .bivariate_poisson import matrix as bp_matrix
from .elo_model import probs as elo_probs
from .logistic_model import MultinomialLogistic
from .market_model import from_odds
from .ensemble import mix
from .adaptive_weights import AdaptiveWeights
from .calibration import fit_temperature,temperature
from .no_bet import decide

class V3Engine:
    def __init__(self):
        self.model=MultinomialLogistic()
        self.adaptive=AdaptiveWeights(['DIXON_COLES','BIVARIATE_POISSON','ELO','LOGISTIC','MARKET','EXTERNAL'])
        self.calibration_history=[]
    def _lambdas(self,s1,s2,elo1=None,elo2=None):
        q,a,b=quality(s1,s2,elo1 is not None and elo2 is not None)
        a=a or {'games':0,'gf':0,'ga':0}; b=b or {'games':0,'gf':0,'ga':0}
        ag=max(a['games'],1); bg=max(b['games'],1)
        league=1.35
        att1=(a['gf']/ag + league)/2; def2=(b['ga']/bg + league)/2
        att2=(b['gf']/bg + league)/2; def1=(a['ga']/ag + league)/2
        l1=max(0.25,min(3.5,att1*def2*1.12)); l2=max(0.20,min(3.2,att2*def1))
        if elo1 is not None and elo2 is not None:
            ep=elo_probs(elo1,elo2); l1*=0.82+0.36*ep['V1']; l2*=0.82+0.36*ep['V2']
        return l1,l2,q
    def predict(self,equipe1,equipe2,stats1,stats2,elo1=None,elo2=None,odds=None,external=None,learn=True):
        l1,l2,q=self._lambdas(stats1,stats2,elo1,elo2)
        dc=dc_matrix(l1,l2); bp=bp_matrix(l1,l2)
        dm=dc_markets(dc); bm=dc_markets(bp)
        ep=elo_probs(elo1,elo2) if elo1 is not None and elo2 is not None else None
        feat=feature_row(stats1,stats2,elo1,elo2,q); lp=self.model.predict(feat)
        mp=from_odds(odds); xp=external
        models={'DIXON_COLES':{k:dm[k] for k in ('V1','NUL','V2')},'BIVARIATE_POISSON':{k:bm[k] for k in ('V1','NUL','V2')},'LOGISTIC':lp}
        if ep: models['ELO']=ep
        if mp: models['MARKET']=mp
        if xp: models['EXTERNAL']=xp
        weights=self.adaptive.weights(); weights={k:v for k,v in weights.items() if k in models}; sw=sum(weights.values()) or 1; weights={k:v/sw for k,v in weights.items()}
        ens=mix(models,weights)
        T=fit_temperature(self.calibration_history) if len(self.calibration_history)>=30 else 1.0
        cal=temperature(ens,T)
        decision=decide(cal,models,q)
        top_scores=sorted(dc.items(),key=lambda kv:kv[1],reverse=True)[:5]
        result={'engine_version':'V3','probabilities':{k:round(v,4) for k,v in cal.items()},'raw_probabilities':{k:round(v,4) for k,v in ens.items()},'weights':{k:round(v,4) for k,v in weights.items()},'models':{m:{k:round(v,4) for k,v in p.items()} for m,p in models.items()},'quality':q,'temperature':T,'no_bet':decision,'expected_goals':{'home':round(l1,3),'away':round(l2,3)},'scores':[{'score':f'{h}-{a}','probability':round(p,4)} for (h,a),p in top_scores]}
        if learn:
            result['_models']=models
        return result
    def learn(self,prediction:Dict[str,Any],outcome:str):
        self.adaptive.update(prediction.get('_models',{}),outcome)
        self.calibration_history.append((prediction['raw_probabilities'],outcome)); self.calibration_history=self.calibration_history[-500:]
