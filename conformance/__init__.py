"""Offline CMP adapter conformance contract and reference fixture."""

from .provider import ProviderBoundary
from .reference import AdapterError, LocalAdapter
from .test_conformance import run_suite

__all__ = ["AdapterError", "LocalAdapter", "ProviderBoundary", "run_suite"]

