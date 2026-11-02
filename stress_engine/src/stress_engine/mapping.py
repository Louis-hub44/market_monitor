"""Projection d'un portefeuille sur les facteurs de risque (phase 2).

    from stress_engine.mapping import map_portfolio
    result = map_portfolio(portfolio, market, config)        # MappingResult
    result.by_factor()                                        # deltas agrégés par facteur
    result.summary()                                          # méthode, qualité, DV01, CS01 par ligne

Une ligne qui ne peut pas être projetée n'interrompt rien : elle est marquée ``EXCLUDED`` avec sa
raison, et la couverture du calcul (part de l'exposition projetée) l'indique.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from rates_analytics.exceptions import RatesAnalyticsError

from stress_engine.config import EngineConfig, StressConfigError
from stress_engine.exposures import Exposure, MappingMethod, MappingResult, PositionMapping
from stress_engine.factors import FactorSpace
from stress_engine.instruments.bonds import BondMappingError, map_bond
from stress_engine.instruments.funds import (
    FundMappingError,
    fund_value,
    map_direct,
    map_lookthrough,
    map_money_market,
    map_proxy,
)
from stress_engine.instruments.futures import FutureMappingError, contract_specs, map_future
from stress_engine.market import MarketState
from stress_engine.portfolio import Holding, InstrumentType, StressPortfolio

MAPPING_ERRORS = (BondMappingError, FutureMappingError, FundMappingError, StressConfigError, RatesAnalyticsError,
                  ValueError)
MAX_LOOKTHROUGH_DEPTH = 2


class Mapper:
    """Projette des lignes sur un :class:`FactorSpace` avec un :class:`MarketState` donné."""

    def __init__(self, market: MarketState, config: EngineConfig | None = None,
                 lookthrough: Mapping[str, Sequence[tuple[Holding, float]]] | None = None,
                 space: FactorSpace | None = None) -> None:
        self.config = config or EngineConfig.load()
        self.space = space or FactorSpace.from_config(self.config.factors)
        self.market = market
        self.lookthrough = dict(lookthrough or {})
        self.specs = contract_specs(self.config.eurex)
        self.proxies = dict(self.config.funds.get("proxies") or {})

    # ------------------------------------------------------------------ change
    def _fx(self, holding: Holding) -> tuple[float, str | None]:
        base = self.space.base_currency
        if holding.currency.upper() == base:
            return 1.0, None
        rate = holding.fx_rate if holding.fx_rate is not None else self.market.fx_to_base(holding.currency, base)
        if rate is None:
            raise StressConfigError(f"taux de change {holding.currency}/{base} inconnu (colonne fx_rate ou "
                                    "état de marché)")
        factor = f"FX.{holding.currency.upper()}"
        self.space.ensure(factor)
        return float(rate), factor

    # ------------------------------------------------------------------ une ligne
    def map_holding(self, holding: Holding, depth: int = 0) -> PositionMapping:
        try:
            return self._map(holding, depth)
        except MAPPING_ERRORS as exc:
            return PositionMapping(holding=holding, method=MappingMethod.EXCLUDED,
                                   exposure_value=_rough_value(holding), notes=[f"exclue : {exc}"])

    def _map(self, h: Holding, depth: int) -> PositionMapping:
        fx, fx_factor = self._fx(h)
        if h.instrument is InstrumentType.BOND:
            m = map_bond(h, self.space, self.market, fx=fx)
            if fx_factor:
                m.exposures.append(Exposure(fx_factor, m.market_value))
            return m
        if h.instrument is InstrumentType.FUTURE:
            if fx_factor:
                raise FutureMappingError("futures hors devise de base non pris en charge")
            return map_future(h, self.space, self.market, self.specs)
        if h.instrument in (InstrumentType.FUND, InstrumentType.MMF):
            value = fund_value(h) * fx
            if h.lookthrough and h.lookthrough in self.lookthrough:
                if depth >= MAX_LOOKTHROUGH_DEPTH:
                    raise FundMappingError("transparence trop profonde (fonds de fonds de fonds)")
                m = map_lookthrough(h, value, self.lookthrough[h.lookthrough],
                                    lambda child: self.map_holding(_sized(child), depth + 1))
                if fx_factor:
                    m.exposures.append(Exposure(fx_factor, value))
                return m
            notes = []
            if h.lookthrough:
                notes.append(f"composition {h.lookthrough} introuvable : repli sur le profil du fonds")
            if h.instrument is InstrumentType.MMF:
                m = map_money_market(h, self.space, self.config.funds, value, fx_factor)
            elif h.proxy and h.proxy in self.proxies:
                m = map_proxy(h, self.space, self.proxies[h.proxy], value, fx_factor)
            elif h.index:
                m = map_direct(h, self.space, value, fx_factor, "EQUITY")
                m.notes.append(f"fonds sans composition ni profil : traité comme l'indice {h.index}")
            else:
                raise FundMappingError(f"ni composition ni profil proxy ({h.proxy or 'aucun'}) pour ce fonds")
            m.notes = notes + m.notes
            return m
        if h.instrument is InstrumentType.EQUITY:
            return map_direct(h, self.space, _value(h) * fx, fx_factor, "EQUITY")
        if h.instrument is InstrumentType.COMMODITY:
            return map_direct(h, self.space, _value(h) * fx, fx_factor, "COMMODITY")
        if h.instrument is InstrumentType.CASH:
            return map_direct(h, self.space, _value(h) * fx, fx_factor, "CASH")
        raise StressConfigError(f"instrument non pris en charge : {h.instrument}")  # pragma: no cover

    # ------------------------------------------------------------------ portefeuille
    def map_portfolio(self, portfolio: StressPortfolio) -> MappingResult:
        positions = [self.map_holding(h) for h in portfolio]
        warnings = list(portfolio.notes) + list(self.market.warnings)
        result = MappingResult(positions, self.space.base_currency, warnings)
        excluded = result.excluded()
        if excluded:
            warnings.append(f"{len(excluded)} ligne(s) exclue(s) : " + ", ".join(p.id for p in excluded[:6]))
        return result


def _sized(h: Holding) -> Holding:
    """Ligne de composition donnée en poids : taille provisoire, remise à l'échelle par la transparence."""
    if h.instrument is InstrumentType.BOND and h.nominal is None:
        return replace(h, nominal=1_000_000.0)
    if h.instrument is not InstrumentType.BOND and h.market_value is None and h.nominal is None:
        return replace(h, market_value=1_000_000.0)
    return h


def _value(h: Holding) -> float:
    if h.market_value is not None:
        return float(h.market_value)
    if h.nominal is not None and h.price is not None:
        return float(h.nominal) * float(h.price)
    if h.nominal is not None and h.instrument is InstrumentType.CASH:
        return float(h.nominal)
    raise FundMappingError("valorisation absente (market_value, ou quantité × prix)")


def _rough_value(h: Holding) -> float:
    """Ordre de grandeur d'une ligne exclue, pour la couverture (jamais utilisé dans un P&L)."""
    try:
        if h.instrument is InstrumentType.BOND and h.nominal is not None:
            return float(h.nominal) * float(h.price or 100.0) / 100.0
        if h.instrument is InstrumentType.FUTURE and h.lots:
            return abs(float(h.lots)) * 100_000.0
        return abs(_value(h))
    except (FundMappingError, TypeError, ValueError):
        return 0.0


def map_portfolio(portfolio: StressPortfolio, market: MarketState, config: EngineConfig | None = None,
                  lookthrough: Mapping[str, Sequence[tuple[Holding, float]]] | None = None) -> MappingResult:
    return Mapper(market, config, lookthrough).map_portfolio(portfolio)
