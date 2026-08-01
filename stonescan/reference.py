"""Stone reference: what a material actually IS, beyond who stocks it.

The catalog answers "who has it and how much". It cannot answer "what is this
stone, where does it come from, who quarries it, what should it cost" — and not
for want of a join. We measured: `materials.origin` has 44 distinct values of
which exactly three are countries (the other 41 are US warehouse cities like
"Anaheim, CA"), and `materials.price_range` is 76% supplier tier codes ("Group 5",
"Caesarstone B - Standard") rather than money. So every fact in this module comes
from outside the catalog, and that is the whole design problem.

**Nothing here is allowed to be unsourced.** A quarry list or price band invented
from plausible-sounding memory is exactly the failure this project keeps digging
out of — a confident wrong answer costs more trust than a blank field. So:

  * every fact carries `sources` (real URLs, actually fetched) and a `confidence`,
  * a fact with no source is not stored; the field reads "not established",
  * `curated` entries are researched and adversarially fact-checked offline;
    `looked_up` entries were fetched live by the app and are labeled as such,
  * the UI renders the citation next to the fact, not in a footnote.

Two tiers, deliberately kept apart in the UI as well as here:

    catalog facts   — verifiable against the app's own data, link to a supplier
    reference facts — external, cited, dated, and fallible

Engineered stone is not quarried. Silestone/Caesarstone/Dekton entries carry a
`manufacturer` instead, because rendering a "Quarries" heading for a factory
product would be a category error dressed up as data.

File: `stone_reference.json` at the project root, beside suppliers.json /
locations.json / denylist.json, and bundled into the frozen app the same way.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

REFERENCE_FILE = Path(
    os.environ.get("STONESCAN_REFERENCE")
    or (Path(__file__).resolve().parent.parent / "stone_reference.json")
)

# Confidence ladder, worst to best. `unknown` means "we have no supported value",
# NOT "low" — the UI must be able to tell those apart.
CONFIDENCE_ORDER = {"unknown": 0, "low": 1, "medium": 2, "high": 3}

_TEMPLATE: dict[str, Any] = {
    "_comment": (
        "Stone reference data: origin, quarries and market price per trade name. "
        "Every fact must carry real source URLs and a confidence; unsourced facts are "
        "not stored (the UI shows 'not established' instead). 'curated' entries were "
        "researched and fact-checked offline; 'looked_up' entries were fetched live by "
        "the app. Edit freely — this is a plain allow-list style file like suppliers.json."
    ),
    "stones": [],
}


def _norm(s: str) -> str:
    """Loose match key: case/punctuation-insensitive trade name."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _base_of(material_key: str) -> str:
    """'taj mahal|quartzite' -> 'taj mahal'."""
    return (material_key or "").split("|")[0]


def load() -> list[dict]:
    if not REFERENCE_FILE.exists():
        return []
    try:
        return json.loads(REFERENCE_FILE.read_text(encoding="utf-8")).get("stones", [])
    except (OSError, json.JSONDecodeError):
        # A corrupt reference must not silently become "we know nothing" — that would
        # quietly strip every citation from the UI and look like clean missing data.
        raise


def save(stones: list[dict]) -> None:
    data = dict(_TEMPLATE)
    if REFERENCE_FILE.exists():
        try:
            data["_comment"] = json.loads(
                REFERENCE_FILE.read_text(encoding="utf-8")).get("_comment", data["_comment"])
        except (OSError, json.JSONDecodeError):
            pass
    data["stones"] = sorted(stones, key=lambda s: s.get("key") or s.get("display_name", ""))
    REFERENCE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")


def _index(stones: list[dict]) -> dict[str, dict]:
    """Lookup by exact material_key AND by normalized trade name, so a stone
    researched as 'taj mahal|quartzite' still answers for 'Taj Mahal 3cm' rows
    whose key differs by type (the trade mislabels constantly)."""
    idx: dict[str, dict] = {}
    for s in stones:
        if s.get("key"):
            idx.setdefault(s["key"].lower(), s)
            idx.setdefault(_norm(_base_of(s["key"])), s)
        for name in [s.get("display_name", "")] + list(s.get("also_known_as") or []):
            if name:
                idx.setdefault(_norm(name), s)
    return idx


