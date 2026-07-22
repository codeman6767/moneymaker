"""Shared fixtures: train each sport's artifacts once per test session."""

from __future__ import annotations

import pytest

from probability import train_and_build


@pytest.fixture(scope="session")
def mlb_artifacts():
    return train_and_build("mlb", n=8000, budget_ns=1_000_000)


@pytest.fixture(scope="session")
def nba_artifacts():
    return train_and_build("nba", n=8000, budget_ns=1_000_000)
