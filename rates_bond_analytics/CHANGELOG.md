# Changelog

## 1.1.0 – corporate network, free data, UI
- `rates_analytics.network`: corporate SSL-proxy support ported from Market Monitor, with four modes: `system` (Windows certificate store through `truststore`, the default), `ca`, `insecure` and `standard`. One configured session is shared by every REST provider, and connectivity diagnostics name the dominant cause of failure.
- New `public` provider (CNBC → Bundesbank → Stooq, keyless, circuit breaker): generic EGB yields (DE/FR/IT/ES 2-30Y) and the Bundesbank Svensson zero curve.
- Proxy swap curves (`curves/proxy.py`): without OIS / IRS quotes, €STR = ECB AAA Svensson + spread anchored on the €STR fixing, and EURIBOR 6M = OIS + basis (assumptions in `config.yaml`), flagged everywhere. Sovereign curves fall back to Nelson-Siegel fits on generic yields.
- Dashboard: start screen (source + SSL mode + source test before any request), SSL rescue banner, terminal theme shared with Market Monitor (KPI tiles, provenance chips, restyled charts).

## 1.0.0 – phase 10 (tests, documentation, polish)
- `rates_analytics.api`: stable integration API (snapshot, DF / curve grid export, JSON round-trip of curves, bond table, swap risk) for Valuation Screener, Pricing Lab, PTF Event Radar, Portfolio Intelligence and CVA Calculator.
- Fix: an IRS whose first fixing falls on the curve date now uses the published fixing in `npv` / `fair_rate` as in the SWPM leg tables (found by the new invariant tests).
- Tests: integration API, cross-module invariants (par swap, payer/receiver symmetry, scenario vs DV01, price ↔ yield ↔ Z-spread), real `BlpapiClient` against a fake `blpapi`.
- Quality: ruff (lint + isort), mypy clean on the engine, coverage report, GitHub Actions (Windows + Linux, Python 3.11 / 3.12).
- Dashboard service layer now built on `api.market_snapshot`.

## 0.9.0 – phase 9: Streamlit dashboard (GC, sovereigns, YAS, SWPM, scenarios, data)
## 0.8.0 – phase 8: curve scenarios (quotes / zero modes, IRRBB), portfolio P&L
## 0.7.0 – phase 7: SWPM pricer, seasoned swaps with fixings, bucketed risk (Jacobian vs rebuild)
## 0.6.0 – phase 6: G / I / Z / ASW spreads
## 0.5.0 – phase 5: YAS analytics (street / true yield, durations, KRD)
## 0.4.0 – phase 4: NSS sovereign curves (Bund, OAT, BTP), rich / cheap
## 0.3.0 – phase 3: EURIBOR 6M dual curve
## 0.2.0 – phase 2: €STR OIS bootstrap (log-linear, monotone convex)
## 0.1.0 – phase 1: architecture, conventions, data layer
