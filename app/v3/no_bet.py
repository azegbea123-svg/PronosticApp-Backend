from .utils import entropy

def decide(probs, model_probs, quality, min_top=0.46, min_margin=0.10, max_disagreement=0.18, min_quality=45):
    top=max(probs.values()); ordered=sorted(probs.values(),reverse=True); margin=ordered[0]-ordered[1]
    disagreement=0.0
    if model_probs:
        for k in probs:
            disagreement=max(disagreement, max(abs(p.get(k,0)-probs[k]) for p in model_probs.values()))
    reasons=[]
    if top<min_top: reasons.append('probabilité dominante trop faible')
    if margin<min_margin: reasons.append('écart entre les deux issues principales trop faible')
    if disagreement>max_disagreement: reasons.append('désaccord élevé entre modèles')
    if quality<min_quality: reasons.append('qualité de données insuffisante')
    return {"bet":not reasons,"decision":"BET" if not reasons else "NO_BET","top_probability":round(top,3),"margin":round(margin,3),"model_disagreement":round(disagreement,3),"reasons":reasons}
