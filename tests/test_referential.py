from pathlib import Path

import pytest

from market_monitor.referential import (
    AssetClass,
    ChangeUnit,
    QuoteType,
    ReferentialError,
    load_referential,
    referential_from_dicts,
)

CONFIG = Path(__file__).resolve().parents[1] / "config"

BASE = {
    "defaults": {"rates": {"quote": "yield", "unit": "%"}, "equity": {"quote": "price"}},
    "instruments": {
        "SX5E": {"name": "Euro Stoxx 50", "class": "equity", "group": "Europe",
                 "tickers": {"bloomberg": "SX5E Index", "free": "^STOXX50E"}},
        "OAT_10Y": {"name": "OAT 10Y", "class": "rates", "tickers": {"bloomberg": "GFRN10 Index"}},
        "BUND_10Y": {"name": "Bund 10Y", "class": "rates",
                     "tickers": {"bloomberg": "GDBR10 Index", "free": {"ticker": "X", "scale": 0.1}}},
        "OAT_BUND": {"name": "OAT-Bund", "class": "rates", "quote": "spread", "unit": "pb",
                     "derived": {"multiplier": 100, "legs": {"OAT_10Y": 1, "BUND_10Y": -1}}},
    },
}


def _with(instrument_id, raw):
    data = {"defaults": BASE["defaults"], "instruments": {**BASE["instruments"], instrument_id: raw}}
    return referential_from_dicts(data, {})


def test_repository_yaml_is_valid():
    ref = load_referential(CONFIG / "instruments.yaml", CONFIG / "watchlists.yaml")
    assert len(ref) > 60
    assert {"home", "daily_macro"} <= set(ref.watchlist_names)
    for instrument_id in ref.ids:  # every instrument is reachable by at least one provider
        assert ref.symbol_map([instrument_id])


def test_defaults_conventions_and_derived():
    ref = referential_from_dicts(BASE, {})
    bund, spread, sx5e = ref.get("BUND_10Y"), ref.get("OAT_BUND"), ref.get("SX5E")
    assert (bund.quote, bund.change, bund.bp_factor) == (QuoteType.YIELD, ChangeUnit.BP, 100.0)
    assert (spread.change, spread.bp_factor) == (ChangeUnit.BP, 1.0)
    assert sx5e.change is ChangeUnit.PCT and sx5e.asset_class is AssetClass.EQUITY
    assert bund.tickers["free"].scale == 0.1
    assert ref.raw_dependencies(["OAT_BUND", "SX5E"]) == ("OAT_10Y", "BUND_10Y", "SX5E")
    assert ref.symbol_map(["OAT_BUND"]) == {"OAT_10Y": {"bloomberg": "GFRN10 Index"},
                                           "BUND_10Y": {"bloomberg": "GDBR10 Index", "free": "X"}}


def test_selectors():
    ref = referential_from_dicts(BASE, {})
    assert ref.resolve(["class:rates"]) == ("OAT_10Y", "BUND_10Y", "OAT_BUND")
    assert ref.resolve(["group:Europe", "SX5E", "*"])[:2] == ("SX5E", "OAT_10Y")
    with pytest.raises(ReferentialError):
        ref.resolve(["NOPE"])


@pytest.mark.parametrize("raw", [
    {"name": "x", "class": "bonds", "tickers": {"free": "X"}},
    {"name": "x", "class": "equity", "change": "bp", "tickers": {"free": "X"}},
    {"name": "x", "class": "equity", "tickers": {"reuters": "X"}},
    {"name": "x", "class": "equity", "tickers": {"free": " X"}},
    {"name": "x", "class": "equity", "tickers": {"free": {"ticker": "X", "scale": 0}}},
    {"name": "x", "class": "equity"},
    {"name": "", "class": "equity", "tickers": {"free": "X"}},
    {"name": "x", "class": "rates", "tickers": {"free": "X"},
     "derived": {"legs": {"OAT_10Y": 1, "BUND_10Y": -1}}},
    {"name": "x", "class": "rates", "derived": {"legs": {"OAT_10Y": 1}}},
    {"name": "x", "class": "rates", "derived": {"legs": {"OAT_10Y": 1, "GHOST": -1}}},
    {"name": "x", "class": "rates", "derived": {"legs": {"OAT_BUND": 1, "BUND_10Y": -1}}},
    {"name": "x", "class": "rates", "derived": {"legs": {"SX5E": 1, "BUND_10Y": -1}}},
])
def test_invalid_instruments(raw):
    with pytest.raises(ReferentialError):
        _with("NEW", raw)


def test_lowercase_id_rejected_and_errors_aggregated():
    data = {"instruments": {"bad id": {"name": "x", "class": "fx", "tickers": {"free": "X"}},
                            "OTHER": {"name": "y", "class": "nope"}}}
    with pytest.raises(ReferentialError) as info:
        referential_from_dicts(data, {})
    assert "bad id" in str(info.value) and "OTHER" in str(info.value)


def test_watchlists_are_validated():
    ok = referential_from_dicts(BASE, {"watchlists": {"wl": {"instruments": ["class:rates"]}}})
    assert ok.watchlist("wl").instruments == ("class:rates",)
    with pytest.raises(ReferentialError):
        referential_from_dicts(BASE, {"watchlists": {"wl": {"instruments": ["GHOST"]}}})
    with pytest.raises(ReferentialError):
        referential_from_dicts(BASE, {"watchlists": {"wl": {"instruments": []}}})
    with pytest.raises(ReferentialError):
        ok.watchlist("missing")
