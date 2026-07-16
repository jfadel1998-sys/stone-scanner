"""Turn a natural-language query ("blue marble slabs", "black granite tiles",
"3cm polished calacatta") into structured search facets + leftover name terms.

No AI: it matches a vocabulary of material types, colors, forms, finishes and a
thickness pattern, removing each match from the query; whatever is left becomes
name keywords (which also catches material names like "calacatta" or "taj mahal"
and finishes like "polished", which live in the item name).
"""

from __future__ import annotations

import re

# Ordered most-specific first so e.g. "quartzite" wins before "quartz",
# and multi-word types match before their parts.
CANONICAL_TYPES: list[tuple[str, str]] = [
    ("soap stone", "Soapstone"), ("sintered stone", "Sintered Stone"),
    ("semi precious", "Semi-Precious"), ("semi-precious", "Semi-Precious"),
    ("solid surface", "Solid Surface"), ("engineered marble", "Engineered Marble"),
    ("engineered glass", "Engineered Glass"), ("engineered stone", "Engineered Stone"),
    ("natural stone", "Natural Stone"), ("stone veneer", "Stone Veneer"),
    ("quartzite", "Quartzite"), ("granite", "Granite"), ("marble", "Marble"),
    ("quartz", "Quartz"), ("porcelain", "Porcelain"), ("dolomite", "Dolomite"),
    ("travertine", "Travertine"), ("onyx", "Onyx"), ("limestone", "Limestone"),
    ("soapstone", "Soapstone"), ("slate", "Slate"), ("basalt", "Basalt"),
    ("sandstone", "Sandstone"), ("schist", "Schist"), ("terrazzo", "Terrazzo"),
    ("mosaic", "Mosaic"), ("sintered", "Sintered Stone"), ("neolith", "Sintered Stone"),
    ("dekton", "Sintered Stone"), ("lapitec", "Sintered Stone"),
    ("caesarstone", "Quartz"), ("silestone", "Quartz"),
    ("engineered", "Engineered Stone"), ("veneer", "Stone Veneer"),
    ("precious", "Semi-Precious"), ("porcelin", "Porcelain"),
]

# Multi-word colors first so "light blue" is taken before "blue".
COLOR_WORDS: list[str] = [
    "off white", "off-white", "light blue", "dark blue", "royal blue", "navy blue",
    "sea green", "light green", "dark green", "light gray", "dark gray",
    "light grey", "dark grey", "rose gold",
    "white", "black", "beige", "gray", "grey", "blue", "brown", "cream", "green",
    "gold", "ivory", "red", "pink", "silver", "yellow", "taupe", "purple", "aqua",
    "terracotta", "burgundy", "bronze", "copper", "charcoal", "tan", "navy",
    "maroon", "emerald", "turquoise", "teal", "orange", "honey", "mocha", "walnut",
    "cognac", "champagne",
]

FORM_WORDS: dict[str, str] = {
    "slab": "slab", "slabs": "slab",
    "tile": "tile", "tiles": "tile",
    "remnant": "remnant", "remnants": "remnant", "remmnant": "remnant",
}

FINISH_WORDS: list[str] = [
    "polished", "honed", "leathered", "leather", "brushed", "matte", "matt",
    "satin", "flamed", "caressed", "suede", "velvet", "antiqued", "antique",
    "tumbled", "sandblasted", "bushhammered", "glossy", "textured",
]

_STOP = {
    "the", "a", "an", "of", "in", "on", "with", "and", "or", "for", "me", "show",
    "find", "get", "want", "need", "looking", "look", "please", "material",
    "materials", "stone", "stones", "color", "colour", "colored", "coloured",
    "some", "any", "type", "types", "all", "that", "are", "is", "to",
}

_THICK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(cm|mm)\b")