def _brand_index(stones: list[dict]) -> dict[str, dict]:
    """Engineered BRAND entries, keyed by every name they answer to.

    Engineered surfaces are branded product lines, not quarried stones: the catalog
    carries thousands of colours ("Calacatta Gold Silestone", "Dekton Aura") that all
    share one set of facts — who makes it and what it's made of. So the entry is per
    brand, and the colour name is matched against it rather than researched separately.
    """
    idx: dict[str, dict] = {}
    for s in stones:
        if s.get("formation") != "engineered":
            continue
        for nm in [s.get("display_name", ""), *(s.get("also_known_as") or [])]:
            n = _norm(nm)
            if n:
                idx.setdefault(n, s)
    return idx


def _brand_match(text: str, stones: list[dict]) -> dict | None:
    """The engineered brand named inside a product name, or None.

    Matched on WHOLE WORDS of the normalized name, so "Dekton Aura" finds Dekton while
    a stone that merely contains the letters does not. The longest brand wins, so a
    two-word brand ("LX Viatera") beats a one-word substring of it.
    """
    words = set(_norm(text).split())
    if not words:
        return None
    best: tuple[int, dict] | None = None
    for brand, entry in _brand_index(stones).items():
        toks = brand.split()
        if toks and all(t in words for t in toks):
            if best is None or len(toks) > best[0]:
                best = (len(toks), entry)
    return best[1] if best else None


# Types that are MANUFACTURED. Forgiving name matching is right within quarried stone —
# "Fantasy Brown" is the same rock whether a supplier files it as marble or quartzite —
# but it must not cross this line: a factory slab named after a marble is not that marble,
# and serving it the marble's cited quarry states a fact that is simply untrue.
_ENGINEERED_TYPES = frozenset({
    "quartz", "porcelain", "sintered stone", "engineered stone", "engineered glass",
    "engineered marble", "solid surface", "ceramic", "terrazzo",
})


def _is_engineered_key(material_key: str) -> bool:
    """Is this material_key's canonical type a manufactured surface?
    The key is "<base>|<type>", so the type is already there — no extra plumbing."""
    return material_key.rpartition("|")[2].strip().lower() in _ENGINEERED_TYPES


def lookup(material_key: str = "", name: str = "", stones: list[dict] | None = None) -> dict | None:
    """The reference entry for a material, or None if we have not established one.

    Matching is deliberately forgiving on TYPE but strict on NAME: "Fantasy Brown"
    is sold as marble by some suppliers and quartzite by others, and it is the same
    rock either way. Matching on the trade name is what makes one researched entry
    serve every supplier's spelling of it.

    That forgiveness stops at the natural/engineered line. A quarried-stone entry is
    never served to a manufactured surface — "Calacatta Oro" quartz is not the Carrara
    marble it is named after, and printing that marble's quarry beside a factory slab
    is a confident wrong answer, which costs more trust than a blank field. The
    rejected candidate falls through to the brand lookup below rather than ending the
    search, so an engineered product can still find the entry that does describe it.

    An engineered BRAND named inside the product name is the last resort, tried only
    after every exact match fails — so a stone with its own researched entry always
    wins over the brand it happens to mention.
    """
    st = stones if stones is not None else load()
    idx = _index(st)
    engineered = _is_engineered_key(material_key)
    for probe in (material_key.lower(), _norm(_base_of(material_key)), _norm(name)):
        if probe and probe in idx:
            hit = idx[probe]
            # Only an explicitly NATURAL entry is refused: lookup_live() writes
            # formation "unknown", and those must keep resolving.
            if engineered and hit.get("formation") == "natural":
                continue
            return hit
    return _brand_match(name, st) or _brand_match(_base_of(material_key), st)


def has_fact(fact: Any) -> bool:
    """True when a fact block carries something we are willing to display: a value
    AND at least one source. Confidence alone is not enough."""
    if not isinstance(fact, dict):
        return False
    if not fact.get("sources"):
        return False
    if fact.get("confidence", "unknown") == "unknown":
        return False
    return any(fact.get(k) for k in ("country", "low", "high", "name", "value"))


