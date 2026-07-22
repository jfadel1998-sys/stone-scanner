"""Unbuilt — public surplus/outlet marketplace (unbuilt.co).

The one public window into Cosentino outlet stock (Dekton / Silestone / Sensa /
Scalea). Unbuilt is a general building-surplus marketplace, so we query per stone
brand and keep only the "Stone Surface Slabs" category.

    GET /api/listings/?q=<brand>&page=<n>&limit=<n>   -> {success, data:[...], pagination}

`/api/listings/` is explicitly `Allow`ed in robots.txt (the rest of `/api/` is
disallowed) and returns JSON with no auth — the same listings customers browse.
Each listing is one physical slab (quantity 1), so search groups them by name.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..robots import client_for
from .base import SupplierData, material_row

API = "https://unbuilt.co/api/listings/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
      "Gecko) Chrome/120.0.0.0 Safari/537.36")
# Cosentino's stone lines. Override per-entry with "brands": [...] in suppliers.json.
BRANDS = ["dekton", "silestone", "sensa", "scalea"]


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _price(v: Any) -> str:
    try:
        return f"${float(v):,.0f}" if v not in (None, "", 0) else ""
    except (TypeError, ValueError):
        return ""


async def crawl(entry: dict, *, with_slabs: bool = False, delay: float = 0.4,
                limit: int = 0, **_kw) -> SupplierData:
    host = entry["host"]
    out = SupplierData(host=host, company=entry.get("name") or "Unbuilt (Cosentino outlet)")
    crawled_at = _now()
    brands = entry.get("brands") or BRANDS
    try:
        # unbuilt.co disallows /api/ but allows /api/listings/ specifically, so this
        # provider only works if the longest-match rule is honored properly.
        async with client_for(entry, headers={"User-Agent": UA},
                              follow_redirects=True) as client:
            seen: set[str] = set()
            for brand in brands:
                page = 1
                while True:
                    r = await client.get(API, params={"q": brand, "page": page, "limit": 50},
                                         timeout=45)
                    r.raise_for_status()
                    d = r.json()
                    for it in d.get("data", []):
                        iid = it.get("id")
                        if not iid or iid in seen:
                            continue
                        cat = (it.get("category_path") or "")
                        if "slab" not in cat.lower():  # keep only stone slabs
                            continue
                        seen.add(iid)
                        dims = it.get("dimensions") or {}
                        loc = it.get("location") or {}
                        img = (it.get("images") or [{}])[0].get("url") or ""
                        out.materials.append(material_row(
                            name=it.get("title") or f"{it.get('brand', '')} {it.get('canonicalColor', '')}".strip(),
                            crawled_at=crawled_at, item_id=str(iid),
                            category=it.get("brand") or "",     # "Dekton" -> Sintered Stone, etc.
                            subcategory=cat,
                            color=it.get("canonicalColor") or "",
                            thickness=str(dims.get("height_thickness") or ""),
                            product_form="SLAB",
                            available_slabs=it.get("quantity") or 1,
                            avg_length=dims.get("length_depth"),
                            avg_width=dims.get("width_diameter"),
                            uom="EA",
                            origin=", ".join(x for x in (loc.get("city"), loc.get("state")) if x),
                            price_range=_price(it.get("price")),
                            image_url=img,
                            source_url=f"https://unbuilt.co/listing/{iid}",
                        ))
                        if limit and len(out.materials) >= limit:
                            break
                    pg = d.get("pagination") or {}
                    if (limit and len(out.materials) >= limit) or page >= (pg.get("totalPages") or 1):
                        break
                    page += 1
                    await asyncio.sleep(delay)
                if limit and len(out.materials) >= limit:
                    break
        out.ok = bool(out.materials)
        if not out.ok:
            out.error = "no stone listings returned"
    except Exception as e:  # noqa: BLE001 - one provider must not kill the crawl
        # Preserve partial results: a failure partway through must not discard the rows
        # already collected. ingest stores them (with the error recorded) rather than
        # throwing the whole supplier away over one bad page.
        out.error = f"{type(e).__name__}: {e}"
        out.ok = out.ok or bool(out.materials)
    return out