# Common misspellings and trade shorthand -> the spelling suppliers actually use.
# Applied to the raw query before parsing, so "calcutta gold" finds Calacatta Gold.
ALIASES: dict[str, str] = {
    "calcutta": "calacatta", "calacata": "calacatta", "calactta": "calacatta",
    "calacutta": "calacatta", "calcatta": "calacatta", "calacatt": "calacatta",
    "carrera": "carrara", "carara": "carrara",
    "cararra": "carrara", "carrarra": "carrara",
    "statuary": "statuario", "statuarrio": "statuario", "stautario": "statuario",
    "taj": "taj mahal", "tajmahal": "taj mahal",
    "cambrian": "cambria", "silstone": "silestone", "ceasarstone": "caesarstone",
    "quartzsite": "quartzite", "quartsite": "quartzite",
    "travertino": "travertine", "travertin": "travertine",
    "blue de savoie": "bleu de savoie", "cremarfil": "crema marfil",
    "marquinia": "marquina", "marqunia": "marquina",
    "thasos": "thassos", "thassoss": "thassos", "imperador": "emperador",
    "bottocino": "botticino", "botocino": "botticino",
}


def apply_aliases(q: str) -> str:
    """Rewrite known misspellings/shorthand in a query to supplier spelling."""
    text = q
    for wrong, right in ALIASES.items():
        text = re.sub(r"(?<![a-z])" + re.escape(wrong) + r"(?![a-z])",
                      right, text, flags=re.I)
    return text


def _take(text: str, phrase: str) -> tuple[str, bool]:
    """Remove `phrase` (optionally plural) from text on word boundaries."""
    pat = re.compile(r"(?<![a-z])" + re.escape(phrase) + r"s?(?![a-z])")
    m = pat.search(text)
    if m:
        return text[: m.start()] + " " + text[m.end():], True
    return text, False


def parse_query(q: str) -> dict:
    """Return {material_type, color, thickness, form, terms:[...]} for a query."""
    out: dict = {"material_type": None, "color": None, "thickness": None,
                 "form": None, "terms": []}
    if not q:
        return out
    text = " " + apply_aliases(q.lower().strip()) + " "

    m = _THICK_RE.search(text)
    if m:
        val = float(m.group(1))
        cm = val / 10 if m.group(2) == "mm" else val
        out["thickness"] = f"{cm:g}cm"
        text = text[: m.start()] + " " + text[m.end():]

    for phrase, label in CANONICAL_TYPES:
        if out["material_type"] is None:
            text, hit = _take(text, phrase)
            if hit:
                out["material_type"] = label

    for phrase in COLOR_WORDS:
        if out["color"] is None:
            text, hit = _take(text, phrase)
            if hit:
                out["color"] = phrase

    for phrase, canon in FORM_WORDS.items():
        if out["form"] is None:
            text, hit = _take(text, phrase)
            if hit:
                out["form"] = canon

    # Finishes stay as name terms (they live in the item name), but are recognised
    # so they display as understood and aren't dropped as stopwords.
    terms: list[str] = []
    for phrase in FINISH_WORDS:
        text, hit = _take(text, phrase)
        if hit:
            terms.append(phrase)

    for tok in re.split(r"[^a-z0-9]+", text):
        if tok and tok not in _STOP and len(tok) > 1:
            terms.append(tok)

    seen: set[str] = set()
    out["terms"] = [t for t in terms if not (t in seen or seen.add(t))]
    return out


def color_base(color_phrase: str) -> str:
    """Base color word for lenient matching: 'light blue' -> 'blue'."""
    return re.split(r"[\s-]+", color_phrase)[-1]


def summary(parsed: dict) -> list[str]:
    """Human-readable chips of what was understood, for display."""
    chips = []
    if parsed.get("material_type"):
        chips.append(parsed["material_type"])
    if parsed.get("color"):
        chips.append(parsed["color"].title())
    if parsed.get("thickness"):
        chips.append(parsed["thickness"])
    if parsed.get("form"):
        chips.append({"slab": "Slabs", "tile": "Tiles", "remnant": "Remnants"}[parsed["form"]])
    for t in parsed.get("terms", []):
        chips.append(f'"{t}"')
    for w in parsed.get("fuzzy", []):
        chips.append(f"or ~{w.title()}")
    return chips