def price_label(price: dict | None) -> str:
    """'$60–$100 / sq ft installed' — never a bare number, because slab-only and
    installed differ by roughly 2x and an unlabeled figure misleads."""
    if not has_fact(price):
        return ""
    lo, hi = price.get("low"), price.get("high")
    cur = "$" if (price.get("currency") or "USD").upper() == "USD" else ""
    if lo and hi and lo != hi:
        span = f"{cur}{lo:g}–{cur}{hi:g}"
    elif lo or hi:
        span = f"{cur}{(lo or hi):g}"
    else:
        return ""
    basis = (price.get("basis") or "").strip()
    return f"{span} / sq ft" + (f" · {basis}" if basis else "")


def manufacturer_fact(entry: dict) -> dict:
    """The manufacturer as a SOURCED fact, in the same shape as origin/price.

    For an engineered surface "who makes it" is the load-bearing claim — the equivalent
    of origin for a quarried stone — so it has to carry citations rather than sit in the
    file as a bare string. Older entries store a plain string; those still display, but
    without sources they never pass has_fact and so are never presented as established.
    """
    m = entry.get("manufacturer")
    if isinstance(m, dict):
        return m
    return {"name": m, "confidence": "unknown", "sources": []} if m else {}


def summarize(entry: dict | None) -> dict:
    """Flatten one entry into what a template needs, with every fact pre-checked
    for displayability so the template never has to decide what is trustworthy."""
    if not entry:
        return {"known": False}
    origin = entry.get("origin") or {}
    price = entry.get("price_per_sqft") or {}
    quarries = [q for q in (entry.get("quarries") or []) if q.get("sources") and q.get("name")]
    return {
        "known": True,
        "key": entry.get("key", ""),
        "display_name": entry.get("display_name", ""),
        "formation": entry.get("formation", "unknown"),
        "stone_type": entry.get("stone_type", ""),
        "trade_type_note": entry.get("trade_type_note", ""),
        "summary": entry.get("summary", ""),
        "also_known_as": entry.get("also_known_as") or [],
        "origin": origin if has_fact(origin) else None,
        "origin_text": ", ".join(x for x in (origin.get("region"), origin.get("country")) if x)
                       if has_fact(origin) else "",
        "price": price if has_fact(price) else None,
        "price_text": price_label(price),
        "quarries": quarries,
        # Engineered surfaces have a maker instead of a quarry; the fact form carries its
        # citations, the plain text keeps older string-only entries rendering.
        "manufacturer": manufacturer_fact(entry).get("name", ""),
        "manufacturer_fact": (manufacturer_fact(entry)
                              if has_fact(manufacturer_fact(entry)) else None),
        "engineered": entry.get("formation") == "engineered",
        "notes": entry.get("notes", ""),
        "provenance": entry.get("provenance", "curated"),
        "updated": entry.get("updated", ""),
        # Anything we could not establish, so the UI can say so out loud instead of
        # rendering an empty box that reads as "this stone has no quarries".
        "gaps": [label for label, ok in (
            ("origin", has_fact(origin)),
            ("price", has_fact(price)),
            ("quarries", bool(quarries)),
        ) if not ok],
    }


# --- general care, by material category -----------------------------------------

