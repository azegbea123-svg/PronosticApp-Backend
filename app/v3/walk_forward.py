from datetime import datetime
from typing import List,Dict,Any
from .engine import V3Engine
from .scoring import score_predictions

def outcome(h,a): return 'V1' if h>a else ('NUL' if h==a else 'V2')

def run(rows:List[Dict[str,Any]],min_history:int=20,rolling:int=0):
    data=sorted(rows,key=lambda r:r.get('kickoff',''))
    eng=V3Engine(); results=[]
    for i,row in enumerate(data):
        if i<min_history: continue
        train=data[max(0,i-rolling):i] if rolling else data[:i]
        # Rebuild model state exclusively from prior observations.
        eng=V3Engine()
        for tr in train:
            if tr.get('prediction_snapshot'):
                eng.learn(tr['prediction_snapshot'],tr['outcome'])
        pred=eng.predict(row.get('home',''),row.get('away',''),row.get('home_stats',[]),row.get('away_stats',[]),row.get('home_elo'),row.get('away_elo'),row.get('odds'),row.get('external_probs'),learn=True)
        out=row.get('outcome') or outcome(int(row.get('home_goals',0)),int(row.get('away_goals',0)))
        results.append({'kickoff':row.get('kickoff'),'home':row.get('home'),'away':row.get('away'),'prediction':max(pred['probabilities'],key=pred['probabilities'].get),'outcome':out,'probs':pred['probabilities'],'weights':pred['weights'],'no_bet':pred['no_bet']})
        eng.learn(pred,out)
    return {'engine_version':'V3','evaluation':score_predictions(results),'predictions':results,'method':'expanding_walk_forward' if not rolling else f'rolling_{rolling}','lookahead_protection':True}
