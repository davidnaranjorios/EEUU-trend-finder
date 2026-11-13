"""Fake de cbp para el test de contrato (ver tests/fakes_market.py)."""

from tests.fakes_market import bump_cbp as bump
from tests.fakes_market import make_cbp as make_adapter

__all__ = ["bump", "make_adapter"]