# Care guidance keyed on MATERIAL CATEGORY, not trade name. This is established
# material-category knowledge (marble is calcite-based and acid-sensitive; quartz is
# non-porous) — never a claim about a specific quarry or slab — so, unlike origin/price,
# it needs no per-stone citation. The UI labels it "general, by material type" to keep
# it visibly distinct from the cited reference facts. Unknown type -> no panel shown.
_CARE_BY_TYPE: dict[str, dict[str, str]] = {
    "marble": {"porosity": "Porous, calcite-based", "sealing": "Seal periodically",
               "durability": "Acid-sensitive — etches from citrus, wine, vinegar",
               "best_for": "Vanities, low-traffic counters, feature walls, floors"},
    "granite": {"porosity": "Dense, low porosity", "sealing": "Seal yearly (some lines rarely need it)",
                "durability": "Acid-resistant, very hard, heat-tolerant",
                "best_for": "Kitchen counters, high-traffic and outdoor surfaces"},
    "quartzite": {"porosity": "Dense but variable porosity", "sealing": "Seal on install, re-test yearly",
                  "durability": "Acid-resistant, harder than granite",
                  "best_for": "Kitchen counters and high-use surfaces"},
    "quartz": {"porosity": "Non-porous (engineered)", "sealing": "No sealing needed",
               "durability": "Stain-resistant, but not heat- or UV-proof — no hot pans or direct sun",
               "best_for": "Indoor kitchen and bath counters"},
    "dolomite": {"porosity": "Porous — between marble and quartzite", "sealing": "Seal periodically",
                 "durability": "Mildly acid-sensitive", "best_for": "Counters and vanities, with some care"},
    "soapstone": {"porosity": "Non-porous", "sealing": "No sealing; oil to even the patina",
                  "durability": "Acid- and heat-proof, but softer — scratches show",
                  "best_for": "Kitchen counters, sinks, hearths, lab tops"},
    "limestone": {"porosity": "Very porous", "sealing": "Seal well and often",
                  "durability": "Acid-sensitive and softer", "best_for": "Floors, walls, exterior cladding"},
    "travertine": {"porosity": "Porous, naturally pitted", "sealing": "Seal; consider a filled finish",
                   "durability": "Acid-sensitive", "best_for": "Floors, walls, pool decks, exteriors"},
    "sandstone": {"porosity": "Porous", "sealing": "Seal for stain resistance",
                  "durability": "Moderately durable outdoors", "best_for": "Paving, cladding, exteriors"},
    "onyx": {"porosity": "Porous, soft, often translucent", "sealing": "Seal and handle gently",
             "durability": "Acid-sensitive and delicate",
             "best_for": "Backlit features and accents — not working counters"},
    "slate": {"porosity": "Low porosity", "sealing": "Optional sealing enhances color",
              "durability": "Durable, naturally cleft", "best_for": "Floors, roofing, exteriors"},
    "sintered stone": {"porosity": "Non-porous (fired)", "sealing": "No sealing needed",
                       "durability": "Acid-, heat-, scratch- and UV-proof",
                       "best_for": "Indoor and outdoor counters, facades, high-use surfaces"},
    "porcelain": {"porosity": "Non-porous", "sealing": "No sealing needed",
                  "durability": "Acid- and heat-resistant, UV-stable",
                  "best_for": "Counters, floors, walls, exteriors"},
    "terrazzo": {"porosity": "Depends on binder (cement porous, resin non-porous)",
                 "sealing": "Seal cement-based; resin needs none",
                 "durability": "Cement-based can etch from acids", "best_for": "Floors, counters, features"},
}


def care_by_type(stone_type: str) -> dict:
    """General care guidance for a MATERIAL CATEGORY (not a specific stone): porosity,
    sealing, acid/heat tolerance, typical uses. Category-level material science, so it
    carries no per-stone citation. Empty dict when we have no guidance for the type."""
    return _CARE_BY_TYPE.get((stone_type or "").strip().lower(), {})


