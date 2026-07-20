"""StoneTrash — a public multi-seller stone marketplace.

Unlike the other providers this is not one supplier's catalog: it is a
marketplace where many sellers list material, so every listing is stored under
the single `stonetrash.com` supplier with the seller's name kept on the row and
the seller's city used as the slab location.

It is a Next.js site, so the data behind each listing page is available as JSON:

    /server-sitemap.xml                       -> every /listings/<id>
    /_next/data/<buildId>/listings/<id>.json  -> the listing record

The `buildId` changes on every deploy, so it is read from `__NEXT_DATA__` at
crawl time rather than stored. The /slabs search page renders client-side and
has no data in it — the sitemap is the only complete list.

StoneTrash is the one source that publishes real coordinates and price/sqft.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from ..robots import client_for
from .base import SupplierData, material_row, slab_row

SITE = "https://stonetrash.com"
UA = "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"
_BUILD_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')
_LISTING_RE = re.compile(r"/listings/([0-9a-zA-Z_-]+)")


async def _build_id(client: httpx.AsyncClient) -> str:
    r = await client.get(f"{SITE}/", timeout=45)
    r.raise_for_status()
    m = _BUILD_RE.search(r.text)
    if not m:
        raise RuntimeError("could not read Next.js buildId from the homepage")
    return m.group(1)


async def _listing_ids(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(f"{SITE}/server-sitemap.xml", timeout=60)
    r.raise_for_status()
    seen, out = set(), []
    for lid in _LISTING_RE.findall(r.text):
        if lid not in seen:
            seen.add(lid)
            out.append(lid)
    return out


def _thickness_cm(sixteenths: Any) -> str:
    """Thickness arrives in sixteenths of an inch: 6/16" = 0.95cm ~ 1cm."""
    try:
        n = float(sixteenths or 0)
    except (TypeError, ValueError):
        return ""
    if not n:
        return ""
    cm = n / 16 * 2.54
    return f"{round(cm * 2) / 2:g}cm"  # snap to the nearest 0.5cm the trade uses


async def crawl(entry: dict, *, with_slabs: bool = False, delay: float = 0.25,
                limit: int = 0, **_kw) -> SupplierData:
    host = entry.get("host") or "stonetrash.com"
    out = SupplierData(host=host, company=entry.get("name") or "StoneTrash (marketplace)")
    crawled_at = _now()
    try:
        async with client_for(entry, headers={"User-Agent": UA},
                              follow_redirects=True) as client:
            build = await _build_id(client)
            ids = await _listing_ids(client)
            if limit:
                ids = ids[:limit]
            for lid in ids:
                try:
                    r = await client.get(
                        f"{SITE}/_next/data/{build}/listings/{lid}.json", timeout=45)
                    if r.status_code == 404:
                        continue
                    r.raise_for_status()
                    listing = (r.json().get("pageProps") or {}).get("listing") or {}
                except (httpx.HTTPError, json.JSONDecodeError):
                    continue
                finally:
                    await asyncio.sleep(delay)
                if not listing or not (listing.get("title") or "").strip():
                    continue

                cat = listing.get("category") or {}
                loc = listing.get("location") or {}
                seller = (listing.get("seller") or {}).get("friendlyName") or ""
                imgs = listing.get("images") or []
                qty = int(listing.get("qtyUnitsAvailable") or 0)
                # A marketplace lists sold/pending stock too; only live listings count.
                live = (listing.get("status") or "") == "ListedForSale"
                city = (loc.get("formattedAddress_Approximate") or "").split(",")[0].strip()
                price = listing.get("pricePerSqFt_USD")

                # The stone type comes from materials_Tags / category.segment — NOT
                # from `taxonomy`, which can be just "Tile" (a form, not a material)
                # and would classify a marble tile as type "Tile". Sellers sometimes
                # leave both blank, in which case the name is the only clue and the
                # classifier falls back to it.
                mats = listing.get("materials_Tags") or []
                out.materials.append(material_row(
                    name=listing["title"], crawled_at=crawled_at, item_id=str(lid),
                    category=(mats[0] if mats else cat.get("segment") or ""),
                    subcategory=cat.get("segment") or "",
                    color=", ".join(listing.get("colors_Tags") or []),
                    finish=listing.get("finish") or "",
                    thickness=_thickness_cm(listing.get("dimensionThickness_SixteenthInches")),
                    product_form=(listing.get("format_Tag") or "SLAB"),
                    available_slabs=qty if live else 0,
                    avg_length=listing.get("dimensionLongest_Inches"),
                    avg_width=listing.get("dimensionShortest_Inches"),
                    uom="SF", sku=str(lid),
                    # Seller identity matters on a marketplace: it is who you'd buy from.
                    idone=seller,
                    price_range=f"${price:g}/sf" if price else "",
                    image_url=(imgs[0].get("url") if imgs else "") or "",
                    source_url=f"{SITE}/listings/{lid}",
                ))

                if live and qty:
                    out.slabs.append(slab_row(
                        item_id=str(lid), crawled_at=crawled_at, slab_no=str(lid),
                        location=city or (loc.get("state") or ""),
                        length=listing.get("dimensionLongest_Inches"),
                        width=listing.get("dimensionShortest_Inches"),
                        qty=qty, uom="SF", barcode="",
                        image_url=(imgs[0].get("url") if imgs else "") or "",
                    ))
        out.ok = bool(out.materials)
        if not out.ok:
            out.error = "no listings returned"
    except Exception as e:  # noqa: BLE001 - one provider must not kill the crawl
        out.error = f"{type(e).__name__}: {e}"
    return out


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
