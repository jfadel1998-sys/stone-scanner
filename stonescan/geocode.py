"""Turn the catalogs' free-text stocking locations into map coordinates.

Suppliers type whatever they like into a slab's location field. In practice it is
one of three things:

  "Dallas", "Calgary"        -> a plain city name
  "Harrisburg, PA"           -> city + state
  "Arca - West Palm Beach"   -> a city buried in a warehouse label
  "KLZ", "BF", "HG-NJ"       -> an internal warehouse name that is not a place

Only the first three can be resolved. The fourth is *not guessed*: an unresolved
location stays unresolved and is reported as such, because a wrong pin on a map
is worse than a missing one. To place those, add them to `locations.json`.

Resolution is fully offline (bundled city dataset), so the packaged app needs no
API key and no network:

    python -m stonescan.geocode            # resolve + report coverage
    python -m stonescan.geocode --recheck  # re-resolve everything from scratch
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from . import db

EARTH_MI = 3958.8


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/long points, in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_MI * 2 * math.asin(math.sqrt(a))


def locations_within(conn, lat: float, lon: float, radius_mi: float) -> dict[str, float]:
    """Map each stored location within `radius_mi` of (lat, lon) to its distance.

    Keys are lowercased so the caller can match against a slab's raw location text.
    Only locations we've actually resolved to coordinates can be in range — the
    rest (internal yard names) are simply never near anything."""
    out: dict[str, float] = {}
    for r in conn.execute("SELECT location, lat, lon FROM location_geo WHERE lat IS NOT NULL"):
        d = haversine_mi(lat, lon, r["lat"], r["lon"])
        if d <= radius_mi:
            out[r["location"].strip().lower()] = round(d, 1)
    return out

# User-editable overrides, e.g. mapping an internal warehouse name to a place.
# Env-overridable so the packaged app can point at its writable data dir (the
# bundle itself is read-only), same as suppliers.json.
OVERRIDES_PATH = Path(os.environ.get("STONESCAN_LOCATIONS")
                      or (Path(__file__).resolve().parent.parent / "locations.json"))

_CACHE: dict[str, Any] = {}

# Separators suppliers use between a label and a place ("Arca - Warehouse",
# "Dallas-AMARA", "HG-NJ").
_SPLIT = re.compile(r"\s*[-–—/|]\s*|\s{2,}")
_US_STATE_RE = re.compile(r"^(.*?),\s*([A-Za-z]{2})\.?$")
_ZIP_RE = re.compile(r"^\s*(\d{5})(?:-\d{4})?\s*$")
_ZIP_PATH = Path(__file__).resolve().parent / "data" / "us_zips.json.gz"


def _zips() -> dict[str, list]:
    """US zip -> [lat, lon, 'City, ST'], from the bundled centroid file (offline)."""
    if "zips" not in _CACHE:
        import gzip
        try:
            with gzip.open(_ZIP_PATH, "rt", encoding="utf-8") as f:
                _CACHE["zips"] = json.load(f)
        except (OSError, json.JSONDecodeError):
            _CACHE["zips"] = {}
    return _CACHE["zips"]


def resolve_zip(text: str) -> dict | None:
    """Resolve a US 5-digit zip (or ZIP+4) to {lat, lon, label} from bundled data."""
    m = _ZIP_RE.match(text or "")
    if not m:
        return None
    z = _zips().get(m.group(1))
    if not z:
        return None
    return {"lat": z[0], "lon": z[1], "label": z[2], "source": "zip"}


def load_overrides() -> dict[str, dict]:
    """Hand-placed locations from locations.json (keys are matched case-insensitively)."""
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    for k, v in raw.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue  # "_readme" and friends
        # Seeded-but-unfilled entries have lat/lon null: they are a to-do list,
        # not a placement, so they must not count as an override.
        if v.get("lat") is not None and v.get("lon") is not None:
            out[k.strip().lower()] = v
    return out


def _cities():
    """Name -> [city records], from the bundled dataset (cities over ~15k people).

    Canonical names only. The dataset's `alternatenames` are a magnet for false
    positives here — they turn "Arca - Warehouse" into Arsk, Russia — and a
    confidently wrong pin is the one failure this module exists to avoid."""
    if "by_name" not in _CACHE:
        import geonamescache
        gc = geonamescache.GeonamesCache()
        by_name: dict[str, list] = {}
        for c in gc.get_cities().values():
            by_name.setdefault(c["name"].lower(), []).append(c)
        _CACHE["by_name"] = by_name
    return _CACHE["by_name"]


def _pick(matches: list, state: str = "") -> tuple[dict | None, bool]:
    """Choose among same-named cities. Returns (city, ambiguous).

    Without a state there is no way to tell Columbia SC from Columbia MO, so the
    biggest city wins but the result is flagged ambiguous rather than presented
    as fact."""
    if state:
        exact = [c for c in matches if (c.get("admin1code") or "").upper() == state.upper()]
        if exact:
            return max(exact, key=lambda c: c["population"]), False
        return None, False
    if not matches:
        return None, False
    ranked = sorted(matches, key=lambda c: c["population"], reverse=True)
    # A single real candidate, or one that dwarfs the rest, is safe enough.
    big = [c for c in ranked if c["population"] > 0]
    ambiguous = len(big) > 1 and big[1]["population"] * 4 > big[0]["population"]
    return ranked[0], ambiguous


def _label(city: dict) -> str:
    admin = city.get("admin1code") or ""
    cc = city.get("countrycode") or ""
    bits = [city["name"]] + ([admin] if admin and cc == "US" else []) + ([cc] if cc != "US" else [])
    return ", ".join(bits)


def resolve(location: str) -> dict | None:
    """Resolve one location string to {lat, lon, label, source}, or None.

    source: override | zip | exact | state | parsed | ambiguous
    """
    raw = (location or "").strip()
    if not raw:
        return None

    ov = load_overrides().get(raw.lower())
    if ov:
        return {"lat": float(ov["lat"]), "lon": float(ov["lon"]),
                "label": ov.get("label") or raw, "source": "override"}

    # A US zip is unambiguous — resolve it before city-name matching.
    zhit = resolve_zip(raw)
    if zhit:
        return zhit

    by_name = _cities()

    # "Harrisburg, PA" — the state makes it unambiguous.
    m = _US_STATE_RE.match(raw)
    if m:
        city, _amb = _pick(by_name.get(m.group(1).strip().lower(), []), m.group(2))
        if city:
            return {"lat": float(city["latitude"]), "lon": float(city["longitude"]),
                    "label": _label(city), "source": "state"}

    # A plain city name.
    city, ambiguous = _pick(by_name.get(raw.lower(), []))
    if city:
        return {"lat": float(city["latitude"]), "lon": float(city["longitude"]),
                "label": _label(city), "source": "ambiguous" if ambiguous else "exact"}

    # A city inside a warehouse label: "Arca - West Palm Beach", "Dallas-AMARA".
    # Longest part first, so a real place beats a two-letter site code.
    parts = sorted((p.strip() for p in _SPLIT.split(raw) if p.strip()),
                   key=len, reverse=True)
    for part in parts:
        if len(part) < 5:
            continue  # "BF", "HG", "RF" are codes, not places
        city, ambiguous = _pick(by_name.get(part.lower(), []))
        if city:
            return {"lat": float(city["latitude"]), "lon": float(city["longitude"]),
                    "label": _label(city), "source": "ambiguous" if ambiguous else "parsed"}

    # Last resort: a known city name appearing as whole words anywhere in the
    # string ("CRS San Antonio"). Look up word runs from the string rather than
    # scanning every city name — the dataset has ~25k of them.
    words = [w for w in re.split(r"[^A-Za-z]+", raw.lower()) if w]
    for size in (4, 3, 2, 1):  # longest run first: "West Palm Beach" before "Beach"
        for i in range(len(words) - size + 1):
            cand = " ".join(words[i:i + size])
            if len(cand) < 5:
                continue  # too short to be anything but noise
            city, ambiguous = _pick(by_name.get(cand, []))
            if city:
                return {"lat": float(city["latitude"]), "lon": float(city["longitude"]),
                        "label": _label(city), "source": "ambiguous" if ambiguous else "parsed"}
    return None


def resolve_all(conn, recheck: bool = False) -> dict[str, int]:
    """Resolve every distinct slab location and cache the result in the DB."""
    known = {} if recheck else {
        r["location"]: r for r in conn.execute("SELECT location FROM location_geo")
    }
    locs = [r["location"] for r in conn.execute(
        "SELECT DISTINCT location FROM slabs WHERE location <> ''"
    )]
    stats = {"resolved": 0, "unresolved": 0, "skipped": 0}
    for loc in locs:
        if loc in known:
            stats["skipped"] += 1
            continue
        hit = resolve(loc)
        db.save_location_geo(conn, loc, hit)
        stats["resolved" if hit else "unresolved"] += 1
    return stats


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Resolve catalog locations to coordinates.")
    ap.add_argument("--recheck", action="store_true",
                    help="re-resolve every location, ignoring cached results")
    args = ap.parse_args()

    conn = db.init_db()
    stats = resolve_all(conn, recheck=args.recheck)
    rows = db.location_geo(conn)
    mapped = [r for r in rows.values() if r["lat"] is not None]
    print(f"resolved {stats['resolved']}, unresolved {stats['unresolved']}, "
          f"cached {stats['skipped']}")
    print(f"{len(mapped)}/{len(rows)} locations on the map")
    unmapped = [k for k, v in rows.items() if v["lat"] is None]
    if unmapped:
        print(f"\nNot places (add to {OVERRIDES_PATH.name} to pin them):")
        for u in sorted(unmapped):
            print("   ", u)
    conn.close()


if __name__ == "__main__":
    main()