# Typical market price per square foot, per MATERIAL CATEGORY. Unlike care guidance —
# which is material science and needs no citation — a price is a claim about the world,
# so each band carries its sources and the BASIS it was quoted on. Installed and
# slab-only differ by roughly 2x, so a band without a stated basis is worthless and is
# not stored. This is never mixed with `_observed_prices` (real money found in supplier
# catalogs): that is what THIS catalog says, this is what the market generally says.
_PRICE_BY_TYPE: dict[str, dict] = {
    'quartz': {"low": 50, "high": 200, "currency": 'USD', "basis": 'installed',
        "confidence": 'high',
        "sources": [{"url": 'https://www.bobvila.com/articles/quartz-countertops-cost/', "title": 'Bob Vila — Quartz Countertops Cost: A Budgeting Guide'},
                    {"url": 'https://www.fixr.com/costs/install-quartz-countertop', "title": 'Fixr.com — How Much Are Quartz Countertops?'}]},
    'granite': {"low": 90, "high": 150, "currency": 'USD', "basis": 'installed',
        "confidence": 'medium',
        "sources": [{"url": 'https://gwsurfaces.com/comparing-costs-granite-quartz-marble-countertops/', "title": 'GW Surfaces — Comparing Costs: Granite vs. Quartz vs. Marble'},
                    {"url": 'https://www.homewyse.com/services/cost_to_install_granite_countertops.html', "title": 'Homewyse — Cost to Install Granite Countertops'}]},
    'marble': {"low": 50, "high": 200, "currency": 'USD', "basis": 'installed',
        "confidence": 'medium',
        "sources": [{"url": 'https://www.fixr.com/costs/marble-countertops', "title": 'Fixr.com — Cost to Install a Marble Countertop'},
                    {"url": 'https://www.homewyse.com/services/cost_to_install_marble_countertop.html', "title": 'Homewyse — Cost to Install Marble Countertop'}]},
    'quartzite': {"low": 65, "high": 210, "currency": 'USD', "basis": 'installed',
        "confidence": 'high',
        "sources": [{"url": 'https://www.fixr.com/costs/quartzite-countertop-installation', "title": 'Fixr — Cost to Install Quartzite Countertops'},
                    {"url": 'https://marble.com/articles/what-to-expect-from-quartzite-countertop-costs', "title": 'Marble.com — Average Cost of Quartzite Countertops'}]},
    'sintered stone': {"low": 60, "high": 200, "currency": 'USD', "basis": 'installed',
        "confidence": 'medium',
        "sources": [{"url": 'https://www.housedigest.com/1930537/cost-sintered-stone-countertops/', "title": 'House Digest — Sintered Stone Countertops cost'},
                    {"url": 'https://carmelimports.com/blog/engineered-stone-countertops-cost/', "title": 'Carmel Imports — Engineered Stone Countertops Cost'}]},
    'soapstone': {"low": 70, "high": 120, "currency": 'USD', "basis": 'slab only',
        "confidence": 'high',
        "sources": [{"url": 'https://www.homeadvisor.com/cost/cabinets-and-countertops/soapstone-counter-install/', "title": 'HomeAdvisor — What Do Soapstone Countertops Cost?'},
                    {"url": 'https://www.bobvila.com/articles/soapstone-countertops-cost/', "title": 'Bob Vila — How Much Do Soapstone Countertops Cost?'}]},
    'limestone': {"low": 70, "high": 150, "currency": 'USD', "basis": 'installed',
        "confidence": 'medium',
        "sources": [{"url": 'https://www.homeadvisor.com/cost/cabinets-and-countertops/limestone-countertops', "title": 'HomeAdvisor — Limestone Countertops Cost'}]},
    'travertine': {"low": 25, "high": 50, "currency": 'USD', "basis": 'slab only',
        "confidence": 'medium',
        "sources": [{"url": 'https://www.homeadvisor.com/cost/cabinets-and-countertops/travertine-countertops/', "title": 'HomeAdvisor — Travertine Countertop Cost'}]},
    'onyx': {"low": 75, "high": 300, "currency": 'USD', "basis": 'installed',
        "confidence": 'low',
        "sources": [{"url": 'https://countertopadvisor.com/types-of-countertops/onyx-countertops/', "title": 'Countertop Advisor — Onyx Countertops'}]},
    # Porcelain is deliberately absent: the band researched for it did not survive
    # citation checking, and a category with no defensible source gets no band.
}


def price_band_by_type(stone_type: str) -> dict:
    """Typical $/sq ft for a MATERIAL CATEGORY, or {} when we have no sourced band.

    Category-level and labeled as such — it must never be read as this stone's price, and
    never as a price observed in the catalog. Same honesty contract as everything else
    here: no sources, no band.
    """
    band = _PRICE_BY_TYPE.get((stone_type or "").strip().lower())
    if not band or not band.get("sources"):
        return {}
    return band


def price_band_label(band: dict | None) -> str:
    """'$50–$100 / sq ft · installed' — always carries the basis, for the same reason
    price_label does: an unlabeled figure invites a 2x misread."""
    if not band:
        return ""
    return price_label({**band, "confidence": band.get("confidence", "medium"),
                        "sources": band.get("sources") or []})


# --- live lookup ----------------------------------------------------------------

WIKI_API = "https://en.wikipedia.org/w/api.php"
_STONE_WORDS = re.compile(
    r"granite|quartzite|marble|limestone|dolomit|soapstone|onyx|travertine|"
    r"gneiss|schist|slate|basalt|sandstone|quarr", re.I)


