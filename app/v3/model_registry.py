from .engine import V3Engine
ENGINE_VERSION='V3.1.0'
ENGINE_DESCRIPTION='Adaptive ensemble + multi-source provenance/deduplication; Dixon-Coles, Bivariate Poisson, Elo, Logistic, Market, External, calibration and no-bet.'
_engine=V3Engine()
def get_engine(): return _engine
