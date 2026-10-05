from typing import Any,Dict,List,Optional

def _stats(src):
    if not src: return None
    # Deduplicate using source-provided detail when possible.
    details=[]; seen=set()
    for s in src:
        for m in s.get('matchs_detail',[]) or []:
            key=(m.get('adversaire_slug'),m.get('domicile'),m.get('jour_annee_approx'),m.get('buts_pour'),m.get('buts_contre'))
            if key==(None,None,None,None,None) or key not in seen:
                details.append(m); seen.add(key)
    games=len(details) or sum(int(s.get('matchs_analyses',0)) for s in src)
    gf=sum(float(m.get('buts_pour',0)) for m in details) if details else sum(float(s.get('buts_marques',0)) for s in src)
    ga=sum(float(m.get('buts_contre',0)) for m in details) if details else sum(float(s.get('buts_encaisses',0)) for s in src)
    return {'games':games,'gf':gf,'ga':ga,'details':details,'sources':sorted(set(s.get('source','?') for s in src))}

def quality(s1,s2,elo_available=False,market_available=False):
    a,b=_stats(s1),_stats(s2); score=0
    for s in (a,b):
        if s:
            score += min(22, s['games']*1.6)
            score += min(8, len(s['sources'])*4)
    if elo_available: score+=18
    if market_available: score+=12
    return round(min(100,score),1),a,b

def feature_row(s1,s2,elo1=None,elo2=None,quality_score=50):
    q1=_stats(s1) or {'games':0,'gf':0,'ga':0}; q2=_stats(s2) or {'games':0,'gf':0,'ga':0}
    g1=max(q1['games'],1); g2=max(q2['games'],1)
    return {'elo_diff':(elo1 or 1500)-(elo2 or 1500),'attack_diff':q1['gf']/g1-q2['gf']/g2,'defense_diff':q2['ga']/g2-q1['ga']/g1,'form_diff':(q1['gf']-q1['ga'])/g1-(q2['gf']-q2['ga'])/g2,'home_strength':1.0,'data_quality':quality_score}
