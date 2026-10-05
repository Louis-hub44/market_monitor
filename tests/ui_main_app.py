"""AppTest script: the real ``main()`` with the repository configuration, synthetic data."""

from market_monitor.monitor import MarketMonitor
from market_monitor.referential import load_referential
from market_monitor.ui.page import main
from tests.synthetic import synthetic_monitor


def _fake_from_settings(cls, settings):  # noqa: ANN001, ANN202
    return synthetic_monitor(load_referential(settings.instruments_path, settings.watchlists_path))


MarketMonitor.from_settings = classmethod(_fake_from_settings)  # type: ignore[method-assign]
main()
