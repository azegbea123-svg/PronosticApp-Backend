from .engine import V3Engine
ENGINE_VERSION='V3.3.2'
ENGINE_DESCRIPTION='Adaptive ensemble + multi-source provenance/deduplication + SportAPI7 scheduled events; Dixon-Coles, Bivariate Poisson, Elo, Logistic, Market, External, calibration and no-bet.'
_engine=V3Engine()
def get_engine(): return _engine
