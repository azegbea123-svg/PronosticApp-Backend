from typing import Dict
from .utils import normalize

def from_odds(odds:Dict[str,float]|None)->Dict[str,float]|None:
    if not odds: return None
    try:
        inv={"V1":1/float(odds['1']),"NUL":1/float(odds['X']),"V2":1/float(odds['2'])}
        return normalize(inv)
    except Exception: return None
