"""Moteur de stress tests, de scénarios et de VaR du terminal.

Indépendant de toute interface : il reçoit un portefeuille (:mod:`stress_engine.portfolio`) et un
état de marché (:mod:`stress_engine.market`, construit depuis Rates & Bond Analytics) et ne fait
aucun appel réseau. Phase 2 : projection des positions sur les facteurs (:mod:`stress_engine.mapping`).
"""

from stress_engine.config import EngineConfig, StressConfigError
from stress_engine.exposures import Exposure, MappingMethod, MappingResult, PositionMapping
from stress_engine.factors import Factor, FactorFamily, FactorSpace
from stress_engine.mapping import Mapper, map_portfolio
from stress_engine.market import MarketState
from stress_engine.portfolio import (
    AssetClass,
    Holding,
    InstrumentType,
    StressPortfolio,
    load_lookthrough,
    load_portfolio,
)

__all__ = [
    "AssetClass",
    "EngineConfig",
    "Exposure",
    "Factor",
    "FactorFamily",
    "FactorSpace",
    "Holding",
    "InstrumentType",
    "Mapper",
    "MappingMethod",
    "MappingResult",
    "MarketState",
    "PositionMapping",
    "StressConfigError",
    "StressPortfolio",
    "load_lookthrough",
    "load_portfolio",
    "map_portfolio",
]
__version__ = "0.1.0"
