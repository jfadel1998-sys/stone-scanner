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

from .smartsearch import COLOR_WORDS

# Colour words a stone name may carry ("Blue Bahia", "White Ice"), longest-first so
# "light blue" is matched before "blue". Used only as a fallback when the supplier
# left the structured Color field blank (~1/3 of rows).
_NAME_COLOR_RE = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(c) for c in COLOR_WORDS) + r")(?![a-z])",
    re.IGNORECASE,
)

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
    ("valiant", "Quartz"), ("revolux", "Quartz"), ("q-luxe", "Quartz"), ("q luxe", "Quartz"),
    ("q-quartz", "Quartz"), ("qz ", "Quartz"),   # brand/SKU prefixes seen only on quartz
    # LG's quartz line, and the "QTZ" SKU abbreviation — both sat in Other while every
    # competing quartz brand above was already recognized.
    ("viatera", "Quartz"), ("qtz", "Quartz"),
    ("geoscape", "Porcelain"),
    ("corian", "Solid Surface"), ("solid surface", "Solid Surface"),
    ("terrazzo", "Terrazzo"), ("terrazo", "Terrazzo"),
    # natural stones (specific before generic)
    ("quartzite", "Quartzite"),
    ("soap stone", "Soapstone"), ("soapstone", "Soapstone"),
    ("dolomite", "Dolomite"), ("dolomites", "Dolomite"), ("dolamite", "Dolomite"),
    # Calcite and serpentine are distinct commercial categories the suppliers name
    # themselves (category='Calcite', category='serpentine'). Given their own types for
    # the same reason Dolomite has one — marble-adjacent, but sold as its own thing.
    ("calcite", "Calcite"), ("serpentine", "Serpentine"), ("serpentinite", "Serpentine"),
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
    # Ceramic sits LAST on purpose. Suppliers use it as a catch-all category, so letting
    # it match early would demote listings that already classify correctly (a ceramic
    # mosaic is a Mosaic; a "Ceramic"-categorised Dekton is Sintered Stone). Down here it
    # only rescues rows that would otherwise fall through to "Other".
    ("ceramic", "Ceramic"),
    ("natural stone", "Natural Stone"),
]

# Types weak enough that a specific match on the item NAME should outrank them: suppliers
# file branded sintered/porcelain slabs under a generic "Ceramic" category, and the name
# ("DEKTON KERANIUM") is the more reliable signal.
_GENERIC_LABELS = {"Ceramic"}

# Strong signals that a listing is NOT a slab/material (sinks, hardware, care...).
# NB: no "blanco" — it's Spanish for "white" and hides real stones (Silestone Blanco,
# Blanco Atlantico, Super Blanco-Gris); genuine Blanco sinks are caught by silgranit/sink.
_ACCESSORY_RULES = [
    "sink", "faucet", "furniture", "sofa", "lighting", "light fixture", "cleaning",
    "maintenance", "sealer", "adhesive", "epoxy", "cabinet", "wall panel", "hardware",
    "stainless steel", "kohler", "silgranit", "pvc", "spc tile", "lvt",
    "cleaning product", "stone care", "sample kit", "brochure", "display",
    # Multi-word fabrication tools / consumables / care products / PPE that were
    # landing in "Other" — safe as substrings (the phrases don't occur in stone names).
    "core drill", "drill bit", "razor blade", "steel rod", "polishing pad",
    "diamond blade", "mesh diamond", "knife grade", "brush applicator", "angle grinder",
    "sample tower", "soap dish", "tape roll", "face mask", "countertop cleaner",
    "dry treat", "poultice", "stain-proof", "stain proof",
    # Kitchen/fabrication accessories and sealers still landing in "Other". Every phrase
    # here was taken from a real row (subcategory 'Kitchen Accessories', the "511"
    # sealer line, and the Italian tool listings), and each is long enough not to occur
    # in a stone name.
    "kitchen accessor", "cutting board", "colander", "strainer",
    "tool holder", "toll holder", "wine holder", "scratching tool",
    "dremel", "electroplated", "impregnator", "kleen",
]

# Single-word fabrication consumables/tools/PPE, matched on WORD BOUNDARIES so a stone
# whose name merely *contains* the letters isn't swept into the non-slab bucket — e.g.
# "gritstone" (a real sandstone) must not match "grit", and "brushed"/"steel grey" (a
# finish / a granite) must not match "brushes"/"steel". These catch "36 GRIT",
# "1 GALLON", "MAKITA BRUSHES", "CUTTERS #2", etc.
_ACCESSORY_WORDS = {"grit", "velcro", "acetone", "abrasive", "backer", "sanding",
                    "gallon", "makita", "grinder", "cutters", "brushes", "abrax",
                    "workdiamond", "handout", "handouts", "cleaner", "gloves", "respirator"}
