from typing import Dict
from .utils import normalize

def probs(elo_home:float, elo_away:float, home_advantage:float=55.0)->Dict[str,float]:
    d=(elo_home+home_advantage)-elo_away
    p_home=1/(1+10**(-d/400))
    # derive draw probability smoothly around balanced games
    p_draw=0.26 + 0.10*(1-abs(2*p_home-1))
    p_draw=min(0.34,max(0.20,p_draw))
    p_home=(1-p_draw)*p_home
    return normalize({"V1":p_home,"NUL":p_draw,"V2":1-p_draw-p_home})

def update(elo_home:float, elo_away:float, outcome:str, k:float=20.0, home_advantage:float=55.0):
    p=probs(elo_home,elo_away,home_advantage)
    actual={"V1":1.0,"NUL":0.5,"V2":0.0}[outcome]
    expected=p["V1"]+0.5*p["NUL"]
    delta=k*(actual-expected)
    return elo_home+delta, elo_away-delta
