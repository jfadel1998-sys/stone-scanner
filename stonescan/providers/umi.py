"""UMI Stone — public live-inventory API (apps.umistone.com).

UMI's website is WordPress, but its live inventory is served by a separate,
unauthenticated JSON API. No login, no token, no browser: the whole crawl is
plain HTTP, which makes this the cheapest provider we have.

    IMat.php?branch=<b>                          -> materials at a branch
    ILot.php?branch=&item=&GroupCode=&qty=       -> lots for a material
    iser.php?branch=&lot=&qty=&item=             -> individual slabs in a lot

Two things bite here:

1. **Branches share pools.** `branch=connecticut|boston|brooklyn` return
   byte-identical material sets (1,014 each), and a slab fetched via
   `branch=connecticut` reports `"Branch": "Boston"`. Branches are regional
   windows onto one pool, not disjoint inventories — so materials are unioned by
   item code and a slab's own `Branch` field is trusted over the queried branch.
   Summing per-branch counts would triple-count the catalog.
2. **The API lies about its content type** (`text/html` for JSON), emits a UTF-8
   BOM, and double-wraps arrays as `[[{...}]]`.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl

import httpx

from .base import SupplierData, material_row, slab_row

API = "https://apps.umistone.com/linv"
SITE = "https://umistone.com"
UA = "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"

# GroupName -> a category string normalize.canonical_type() understands.
_GROUP_HINT = re.compile(r"(quartzite|granite|marble|quartz|porcelain|dolomite|onyx|"
                         r"travertine|limestone|soapstone|slate|sintered)", re.I)


def _ssl_ctx() -> ssl.SSLContext:
    """apps.umistone.com only offers legacy RSA key-exchange ciphers (no forward
    secrecy), which OpenSSL 3's default security level refuses outright — the
    handshake dies before any request. Certificate verification stays ON; only
    the cipher security level is relaxed, which is the minimum needed to talk to
    their server at all. (curl on Windows succeeds because schannel is laxer.)"""
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx


def _decode(text: str) -> list[dict]:
    """Parse UMI's JSON: BOM-prefixed, served as text/html, and often [[{...}]]."""
    d = json.loads(text.lstrip("﻿") or "[]")
    while isinstance(d, list) and d and isinstance(d[0], list):
        d = d[0]
    return [x for x in d if isinstance(x, dict)]


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
    r = await client.get(f"{API}/{path}", params=params, timeout=45)
    r.raise_for_status()
    return _decode(r.text)


async def _branches(client: httpx.AsyncClient) -> list[str]:
    """Branch slugs, read from the site's own location-inventory links."""
    try:
        r = await client.get(f"{SITE}/", timeout=45)
        found = sorted(set(re.findall(r"/location-inventory/([a-z0-9-]+)/", r.text)))
        if found:
            return found
    except httpx.HTTPError:
        pass
    return ["connecticut"]


def _form_of(group_name: str) -> str:
    g = (group_name or "").lower()
    if "remnant" in g:
        return "REMNANT"
    if "tile" in g:
        return "TILE"
    return "SLAB"


async def crawl(entry: dict, *, with_slabs: bool = False, delay: float = 0.25,
                limit: int = 0, **_kw) -> SupplierData:
    """Crawl UMI. `limit` caps how many materials get lot/slab detail (0 = all);
    the full catalog is ~1k materials and one ILot call each."""
    host = entry.get("host") or "umistone.com"
    out = SupplierData(host=host, company=entry.get("name") or "UMI Stone",
                       image_base="https://apps.umistone.com/images/")
    crawled_at = _now()
    try:
        async with httpx.AsyncClient(headers={"User-Agent": UA}, verify=_ssl_ctx(),
                                     follow_redirects=True) as client:
            branches = await _branches(client)
            # item code -> (material, branch it was first seen at)
            seen: dict[str, tuple[dict, str]] = {}
            for b in branches:
                try:
                    mats = await _get(client, "IMat.php", {"branch": b})
                except (httpx.HTTPError, json.JSONDecodeError) as e:
                    print(f"         (umi: branch {b} failed: {e})")
                    continue
                for m in mats:
                    code = (m.get("item") or "").strip()
                    if code and code not in seen:
                        seen[code] = (m, b)
                await asyncio.sleep(delay)

            items = list(seen.items())
            if limit:
                items = items[:limit]
            for code, (m, branch) in items:
                name = (m.get("MaterialName") or "").strip()
                if not name:
                    continue
                group = (m.get("GroupName") or "").strip()
                hint = _GROUP_HINT.search(group)
                lots = int(m.get("lots") or 0)
                slab_total, lot_rows = 0, []
                if lots:
                    try:
                        lot_rows = await _get(client, "ILot.php", {
                            "branch": branch, "item": code,
                            "GroupCode": m.get("GroupCode") or "", "qty": lots,
                        })
                        slab_total = sum(int(l.get("Qty") or 0) for l in lot_rows)
                        await asyncio.sleep(delay)
                    except (httpx.HTTPError, json.JSONDecodeError):
                        lot_rows = []

                dims: list[tuple[float, float]] = []
                if with_slabs and lot_rows:
                    for lot in lot_rows:
                        try:
                            slabs = await _get(client, "iser.php", {
                                "branch": branch, "lot": lot.get("Lot"),
                                "qty": lot.get("Qty") or 1, "item": code,
                            })
                        except (httpx.HTTPError, json.JSONDecodeError):
                            continue
                        for s in slabs:
                            ln, wd = s.get("length"), s.get("wide")
                            if ln and wd:
                                dims.append((float(ln), float(wd)))
                            out.slabs.append(slab_row(
                                item_id=code, crawled_at=crawled_at,
                                slab_no=str(s.get("Serial") or s.get("Seq") or ""),
                                # The slab's own Branch is authoritative; the queried
                                # branch is only a regional window onto the pool.
                                location=(s.get("Branch") or "").strip(),
                                length=ln, width=wd,
                                qty=1, uom="SF", barcode=str(s.get("Serial") or ""),
                                # Individual slabs usually have no photo of their own
                                # (url is null); fall back to the photo of the lot the
                                # slab belongs to rather than showing nothing.
                                image_url=(s.get("url") or lot.get("url") or "").strip(),
                            ))
                        await asyncio.sleep(delay)

                # UMI publishes no size on a material, so average its slabs' sizes —
                # otherwise the size filter and "biggest slabs" sort can't see UMI.
                avg_l = round(sum(d[0] for d in dims) / len(dims), 1) if dims else None
                avg_w = round(sum(d[1] for d in dims) / len(dims), 1) if dims else None

                out.materials.append(material_row(
                    name=name, crawled_at=crawled_at, item_id=code,
                    category=hint.group(1) if hint else group,
                    subcategory=group, thickness=name,  # thickness rides in the name
                    product_form=_form_of(group),
                    available_slabs=slab_total,
                    avg_length=avg_l, avg_width=avg_w,
                    new_arrival=1 if m.get("newArrival") else 0,
                    image_url=(m.get("url") or "").strip(),
                    source_url=f"{SITE}/live-inventory/",
                ))
        out.ok = bool(out.materials)
        if not out.ok:
            out.error = "no items returned"
    except Exception as e:  # noqa: BLE001 - one provider must not kill the crawl
        out.error = f"{type(e).__name__}: {e}"
    return out


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
