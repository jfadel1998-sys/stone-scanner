"""SlabWare — public tenant catalogs at <tenant>.slabware.com.

Structurally the closest thing to Stone Profits, and easier: the ASP.NET
script-service endpoints are plain JSON POSTs with no token to harvest, so this
needs no browser at all.

    POST /FullInventory.aspx/ObterListaBundles   {"inicio": <offset>, "json": ""}
    POST /FullInventory.aspx/DetalheBundleNovo   {"IdBundle": n, "Remnant": false,
                                                  "IdCampanha": 0}

Gotchas:
  * Cloudflare 403s a non-browser User-Agent — a browser UA is required just to
    get a response (we still identify ourselves in a comment header below).
  * `d.Bundles` is a JSON **string nested inside** JSON, so it decodes twice.
  * `DetalheBundleNovo` needs the FULL param set or it errors out — same class of
    trap as Stone Profits' getItemGallery.
  * Fields are Portuguese: chapas=slabs, cor=colour, acabamento=finish,
    espessura=thickness, cavalete=rack, localizacao=location.
  * Prices are gated per tenant (`Permissoes.MostrarPrecoDeslogado`); when hidden
    they come back as the literal "CALL", which must not be stored as a price.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from .base import SupplierData, material_row, slab_row

PAGE = 40  # the site's own infinite scroll pages by 40
# Cloudflare rejects unknown agents outright; this is the minimum needed to be
# served at all. Crawls stay slow, single-threaded and well under a human's rate.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
      "Gecko) Chrome/120.0.0.0 Safari/537.36")
_SIZE_RE = re.compile(r"([\d.]+)\s*''?\s*x\s*([\d.]+)")


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "", "CALL") else None
    except (TypeError, ValueError):
        return None


def _price(v: Any) -> str:
    """'CALL' / 'Login for Price' mean the tenant hides pricing — not a value."""
    s = str(v or "").strip()
    if not s or s.upper() == "CALL" or "login" in s.lower():
        return ""
    return s


def _composition(v: str) -> str:
    """'Granite / ' -> 'Granite'. SlabWare pre-classifies material type for us."""
    return (v or "").split("/")[0].strip()


async def _post(client: httpx.AsyncClient, host: str, method: str, payload: dict) -> Any:
    r = await client.post(
        f"https://{host}/FullInventory.aspx/{method}",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=45,
    )
    r.raise_for_status()
    return r.json().get("d")


async def crawl(entry: dict, *, with_slabs: bool = False, delay: float = 0.4,
                limit: int = 0, **_kw) -> SupplierData:
    host = entry["host"]
    out = SupplierData(host=host, company=entry.get("name") or "")
    crawled_at = _now()
    try:
        async with httpx.AsyncClient(headers={"User-Agent": UA},
                                     follow_redirects=True) as client:
            bundles: list[dict] = []
            offset = 0
            while True:
                d = await _post(client, host, "ObterListaBundles",
                                {"inicio": offset, "json": ""})
                raw = (d or {}).get("Bundles") or "[]"
                page = json.loads(raw) if isinstance(raw, str) else raw
                bundles.extend(page)
                # Mirror the site's own "keep going" rule (it pages while > 38).
                if len(page) < PAGE - 1 or (limit and len(bundles) >= limit):
                    break
                offset += PAGE
                await asyncio.sleep(delay)
            if limit:
                bundles = bundles[:limit]

            for b in bundles:
                bid = b.get("id")
                name = (b.get("nomeMaterial") or "").strip()
                if not name:
                    continue
                size = _SIZE_RE.search(b.get("averageSize") or "")
                detail: dict = {}
                chapas: list[dict] = []
                if with_slabs and bid is not None:
                    try:
                        dd = await _post(client, host, "DetalheBundleNovo",
                                         {"IdBundle": bid, "Remnant": False,
                                          "IdCampanha": 0})
                        detail = (dd or {}).get("Bundle") or {}
                        if isinstance(detail, str):
                            detail = json.loads(detail)
                        if isinstance(detail, list):
                            detail = detail[0] if detail else {}
                        chapas = detail.get("chapas") or []
                        if isinstance(chapas, str):
                            chapas = json.loads(chapas)
                        await asyncio.sleep(delay)
                    except (httpx.HTTPError, json.JSONDecodeError, KeyError):
                        detail, chapas = {}, []

                photo = (b.get("fotoPrincipal") or "").strip()
                out.materials.append(material_row(
                    name=name, crawled_at=crawled_at, item_id=str(bid),
                    category=_composition(b.get("nomeComposicao")),
                    color=detail.get("cor") or "",
                    finish=b.get("acabamento") or "",
                    thickness=(b.get("nomeEspessura") or "").replace(" ", ""),
                    product_form="REMNANT" if detail.get("Remnant") else "SLAB",
                    origin=detail.get("origem") or "",
                    available_slabs=_num(b.get("qtdChapas")) or 0,
                    avg_length=_num(size.group(1)) if size else None,
                    avg_width=_num(size.group(2)) if size else None,
                    uom="SF", sku=(b.get("bloco") or "").strip(),
                    price_range=_price(b.get("preco1")),
                    image_url=_img(host, bid, photo) if photo else "",
                    source_url=f"https://{host}/FullInventory.aspx",
                ))

                for c in chapas:
                    out.slabs.append(slab_row(
                        item_id=str(bid), crawled_at=crawled_at,
                        slab_no=str(c.get("Numero") or c.get("Id") or ""),
                        location=(c.get("Localizacao") or b.get("cavalete") or "").strip(),
                        length=_num(c.get("Length")), width=_num(c.get("Height")),
                        qty=1, uom="SF", barcode=str(c.get("Id") or ""),
                        image_url=_img(host, bid, (c.get("Foto") or "").strip()),
                    ))
        out.ok = bool(out.materials)
        if not out.ok:
            out.error = "no bundles returned"
    except Exception as e:  # noqa: BLE001 - one provider must not kill the crawl
        out.error = f"{type(e).__name__}: {e}"
    return out


def _img(host: str, bundle_id: Any, filename: str) -> str:
    """Bundle and slab photos both live under the bundle's own folder."""
    if not filename:
        return ""
    return (f"https://{host}/backendGranite/cadastros/bundles/fotos/"
            f"{bundle_id}/{filename}")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