_ACCESSORY_WORD_RE = re.compile(r"\b(" + "|".join(_ACCESSORY_WORDS) + r")\b", re.IGNORECASE)

# An explicit "slab" in the name means it IS a slab — never an accessory — UNLESS it's
# slab-handling equipment (a "slab rack"/"slab lifter"/"slab dolly", which is a tool).
_SLAB_RE = re.compile(r"\bslab\b", re.IGNORECASE)
_SLAB_TOOL_RE = re.compile(
    r"slab\s+(lifter|rack|dolly|cart|cradle|a-?frame|trolley|clamp|buggy|trailer|"
    r"lifting|handling|storage|hook|caddy|stand)", re.IGNORECASE)

_THICKNESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*cm", re.IGNORECASE)
# Slab dimensions embedded in a name ("127.5x63.7x1.2", "126.5 x 78.5"): a per-slab
# measurement, never part of the material's identity, so it must not reach the match key.
_DIMS_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)*\b", re.IGNORECASE)
# Finish words to strip from a name when building the cross-supplier match key. The
# trailing lookahead accepts a digit as well as a word boundary, because suppliers glue
# the size on ("Cassablanca Polished126.5 x 78.5") and the old \b left "polished126"
# welded into the key — splitting one material into per-size variants.
_FINISH_RE = re.compile(
    r"""\b(?:un)?(?:
        honed | leather(?:ed)? | polished | brushed | flamed | matte
        | satin | caressed | suede | velvet | antique[d]? | dual
    )(?=\d|\b)""",
    re.IGNORECASE | re.VERBOSE,
)
# Other qualifiers to strip when building the match key.
_QUALIFIER_RE = re.compile(
    r"""\b(
        \d+(?:\.\d+)?\s*cm            # thickness
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

    # Non-slab accessories (sinks, care products, hardware, tools, PPE) -> one bucket.
    # Slab-handling equipment ("slab rack/lifter/dolly") is an accessory outright; but
    # otherwise a name that says "slab" IS a slab, so it skips the accessory keywords —
    # this protects real slabs (e.g. "Silestone Blanco X SLAB") from over-broad matches.
    if _SLAB_TOOL_RE.search(nm):
        return "Accessory / Non-Slab"
    if not _SLAB_RE.search(nm):
        for acc in _ACCESSORY_RULES:
            if acc in hay or acc in nm:
                return "Accessory / Non-Slab"
        if _ACCESSORY_WORD_RE.search(hay) or _ACCESSORY_WORD_RE.search(nm):
            return "Accessory / Non-Slab"

    label = _match_rules(hay)
    name_label = _match_rules(nm)
    # A catch-all category must not outrank a specific brand in the name — otherwise a
    # Dekton slab filed under "Ceramic" stops being Sintered Stone.
    if label in _GENERIC_LABELS and name_label and name_label not in _GENERIC_LABELS:
        label = name_label
    label = label or name_label
    if label:
        return label

    # Anything unrecognized (pattern names, brands, inventory qualifiers) -> Other,
    # so the type facet stays a clean set of real material types.
    return "Other"


def _looks_like_color(part: str) -> bool:
    """A real colour word or two ("Beige", "Off White") — not a pasted description."""
    return bool(part) and len(part) <= 18 and len(part.split()) <= 2 and not re.search(r"\d", part)


def clean_color(raw: Any) -> str:
    """Tidy a color value: drop numeric color-IDs, unify casing, and discard the product
    descriptions some suppliers paste into the Color field.

    Filtered PER COMMA-SEPARATED PART, so a genuine multi-colour list survives whole
    ("Gray, Tan, White, Beige") while a description keeps only its real colour
    ("Beige,Shell Reef Brushed 24 X 48 2 Cm Limestone Tile" -> "Beige"). Same test the
    canonical material page already applies for display, moved upstream so the stored
    value is clean too. A value with no colour-looking part at all yields "" rather than
    a stone name masquerading as a colour ("Amarillo Santa Cecilia").
    """
    c = _text(raw)
    if not c or c.replace(".", "").isdigit():
        return ""
    keep = [p.strip() for p in c.split(",") if _looks_like_color(p.strip())]
    if not keep:
        return ""
    # dict.fromkeys keeps first-seen order while dropping repeats ("White, white").
    return ", ".join(dict.fromkeys(p.title() for p in keep))


def derive_color_from_name(name: str) -> str:
    """Best-effort colour read from the item name, for rows with no Color field.
    'Blue Bahia' -> 'Blue', 'Off White Marble' -> 'Off White'. Empty if none found."""
    m = _NAME_COLOR_RE.search(name or "")
    return m.group(1).title() if m else ""


def drop_if_numeric(raw: Any) -> str:
    """Return the text unless it's just a numeric ID (e.g. Finish '0', Origin '1054')."""
    c = _text(raw)
    return "" if (not c or c.replace(".", "").isdigit()) else c


# Thickness units → centimetres (the catalog's canonical display unit). Anything not
# a length (SF/EA/…) is a *selling* unit, not a thickness unit, and is ignored below.
_THK_UNIT_CM = {"mm": 0.1, "cm": 1.0, "in": 2.54, "inch": 2.54, '"': 2.54}
_THICKNESS_VAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(mm|cm|in|inch|\")?\s*$", re.IGNORECASE)


def thickness_unit(uom: str) -> str:
    """Read a length unit out of a UOM field, else '' (SF/EA aren't lengths)."""
    u = (uom or "").strip().lower().rstrip(".")
    if u in ('"', "inch"):
        return "in"
    return u if u in _THK_UNIT_CM else ""


def normalize_thickness(raw_thickness: str | None, name: str, uom: str = "") -> str:
    """Return thickness like '3cm', converting from the item's thickness unit.

    Prefer the structured field, falling back to reading '<n>cm' from the name. The
    unit comes from the value itself ('30mm') or the item's thickness UOM; without one
    we assume cm. This is what keeps a 30 mm sintered slab from being stored — as it
    was — as an absurd '30cm' (the old code blindly appended 'cm' to the bare number).
    """
    if raw_thickness is not None and str(raw_thickness).strip() != "":
        m = _THICKNESS_VAL_RE.match(str(raw_thickness))
        if m:
            num = float(m.group(1))
            unit = (m.group(2) or "").lower() or thickness_unit(uom) or "cm"
            if unit == "inch":
                unit = "in"
            cm = num * _THK_UNIT_CM.get(unit, 1.0)
            return f"{round(cm, 2):g}cm"
    m = _THICKNESS_RE.search(name or "")
    if m:
        return f"{float(m.group(1)):g}cm"
    return ""


def clean_name(name: str) -> str:
    """Collapse whitespace and tidy casing for display."""
    n = re.sub(r"\s+", " ", (name or "").strip())
    return n


def _title_key(name: str) -> str:
    """Base name used for grouping: drop finish/thickness/size qualifiers + punctuation."""
    n = (name or "").lower()
    n = re.sub(r"\(([^)]*)\)", " ", n)          # drop parenthetical qualifiers
    n = _FINISH_RE.sub(" ", n)                  # drop finishes (incl. "polished126")
    n = _QUALIFIER_RE.sub(" ", n)               # drop loose qualifiers
    n = _DIMS_RE.sub(" ", n)                    # drop per-slab dimensions
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
    # The thickness unit is whichever of ThicknessUOM / UOM is actually a length —
    # the general UOM is often a *selling* unit (SF/EA) that must not be read as cm/mm.
    thk_uom = next((u for u in (_text(item.get("ThicknessUOM")), _text(item.get("UOM")))
                    if thickness_unit(u)), "")
    thickness = normalize_thickness(
        _pick(item, "ProductThickness", "Thickness"), name, thk_uom)

    def num(v):
        try:
            return float(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    iid = _text(item.get("ItemID"))
    return {
        "item_id": iid,
        "item_name": name,
        "name_norm": name.upper(),
        "material_key": material_key(name, mtype),
        "material_type": mtype,
        "category": category,
        "subcategory": subcategory,
        "color": clean_color(item.get("Color")) or derive_color_from_name(name),
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
        # Deep-link straight to the product: the SPS storefront routes
        # /InventoryDetail/<ItemID> to the item (falls back to the catalog root only
        # when we somehow have no item id).
        "source_url": (f"https://{supplier_host}/InventoryDetail/{iid}"
                       if iid else f"https://{supplier_host}/"),
        "crawled_at": crawled_at,
    }
