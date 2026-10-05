import math
from typing import Dict, Tuple

# A lightweight bivariate-Poisson approximation. Shared scoring intensity is
# estimated from the two expected-goal rates; the joint mass is normalized on
# the finite score grid, which keeps it stable without scipy.
def matrix(l1:float,l2:float,shared:float=None,max_goals:int=7)->Dict[Tuple[int,int],float]:
    l1=max(l1,0.05); l2=max(l2,0.05)
    if shared is None: shared=min(0.10, 0.08*min(l1,l2))
    a=max(l1-shared,0.02); b=max(l2-shared,0.02); c=max(shared,0.0)
    raw={}
    for x in range(max_goals+1):
        for y in range(max_goals+1):
            total=0.0
            for k in range(min(x,y)+1):
                total += (math.exp(-a)*a**(x-k)/math.factorial(x-k))*(math.exp(-b)*b**(y-k)/math.factorial(y-k))*(math.exp(-c)*c**k/math.factorial(k) if c else (1.0 if k==0 else 0.0))
            raw[(x,y)]=total
    s=sum(raw.values())
    return {k:v/s for k,v in raw.items()}
