"""Shared contract for catalog providers.

Stone Scanner started as a Stone Profits-only crawler. Other platforms publish
public slab catalogs too, so a provider is simply "something that turns one
supplier entry into normalized rows". Each provider hands back a `SupplierData`
whose material rows already match the materials-table schema.

Providers MUST build rows via `material_row()` rather than assembling dicts by
hand: it routes name/type through `normalize.py`, so `material_key` is derived
the same way for every platform. That key is what makes one search show the same
stone across suppliers — if a provider derived it differently, its materials
would silently never group with anyone else's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..normalize import (
    canonical_type,
    clean_color,
    clean_name,
    derive_finish,
    material_key,
    normalize_thickness,
)


@dataclass
class SupplierData:
    """One supplier's crawl output, in storage-ready form (minus supplier_id)."""

    host: str
    ok: bool = False
    company: str = ""
    products: str = ""
    phone: str = ""
    email: str = ""
    image_base: str = ""
    materials: list[dict[str, Any]] = field(default_factory=list)
    slabs: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def _num(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def material_row(
    *,
    name: str,
    crawled_at: str,
    item_id: str = "",
    category: str = "",
    subcategory: str = "",
    color: str = "",
    finish: str = "",
    thickness: str = "",
    product_form: str = "",
    origin: str = "",
    available_qty: Any = None,
    available_slabs: Any = None,
    avg_length: Any = None,
    avg_width: Any = None,
    uom: str = "",
    # The unit `thickness` is expressed in, when it is NOT the unit `uom` names. Separate on
    # purpose: `uom` is a SELLING unit as often as a measuring one (SF, EA), it gates the
    # tile_sf column downstream, and rewriting it to "in" to fix a thickness would change
    # what the search UI shows. Unbuilt is the case in hand — it sends inch thicknesses
    # alongside uom="EA", which thickness_unit() correctly refuses to read as a length, so
    # every Cosentino row fell back to centimetres and Dekton's 20mm slabs were stored as
    # 0.79cm. A dedicated parameter says the one thing that is true without disturbing the
    # thing that isn't.
    thickness_uom: str = "",
    sku: str = "",
    idone: str = "",
    price_range: str = "",
    new_arrival: int = 0,
    image_url: str = "",
    source_url: str = "",
) -> dict[str, Any]:
    """Build a materials-table row, classifying type/key exactly like the
    Stone Profits path does."""
    nm = clean_name(name)
    mtype = canonical_type(category, subcategory, nm)
    thk = normalize_thickness(thickness, nm, thickness_uom or uom)
    return {
        "item_id": str(item_id or ""),
        "item_name": nm,
        "name_norm": nm.upper(),
        "material_key": material_key(nm, mtype),
        "material_type": mtype,
        "category": category or "",
        "subcategory": subcategory or "",
        "color": clean_color(color),
        # Derived here as well as in reclassify: a crawl replaces the supplier's rows
        # wholesale, so a reclassify-only value would survive less than a day.
        "finish": derive_finish(nm, finish or ""),
        "thickness": thk,
        "product_form": (product_form or "").strip().upper(),
        "origin": (origin or "").strip(),
        "available_qty": _num(available_qty),
        "available_slabs": int(available_slabs or 0),
        "avg_length": _num(avg_length),
        "avg_width": _num(avg_width),
        "uom": (uom or "").strip(),
        "sku": (sku or "").strip(),
        "idone": (idone or "").strip(),
        "price_range": (price_range or "").strip(),
        "new_arrival": 1 if new_arrival else 0,
        "image_filename": "",
        "image_url": image_url or "",
        "source_url": source_url or "",
        "crawled_at": crawled_at,
    }


def slab_row(
    *,
    item_id: str,
    crawled_at: str,
    slab_no: str = "",
    location: str = "",
    length: Any = None,
    width: Any = None,
    qty: Any = None,
    uom: str = "",
    barcode: str = "",
    image_url: str = "",
) -> dict[str, Any]:
    return {
        "item_id": str(item_id or ""),
        "slab_no": str(slab_no or ""),
        "location": (location or "").strip(),
        "length": _num(length),
        "width": _num(width),
        "qty": _num(qty),
        "uom": (uom or "").strip(),
        "barcode": str(barcode or ""),
        "image_filename": "",
        "image_url": image_url or "",
        "crawled_at": crawled_at,
    }


M_PER_INCH = 0.0254
CM_PER_INCH = 2.54


def metres_to_inches(v: Any) -> float | None:
    n = _num(v)
    return round(n / M_PER_INCH, 1) if n else None


def centimetres_to_inches(v: Any) -> float | None:
    """Convert a length the source states in centimetres.

    Deliberately separate from `normalize.dimension_to_inches`, which guesses the unit
    from magnitude for two Stone Profits hosts that store dimensions unitless. Here the
    publisher tells us the unit, so there is nothing to infer — and inference would be
    actively wrong for blocks, whose 50-150cm heights fall inside that helper's
    "must be metres" band.
    """
    n = _num(v)
    return round(n / CM_PER_INCH, 1) if n else None
