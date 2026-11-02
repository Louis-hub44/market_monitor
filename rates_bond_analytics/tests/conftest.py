"""Shared pytest fixtures and QuantLib conversion helpers."""

from __future__ import annotations

import random
from datetime import date

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "oracle: comparison against QuantLib (test-only oracle)")
    config.addinivalue_line("markers", "network: requires internet access (skipped by default)")


@pytest.fixture
def rng() -> random.Random:
    """Deterministic RNG for randomised oracle tests."""
    return random.Random(20261007)


def random_date(rng: random.Random, start: date = date(2000, 1, 1), end: date = date(2060, 12, 31)) -> date:
    """Uniform random date in ``[start, end]``."""
    return date.fromordinal(rng.randint(start.toordinal(), end.toordinal()))
