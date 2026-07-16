"""Turn a raw Stone Profits inventory item into a normalized material row.

The catalog API already gives us good structured fields (CategoryName,
SubCategoryName, Color, ProductThickness, Finish, ProductFormValue). This module:

  * derives a canonical `material_type` (Granite, Marble, Quartz, ...)
  * normalizes thickness to "<n>cm"
  * builds a clean display name
  * builds a `material_key` so the SAME material from different suppliers
    collapses together (e.g. "ABSOLUTE BLACK" from 5 suppliers)
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

# Ordered classifier rules. First matching needle (substring) wins, so the list
# runs MOST-SPECIFIC first (e.g. "quartzite" before "quartz", brand/engineered
# lines before the generic stone they resemble). Applied to a haystack built from
# subcategory + category, and — as a fallback — the item name.
_TYPE_RULES: list[tuple[str, str]] = [
    # engineered / brand lines first (they often also contain a generic word)
    ("engineered marble", "Engineered Marble"),
    ("engineered glass", "Engineered Glass"),
    ("engineered stone", "Engineered Stone"),
    ("sintered", "Sintered Stone"), ("neolith", "Sintered Stone"), ("dekton", "Sintered Stone"),
    ("lapitec", "Sintered Stone"), ("laminam", "Sintered Stone"), ("ascale", "Sintered Stone"),
    ("maximvs", "Sintered Stone"), ("florim", "Sintered Stone"), ("infinity", "Sintered Stone"),
    ("porcelain", "Porcelain"),
    ("vetrazzo", "Engineered Glass"), ("vetrite", "Engineered Glass"), ("nano glass", "Engineered Glass"),
    ("caesarstone", "Quartz"), ("silestone", "Quartz"), ("compac", "Quartz"),
    ("santa margherita", "Quartz"), ("vicostone", "Quartz"), ("cambria", "Quartz"),
    ("hanstone", "Quartz"), ("silica free", "Quartz"), ("diresco", "Quartz"),
    ("corian", "Solid Surface"), ("solid surface", "Solid Surface"),
    ("terrazzo", "Terrazzo"), ("terrazo", "Terrazzo"),
    # natural stones (specific before generic)
    ("quartzite", "Quartzite"),
    ("soap stone", "Soapstone"), ("soapstone", "Soapstone"),
    ("dolomite", "Dolomite"), ("dolomites", "Dolomite"), ("dolamite", "Dolomite"),
    ("travertine", "Travertine"), ("travertino", "Travertine"), ("trevertine", "Travertine"),
    ("limestone", "Limestone"), ("sandstone", "Sandstone"),
    ("onyx", "Onyx"), ("slate", "Slate"), ("basalt", "Basalt"), ("schist", "Schist"),
    ("labradorite", "Semi-Precious"), ("sodalite", "Semi-Precious"), ("agate", "Semi-Precious"),
    ("gemstone", "Semi-Precious"), ("crystal", "Semi-Precious"), ("quartz crystal", "Semi-Precious"),
    ("semi precious", "Semi-Precious"), ("semi-precious", "Semi-Precious"), ("precious", "Semi-Precious"),
    ("glass", "Engineered Glass"),
    ("marble", "Marble"),
    ("granite", "Granite"),
    ("quartz", "Quartz"),
    ("mosaic", "Mosaic"), ("veneer", "Stone Veneer"), ("tile", "Tile"),
    ("natural stone", "Natural Stone"),
]

# Strong signals that a listing is NOT a slab/material (sinks, hardware, care...).
_ACCESSORY_RULES = [
    "sink", "faucet", "furniture", "sofa", "lighting", "light fixture", "cleaning",
    "maintenance", "sealer", "adhesive", "epoxy", "cabinet", "wall panel", "hardware",
    "stainless steel", "blanco", "kohler", "silgranit", "pvc", "spc tile", "lvt",
    "cleaning product", "stone care", "sample kit", "brochure", "display",
]

_THICKNESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*cm", re.IGNORECASE)
# Qualifiers to strip from a name when building the cross-supplier match key.
_QUALIFIER_RE = re.compile(
    r"""\b(
        \d+(?:\.\d+)?\s*cm            # thickness
        | dual | honed | leather(?:ed)? | polished | brushed | flamed | matte
        | satin | caressed | suede | velvet | antique[d]?
        | \(?\s*l\s*(?:&|x|and)\s*r\s*\)?   # (L & R) / (L x R) block-match markers
        | sj                          # common supplier abbreviation
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)


def _match_rules(haystack: str) -> str:
    for needle, label in _TYPE_RULES:
        if needle in haystack:
            return label
    return ""


def canonical_type(category: str, subcategory: str, name: str = "") -> str:
    """Classify an item into a canonical material type.

    Priority: subcategory/category text -> item name -> Accessory/Other.
    Numeric-only category fields (unresolved IDs) are ignored, so we fall back
    to reading the type out of the item name (e.g. "... Quartzite 3cm").
    """
    sub = (subcategory or "").strip().lower()
    cat = (category or "").strip().lower()
    nm = (name or "").strip().lower()

    # Ignore unresolved numeric IDs that some tenants send as the category name.
    parts = [p for p in (sub, cat) if p and not p.replace(".", "").isdigit()]
    hay = " ".join(parts)

    # Non-slab accessories (sinks, care products, hardware) -> a single bucket.
    for acc in _ACCESSORY_RULES:
        if acc in hay or acc in nm:
            return "Accessory / Non-Slab"

    label = _match_rules(hay) or _match_rules(nm)
    if label:
        return label

    # Anything unrecognized (pattern names, brands, inventory qualifiers) -> Other,
    # so the type facet stays a clean set of real material types.
    return "Other"


