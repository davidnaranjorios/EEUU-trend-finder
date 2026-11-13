"""Fake de econcensus para el test de contrato (ver tests/fakes_market.py)."""

from tests.fakes_market import bump_econcensus as bump
from tests.fakes_market import make_econcensus as make_adapter

__all__ = ["bump", "make_adapter"]
