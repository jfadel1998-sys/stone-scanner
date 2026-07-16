"""SlabCloud — public inventory published from SlabSmith/scanner systems.

SlabSmith itself is desktop software with no web surface; SlabCloud is the
service that syncs a supplier's slab data up and publishes it. One central host
serves every tenant, keyed by slug:

    /api/slabs/<slug>              -> one row per material (with a slab count)
    /api/slabs/<slug>?name=<Name>  -> the individual slabs of that material

Two things to know:

  * **Names must be passed back verbatim.** Many carry a leading TAB from the
    supplier's own data; `?name=Infinity Black Honed` returns `[]` while
    `?name=%09Infinity Black Honed` returns the slabs. Never strip before querying.
  * Dimensions are **metres**, unlike every other provider (inches), so they are
    converted on the way in.

Unlike Stone Profits there is no wildcard-DNS pattern to discover tenants from,
so slugs are seeded in suppliers.json. Every tenant shares one origin, so this
provider crawls serially and politely.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .base import SupplierData, material_row, metres_to_inches, slab_row

SITE = "https://slabcloud.com"
UA = "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"


def _slug(entry: dict) -> str:
    """Tenant slug: explicit, else the label from '<slug>.slabcloud.com'."""
    if entry.get("slug"):
        return str(entry["slug"]).strip()
    return (entry.get("host") or "").split(".")[0].strip()


async def crawl(entry: dict, *, with_slabs: bool = False, delay: float = 0.3,
                limit: int = 0, **_kw) -> SupplierData:
    slug = _slug(entry)
    host = entry.get("host") or f"{slug}.slabcloud.com"
    out = SupplierData(host=host, company=entry.get("name") or slug.title())
    crawled_at = _now()
    try:
        async with httpx.AsyncClient(headers={"User-Agent": UA},
                                     follow_redirects=True) as client:
            r = await client.get(f"{SITE}/api/slabs/{slug}", timeout=60)
            r.raise_for_status()
            rows = [m for m in r.json() if isinstance(m, dict)]

            # The list endpoint returns ONE ROW PER SLAB, repeating `count` on each
            # (6 Sahara Blanc slabs = 6 identical rows, count=6). Group by name, or
            # every material is duplicated and its slabs re-fetched once per slab.
            grouped: dict[str, dict] = {}
            for m in rows:
                key = (m.get("Name") or "").strip().upper()
                if key:
                    grouped.setdefault(key, m)
            mats = list(grouped.values())
            if limit:
                mats = mats[:limit]

            for m in mats:
                raw_name = m.get("Name") or ""      # keep the leading tab for queries
                name = raw_name.strip()
                if not name:
                    continue
                count = int(m.get("count") or 0)
                slabs: list[dict] = []
                if with_slabs and count:
                    try:
                        rs = await client.get(f"{SITE}/api/slabs/{slug}",
                                              params={"name": raw_name}, timeout=60)
                        rs.raise_for_status()
                        slabs = [s for s in rs.json() if isinstance(s, dict)]
                    except (httpx.HTTPError, json.JSONDecodeError):
                        slabs = []
                    await asyncio.sleep(delay)

                first = slabs[0] if slabs else {}
                lens = [metres_to_inches(s.get("Length_Actual")) for s in slabs]
                wids = [metres_to_inches(s.get("Width_Actual")) for s in slabs]
                lens = [x for x in lens if x]
                wids = [x for x in wids if x]
                is_remnant = any((s.get("Type") or "") == "Remnant" for s in slabs)

                out.materials.append(material_row(
                    name=name, crawled_at=crawled_at,
                    item_id=str(m.get("InventoryID") or m.get("SlabID") or ""),
                    category=m.get("Material") or "",
                    thickness=first.get("Thickness_Nominal") or name,
                    product_form="REMNANT" if is_remnant else "SLAB",
                    available_slabs=count,
                    avg_length=round(sum(lens) / len(lens), 1) if lens else None,
                    avg_width=round(sum(wids) / len(wids), 1) if wids else None,
                    uom="SF", sku=str(m.get("InventoryID") or ""),
                    image_url=_img(slug, m.get("SlabID")),
                    source_url=f"{SITE}/{slug}",
                ))

                for s in slabs:
                    out.slabs.append(slab_row(
                        item_id=str(m.get("InventoryID") or m.get("SlabID") or ""),
                        crawled_at=crawled_at,
                        slab_no=str(s.get("InventoryID") or ""),
                        # `Rack` is a spot inside one yard ("Rack 13"), not a place.
                        # `location` drives the location filter and the pin map, so
                        # racks are left out rather than dumped there — a SlabCloud
                        # tenant is a single yard whose geography we aren't told.
                        location="",
                        length=metres_to_inches(s.get("Length_Actual")),
                        width=metres_to_inches(s.get("Width_Actual")),
                        qty=1, uom="SF", barcode=str(s.get("Lot") or ""),
                        image_url=_img(slug, s.get("SlabID")),
                    ))
        out.ok = bool(out.materials)
        if not out.ok:
            out.error = "no materials returned"
    except Exception as e:  # noqa: BLE001 - one provider must not kill the crawl
        out.error = f"{type(e).__name__}: {e}"
    return out


def _img(slug: str, slab_id: Any) -> str:
    return f"{SITE}/slabs/{slug}/{slab_id}.jpg" if slab_id else ""


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