def _is_about(name: str, title: str, text: str) -> bool:
    """A live-fetched page must be about THIS trade stone, not merely mention rock.

    Searching Wikipedia for a commercial name surfaces same-named bands, monuments
    and generic articles: "Black Galaxy" -> a Minnesota gneiss, "Absolute Black" ->
    a rock band, "Taj Mahal" -> the mausoleum, "Taj Mahal quartzite" -> "Stones of
    India". All of those pass a bare rock-vocabulary check, so it is not enough.
    Requiring the normalized trade name to appear as a phrase in the title or intro,
    on top of the rock words, turns those misses into an honest "not established"
    instead of a confident wrong page. Precision over recall: a fallback that
    sometimes says nothing is fine; one that describes the wrong rock is not."""
    n = _norm(name)
    return bool(n) and n in _norm(f"{title} {text}") and bool(_STONE_WORDS.search(text))


def _wiki(session, params: dict) -> dict:
    r = session.get(WIKI_API, params={**params, "format": "json"}, timeout=20)
    r.raise_for_status()
    return r.json()


def lookup_live(name: str, stone_type: str = "", material_key: str = "") -> dict | None:
    """Fetch what we can for a stone we have no curated entry for.

    Scope is deliberately narrow. Wikipedia has a real API, is citable, and is
    robots-clean by design — so it can answer "what is this rock and where is it
    from" for the well-known names. It cannot price a slab or name a quarry, and
    this function does not pretend otherwise: those fields come back unknown, and
    the UI keeps showing "not established" rather than filling them with
    whatever a fabricator's marketing page claims.

    Returns an entry dict (provenance="looked_up") or None if nothing was found.
    """
    import httpx

    from .robots import USER_AGENT

    query = f"{name} {stone_type}".strip()
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as c:
            hits = _wiki(c, {"action": "query", "list": "search",
                             "srsearch": f"{query} stone quarry", "srlimit": 5})
            titles = [h["title"] for h in hits.get("query", {}).get("search", [])]
            if not titles:
                return None
            # Prefer a page that is actually about rock, not a same-named place/person.
            page = None
            for t in titles:
                ext = _wiki(c, {"action": "query", "prop": "extracts|info",
                                "exintro": 1, "explaintext": 1, "inprop": "url",
                                "titles": t})
                pages = list((ext.get("query", {}).get("pages") or {}).values())
                if not pages:
                    continue
                p = pages[0]
                text = p.get("extract") or ""
                if _is_about(name, p.get("title") or t, text):
                    page = p
                    break
            if page is None:
                return None
    except Exception:  # noqa: BLE001 - a failed lookup is a gap, never a crash
        return None

    extract = (page.get("extract") or "").strip()
    url = page.get("fullurl") or f"https://en.wikipedia.org/wiki/{page.get('title', '')}"
    src = [{"url": url, "title": f"Wikipedia — {page.get('title', '')}"}]
    return {
        "key": material_key,
        "display_name": name.title(),
        "formation": "unknown",
        "stone_type": stone_type,
        "trade_type_note": "",
        "also_known_as": [],
        "summary": extract[:600],
        # Deliberately unknown: a Wikipedia intro is not a quarry registry or a
        # price list, and guessing from it would defeat the point of citing.
        "origin": {"country": "", "region": "", "confidence": "unknown", "sources": src},
        "price_per_sqft": {"confidence": "unknown", "sources": []},
        "quarries": [],
        "manufacturer": "",
        "notes": "Fetched live from Wikipedia. Origin, quarries and price were not "
                 "established — those need curated research.",
        "provenance": "looked_up",
        "updated": date.today().isoformat(),
    }


def remember(entry: dict) -> None:
    """Persist a looked-up entry so the next open is instant and the file grows."""
    stones = load()
    key = (entry.get("key") or "").lower()
    nm = _norm(entry.get("display_name", ""))
    stones = [s for s in stones
              if (s.get("key") or "").lower() != key or not key
              if _norm(s.get("display_name", "")) != nm or not nm]
    stones.append(entry)
    save(stones)