def clean_color(raw: Any) -> str:
    """Tidy a color value: drop numeric color-IDs, unify casing."""
    c = _text(raw)
    if not c or c.replace(".", "").isdigit():
        return ""
    return c.title()


def drop_if_numeric(raw: Any) -> str:
    """Return the text unless it's just a numeric ID (e.g. Finish '0', Origin '1054')."""
    c = _text(raw)
    return "" if (not c or c.replace(".", "").isdigit()) else c


def normalize_thickness(raw_thickness: str | None, name: str) -> str:
    """Return thickness like '3cm'. Prefer the structured field, fall back to name."""
    if raw_thickness:
        t = str(raw_thickness).strip().lower().replace("cm", "").strip()
        if t:
            try:
                num = float(t)
                return f"{num:g}cm"
            except ValueError:
                pass
    m = _THICKNESS_RE.search(name or "")
    if m:
        return f"{float(m.group(1)):g}cm"
    return ""


def clean_name(name: str) -> str:
    """Collapse whitespace and tidy casing for display."""
    n = re.sub(r"\s+", " ", (name or "").strip())
    return n


def _title_key(name: str) -> str:
    """Base name used for grouping: drop finish/thickness qualifiers + punctuation."""
    n = (name or "").lower()
    n = re.sub(r"\(([^)]*)\)", " ", n)          # drop parenthetical qualifiers
    n = _QUALIFIER_RE.sub(" ", n)               # drop loose qualifiers
    n = re.sub(r"[^a-z0-9 ]+", " ", n)          # drop punctuation
    n = re.sub(r"\s+", " ", n).strip()
    return n


def material_key(name: str, material_type: str) -> str:
    """Canonical key so the same material across suppliers groups together.

    Uses the qualifier-stripped base name plus the material type, e.g.
    "absolute black|granite". Type guards against unrelated same-name items.
    """
    base = _title_key(name)
    if not base:
        return ""
    return f"{base}|{(material_type or '').lower()}"


def _text(v: Any) -> str:
    """Coerce any API value to a trimmed string (some fields arrive as ints/None)."""
    if v is None:
        return ""
    return str(v).strip()


def _pick(item: dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among the given keys (endpoint-agnostic)."""
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return None


def build_image_url(image_base: str, filename: str) -> str:
    """Live thumbnail URL on the supplier's S3 bucket (not downloaded/cached)."""
    fn = _text(filename)
    if not image_base or not fn:
        return ""
    # Item images are the only ones served from the gallery bucket.
    if not fn.lower().startswith("item_img"):
        return ""
    return f"{image_base}{quote(fn)}"


def normalize_item(
    item: dict[str, Any], supplier_host: str, crawled_at: str, image_base: str = ""
) -> dict[str, Any]:
    """Map one raw API item dict to a materials-table row dict.

    Handles both the item-level (`getItemGallery`) and granular
    (`getInventoryGallery`) field names so either endpoint can feed it.
    """
    name = clean_name(_text(item.get("ItemName")))
    category = _text(item.get("CategoryName"))
    subcategory = _text(_pick(item, "SubCategoryName", "SubCategory"))
    mtype = canonical_type(category, subcategory, name)
    thickness = normalize_thickness(_pick(item, "ProductThickness", "Thickness"), name)

    def num(v):
        try:
            return float(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    return {
        "item_id": _text(item.get("ItemID")),
        "item_name": name,
        "name_norm": name.upper(),
        "material_key": material_key(name, mtype),
        "material_type": mtype,
        "category": category,
        "subcategory": subcategory,
        "color": clean_color(item.get("Color")),
        "finish": drop_if_numeric(item.get("Finish")),
        "thickness": thickness,
        "product_form": _text(_pick(item, "ProductFormValue", "type")),
        "origin": drop_if_numeric(_pick(item, "Origin", "OriginName")),
        "available_qty": num(_pick(item, "AvgCurrentAvailableQty", "AvailableQty")),
        "available_slabs": int(num(item.get("AvailableSlabs")) or 0),
        "avg_length": num(_pick(item, "AvgCurrentSlabLength", "AverageLength")),
        "avg_width": num(_pick(item, "AvgCurrentSlabWidth", "AverageWidth")),
        "uom": _text(_pick(item, "UOM", "ThicknessUOM")),
        "sku": _text(item.get("SKU")),
        "idone": _text(item.get("IDONE")),
        "price_range": _text(item.get("PriceRange")),
        "new_arrival": 1 if _text(item.get("NewArrival")) else 0,
        "image_filename": _text(item.get("Filename")),
        "image_url": build_image_url(image_base, item.get("Filename")),
        "source_url": f"https://{supplier_host}/",
        "crawled_at": crawled_at,
    }
