"""Catalog providers.

A supplier entry in suppliers.json may name a `provider`; entries without one
are Stone Profits, which is the original (and still the largest) source.

    {"host": "umistone.com", "name": "UMI Stone", "provider": "umi"}

Stone Profits keeps its own crawl path in ingest.py because it needs Playwright
and per-host concurrency. Everything registered here is plain HTTP and shares
the simpler `crawl(entry) -> SupplierData` contract in base.py.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .base import SupplierData

STONEPROFITS = "stoneprofits"


def _load(name: str) -> Callable[..., Awaitable[SupplierData]]:
    from importlib import import_module
    return import_module(f".{name}", __package__).crawl


# provider name -> lazy loader (keeps httpx/playwright imports off the hot path)
REGISTRY: dict[str, Callable[[], Callable[..., Awaitable[SupplierData]]]] = {
    "umi": lambda: _load("umi"),
    "slabware": lambda: _load("slabware"),
    "stonetrash": lambda: _load("stonetrash"),
    "slabcloud": lambda: _load("slabcloud"),
}


def provider_of(entry: dict[str, Any]) -> str:
    return (entry.get("provider") or STONEPROFITS).strip().lower()


def get(name: str) -> Callable[..., Awaitable[SupplierData]]:
    if name not in REGISTRY:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(REGISTRY) + [STONEPROFITS]}")
    return REGISTRY[name]()


__all__ = ["SupplierData", "REGISTRY", "STONEPROFITS", "get", "provider_of"]