def stats() -> dict:
    stones = load()
    return {
        "total": len(stones),
        "curated": sum(1 for s in stones if s.get("provenance", "curated") == "curated"),
        "looked_up": sum(1 for s in stones if s.get("provenance") == "looked_up"),
        "with_quarries": sum(1 for s in stones if s.get("quarries")),
        "with_price": sum(1 for s in stones if has_fact(s.get("price_per_sqft"))),
        "with_origin": sum(1 for s in stones if has_fact(s.get("origin"))),
        # Engineered surfaces are a different shape of entry (a manufacturer, no quarry),
        # and they were 0 of 175 while being ~28% of the catalog — worth counting apart.
        "engineered": sum(1 for s in stones if s.get("formation") == "engineered"),
        "price_bands": sum(1 for b in _PRICE_BY_TYPE.values() if b.get("sources")),
    }


def _strip_unsourced(entry: dict) -> tuple[dict, list[str]]:
    """Drop anything that arrived without a real source before it is stored.

    The research pipeline is instructed to leave facts empty rather than guess, but
    this is the gate that makes that a property of the FILE rather than a hope about
    the pipeline: a fact with no source URL never lands, no matter who produced it.
    """
    removed = []
    for field in ("origin", "price_per_sqft"):
        fact = entry.get(field)
        if isinstance(fact, dict) and not fact.get("sources"):
            entry[field] = {"confidence": "unknown", "sources": []}
            removed.append(field)
    quarries = entry.get("quarries") or []
    kept = [q for q in quarries if q.get("sources") and q.get("name")]
    if len(kept) != len(quarries):
        removed.append(f"{len(quarries) - len(kept)} quarry claim(s)")
    entry["quarries"] = kept
    return entry, removed


def import_entries(entries: list[dict], provenance: str = "curated") -> tuple[int, int, list[str]]:
    """Merge researched entries into the reference file. Returns (added, updated, notes)."""
    existing = {(_norm(s.get("key", "")) or _norm(s.get("display_name", ""))): s
                for s in load()}
    notes: list[str] = []
    added = updated = 0
    for raw in entries:
        if not isinstance(raw, dict) or not raw.get("display_name"):
            continue
        entry, removed = _strip_unsourced(dict(raw))
        entry.setdefault("provenance", provenance)
        entry.setdefault("updated", date.today().isoformat())
        if removed:
            notes.append(f"{entry['display_name']}: dropped unsourced {', '.join(removed)}")
        ident = _norm(entry.get("key", "")) or _norm(entry["display_name"])
        if ident in existing:
            updated += 1
        else:
            added += 1
        existing[ident] = entry
    save(list(existing.values()))
    return added, updated, notes


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Manage the stone reference (origin, quarries, market price).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="Merge researched entries from a JSON file.")
    imp.add_argument("path", help='JSON: a list of entries, or {"stones": [...]}.')
    imp.add_argument("--provenance", default="curated", choices=["curated", "looked_up"])
    sub.add_parser("stats", help="Coverage summary.")
    ls = sub.add_parser("list", help="List referenced stones.")
    ls.add_argument("--gaps", action="store_true", help="Only those missing facts.")
    lk = sub.add_parser("lookup", help="Show the entry that answers for a name/key.")
    lk.add_argument("name")
    args = ap.parse_args()

    if args.cmd == "import":
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
        entries = data.get("stones", data) if isinstance(data, dict) else data
        added, updated, notes = import_entries(entries, args.provenance)
        print(f"Imported {added} new, {updated} updated -> {REFERENCE_FILE}")
        for n in notes:
            print(f"  ! {n}")
        print("  " + json.dumps(stats()))
    elif args.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.cmd == "lookup":
        e = lookup(material_key=args.name, name=args.name)
        print(json.dumps(summarize(e), indent=2, ensure_ascii=False) if e
              else f"No reference entry answers for {args.name!r}.")
    else:
        for s in load():
            v = summarize(s)
            if args.gaps and not v["gaps"]:
                continue
            gap = f"  (missing: {', '.join(v['gaps'])})" if v["gaps"] else ""
            print(f"  {v['display_name']:<26} {v['stone_type']:<12} "
                  f"{v['origin_text'][:24]:<26}{len(v['quarries'])} quarr{'y' if len(v['quarries']) == 1 else 'ies'}{gap}")


if __name__ == "__main__":
    main()
