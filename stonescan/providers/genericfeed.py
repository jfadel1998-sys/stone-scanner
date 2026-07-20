"""Generic feed ingester — the long-tail provider for any catalog that publishes a
product sitemap + schema.org Product JSON-LD (e.g. WooCommerce sites like Marble
Systems). No bespoke API: discover the sitemap from robots.txt, expand it, and read
each product's `<script type="application/ld+json">` Product object.

Robots-clean by construction: robots.txt is the entry point, its Sitemap: lines are
followed, and every product URL is checked before it is fetched — now through the
shared `stonescan.robots` engine, so this provider obeys the same RFC 9309 rules
(named agent groups, Allow/Disallow precedence, Crawl-delay) as everything else
rather than its own simplified reading. Product-level only (breadth, not live slab
quantities), so a run is bounded by `max_products` (raise it per-entry once you
trust a site).

suppliers.json entry:
    {"host": "www.marblesystems.com", "provider": "genericfeed",
     "product_pattern": "/product/", "max_products": 800}   # pattern/max optional
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from ..robots import client_for
from .base import SupplierData, material_row

UA = "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"
_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_LD = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _expand(client: httpx.AsyncClient, sitemaps: list[str], seen: set[str],
                  depth: int = 0) -> list[str]:
    """Recursively expand sitemap indexes into page URLs."""
    urls: list[str] = []
    for sm in sitemaps:
        if sm in seen or depth > 3:
            continue
        seen.add(sm)
        try:
            body = (await client.get(sm, timeout=45)).text
        except Exception:  # noqa: BLE001
            continue
        locs = _LOC.findall(body)
        # A sitemap index points at more .xml sitemaps; a urlset points at pages.
        children = [l for l in locs if l.lower().endswith(".xml")]
        if children:
            urls += await _expand(client, children, seen, depth + 1)
        else:
            urls += locs
    return urls


def _price(offers: Any) -> str:
    """Pull the first price out of schema.org offers (WooCommerce nests it oddly:
    priceSpecification can be a dict keyed by "0")."""
    for off in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(off, dict):
            continue
        if off.get("price"):
            return f"${off['price']}"
        spec = off.get("priceSpecification")
        for s in (spec.values() if isinstance(spec, dict) else
                  (spec if isinstance(spec, list) else [spec])):
            if isinstance(s, dict) and s.get("price"):
                return f"${s['price']}"
    return ""


def _products(html: str) -> list[dict]:
    """Every schema.org Product object on a page (handles @graph and arrays)."""
    out = []
    for block in _LD.findall(html):
        try:
            obj = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        graph = obj.get("@graph") if isinstance(obj, dict) and "@graph" in obj else obj
        for o in (graph if isinstance(graph, list) else [graph]):
            if isinstance(o, dict) and "product" in str(o.get("@type", "")).lower():
                out.append(o)
    return out


def _form(name: str) -> str:
    n = name.lower()
    for kw, form in (("slab", "SLAB"), ("mosaic", "MOSAIC"), ("tile", "TILE")):
        if kw in n:
            return form
    return ""


async def crawl(entry: dict, *, with_slabs: bool = False, delay: float = 0.5,
                limit: int = 0, **_kw) -> SupplierData:
    host = entry["host"]
    out = SupplierData(host=host, company=entry.get("name") or "")
    crawled_at = _now()
    pattern = entry.get("product_pattern", "/product/")
    cap = limit or int(entry.get("max_products", 600))
    try:
        async with client_for(entry, headers={"User-Agent": UA},
                              follow_redirects=True) as client:
            policy = await client.robots.policy(f"https://{host}/")
            sitemaps = list(policy.sitemaps) or [
                f"https://{host}/sitemap_index.xml", f"https://{host}/sitemap.xml"]
            # A site that asks for slower crawling gets it.
            delay = max(delay, policy.crawl_delay)
            all_urls = await _expand(client, sitemaps, set())
            # Product pages only, and only those robots permits. The client would
            # refuse a disallowed URL anyway; filtering first means a blocked page
            # doesn't burn one of the `cap` slots and leave the run short.
            product_urls, seen = [], set()
            for u in all_urls:
                if pattern not in u or u in seen:
                    continue
                if not await client.robots.allowed(u):
                    continue
                seen.add(u)
                product_urls.append(u)
            product_urls = product_urls[:cap]

            for u in product_urls:
                try:
                    html = (await client.get(u, timeout=45)).text
                except Exception:  # noqa: BLE001
                    continue
                for p in _products(html):
                    name = (p.get("name") or "").strip()
                    if not name:
                        continue
                    material = p.get("material") or ""
                    out.materials.append(material_row(
                        name=name, crawled_at=crawled_at,
                        item_id=str(p.get("sku") or p.get("productID") or ""),
                        category=material,          # "Basalt" -> Basalt, etc.
                        subcategory=str(p.get("category") or ""),
                        color=p.get("color") or "",
                        product_form=_form(name),
                        sku=str(p.get("sku") or ""),
                        price_range=_price(p.get("offers")),
                        image_url=(p.get("image") if isinstance(p.get("image"), str)
                                   else (p.get("image") or [""])[0] if p.get("image") else ""),
                        source_url=u,
                    ))
                await asyncio.sleep(delay)
        out.ok = bool(out.materials)
        if not out.ok:
            out.error = "no products found via sitemap/JSON-LD"
    except Exception as e:  # noqa: BLE001 - one provider must not kill the crawl
        out.error = f"{type(e).__name__}: {e}"
    return out
