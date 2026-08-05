"""Local search UI over the aggregated material database.

Run from the project root:

    uvicorn stonescan.web.app:app --reload

Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, dedupe, geocode, imagesearch, reference, slabs, smartsearch

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Stone Scanner")


@app.on_event("startup")
def _ensure_schema() -> None:
    """User-owned tables (watchlist, lists) must exist even if the shipped DB
    snapshot predates them — connect() alone doesn't create anything."""
    try:
        db.init_db().close()
    except Exception:  # noqa: BLE001 - a read-only DB shouldn't stop the UI opening
        pass
    # Warm the result caches off-thread so the FIRST Search / What's-New view is
    # instant too, not just repeats — without ever delaying the window opening.
    threading.Thread(target=_warm_caches, daemon=True).start()


def _warm_caches() -> None:
    """Precompute the expensive default views (stats, the landing search, fuzzy
    vocabulary, stock changes) so they're already cached when the user first clicks."""
    try:
        conn = db.connect()
    except Exception:  # noqa: BLE001
        return
    try:
        db.stats(conn)
        # Build the rollup if a shipped/older DB predates it, so the fast path is
        # available on first load (until then _search falls back to the live query).
        db.ensure_product_rollup(conn)
        _name_words(conn)
        # Same params the default landing page uses, so the cache key matches its request.
        _search(conn, q="", material_type="", color="", thickness="", supplier="",
                limit=60, offset=0)
        _stock_changes(conn)
    except Exception:  # noqa: BLE001 - warming is best-effort; a cold cache still works
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


@app.on_event("shutdown")
async def _close_browser() -> None:
    await slabs.shutdown()
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _product_url(host: str, item_id, source_url: str) -> str:
    """Best outbound link to the *exact* product on the supplier's own site.

    Only the Stone Profits crawl path stores a bare-root source_url ('https://<host>/'),
    and those tenants deep-link to a product at /InventoryDetail/<ItemID> (verified) — so
    upgrade a bare root to that deep link. Every other provider already stores a real
    product/listing URL in source_url, so pass it straight through."""
    src = (source_url or "").strip()
    if src and src.rstrip("/") != f"https://{host}":
        return src  # a real product/listing link from a non-Stone-Profits provider
    if item_id:
        return f"https://{host}/InventoryDetail/{item_id}"
    return src or f"https://{host}/"


templates.env.globals["product_url"] = _product_url


def _compare_state() -> dict:
    """Compare-tray state for base.html's nav badge + sticky bar and the row checkboxes.
    Registered as a template global so every page shows the tray without each of the ~20
    routes having to pass it in. Opens its own short-lived connection and never raises: a
    DB that predates the compare_tray table (an old snapshot) reads as an empty tray, so
    the nav shows count 0 rather than 500-ing the page."""
    try:
        conn = db.connect()
        try:
            tray = db.get_compare(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        tray = []
    # 'keyset', not 'keys': Jinja attribute access (cmp.keys) would resolve to the dict's
    # built-in .keys method before the item, breaking `key in cmp.keys`.
    return {"tray": tray, "keyset": {t["material_key"] for t in tray}, "count": len(tray)}


templates.env.globals["compare_state"] = _compare_state


def _alert_state() -> dict:
    """Unread catalog-change count for base.html's 🔔 nav badge. A template global (like
    compare_state) so every page shows it without each route passing it in. Opens its own
    short-lived connection and never raises: a DB predating the alert tables reads as all-
    unread (the digest itself needs two crawls, so a fresh DB is simply 0)."""
    try:
        conn = db.connect()
        try:
            return {"unread": _alert_unread_count(conn)}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {"unread": 0}


templates.env.globals["alert_state"] = _alert_state


def _refresh_warning() -> dict:
    """Whether the nightly refresh has quietly stopped running, for base.html's header.

    A template global (like compare_state and alert_state) because the point is to be seen
    without going looking: /health already knows this, and knowing it there is what failed —
    two consecutive nights were lost in August 2026 and nobody noticed until someone opened
    the page on purpose.

    This answers a different question from the freshness line it sits beside. Freshness says
    how much of the catalog is current; this says whether the thing that keeps it current is
    running at all. Both can be bad at once and each is worth its own words.

    Never raises, for the usual reason: it now runs on every page in the app.
    """
    try:
        conn = db.connect()
        try:
            days = db.lost_refresh_days(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        days = 0
    return {"days": days, "warn": days >= db.LOST_REFRESH_WARN_DAYS}


templates.env.globals["refresh_warning"] = _refresh_warning


def _distinct(conn, column: str) -> list[str]:
    # Never offer the accessory/non-slab bucket as a material-type filter — the catalog
    # is stone/tile only (it remains queryable on the Quality audit page).
    extra = " AND material_type <> 'Accessory / Non-Slab'" if column == "material_type" else ""
    rows = conn.execute(
        f"SELECT DISTINCT {column} v FROM materials WHERE {column} <> ''{extra} ORDER BY {column}"
    ).fetchall()
    return [r["v"] for r in rows]


def _colors_matching(conn, base: str) -> list[str]:
    """Distinct color values whose words include the base color (word-level, so
    'blue' matches 'Blue'/'Light Blue'/'Gray, White' but not 'Colored')."""
    bases = {"gray", "grey"} if base in ("gray", "grey") else {base}
    out = []
    for r in conn.execute("SELECT DISTINCT color FROM materials WHERE color <> ''"):
        words = set(re.split(r"[\s,/&-]+", r["color"].lower()))
        if bases & words:
            out.append(r["color"])
    return out


# --- Colour families --------------------------------------------------------------
#
# `color` is free text: 983 distinct values, 527 of them appearing twice or less, and the
# dropdown offered every one while the filter matched on equality. So "Gray" and "Grey"
# were separate choices that each returned part of the family, and a row coloured
# "Gray, White" was unreachable from either — picking Gray missed 6,452 of the 13,290
# rows that carry a gray/grey word.
#
# The families below cover 97% of coloured rows; the rest stay reachable through a
# long-tail bucket, so nothing becomes unfindable.
#
# Folded at QUERY time rather than stored canonically: the raw supplier value is what the
# item page shows and what a re-crawl rewrites, so rewriting it would either be undone
# every crawl or lose what the supplier actually said. It also means a colour saved in a
# watchlist before this existed still resolves — see _colors_for_choice.
_COLOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "White": ("white",), "Black": ("black",), "Gray": ("gray", "grey"),
    "Blue": ("blue",), "Beige": ("beige",), "Brown": ("brown",),
    "Gold": ("gold", "golden"), "Cream": ("cream", "ivory"), "Green": ("green",),
    "Silver": ("silver",), "Tan": ("tan",), "Multi": ("multi", "multicolor", "multicolour"),
    "Yellow": ("yellow",), "Red": ("red",), "Taupe": ("taupe",), "Pink": ("pink",),
    "Orange": ("orange",), "Purple": ("purple", "violet", "lavender"),
    "Bronze": ("bronze",), "Charcoal": ("charcoal",),
}
_COLOR_OTHER = "Other"
# Splits on ANY non-alphanumeric run, not just the separators the parsed-NL branch uses:
# suppliers write "White." and "Off-White" as well as "Gray, White", and a trailing full
# stop was enough to hide 37 rows from the White family.
_COLOR_WORD_RE = re.compile(r"[^a-z0-9]+")


def _color_words(value: str) -> set[str]:
    return {w for w in _COLOR_WORD_RE.split((value or "").lower()) if w}


def _family_of(value: str) -> str:
    """The first family a raw colour value belongs to, or '' for the long tail.
    A multi-colour value ("Gray, White") belongs to several; it is reachable from each."""
    words = _color_words(value)
    for fam, syns in _COLOR_FAMILIES.items():
        if words & set(syns):
            return fam
    return ""


def _color_options(conn) -> list[str]:
    """Families actually present in the catalog, most-used first, then the long tail."""
    seen: dict[str, int] = {}
    tail = 0
    for r in conn.execute(
            "SELECT color, COUNT(*) n FROM materials WHERE color <> '' GROUP BY color"):
        words, hit = _color_words(r["color"]), False
        for fam, syns in _COLOR_FAMILIES.items():
            if words & set(syns):
                seen[fam] = seen.get(fam, 0) + r["n"]
                hit = True
        if not hit:
            tail += r["n"]
    out = sorted(seen, key=lambda f: -seen[f])
    if tail:
        out.append(_COLOR_OTHER)
    return out


def _colors_for_choice(conn, choice: str) -> list[str]:
    """The raw colour values a dropdown choice stands for.

    A family expands to every value carrying one of its words. `Other` is everything in
    no family. Anything else — a raw value saved in a watchlist before families existed —
    is carried forward into its family if it has one, so an old saved search widens rather
    than silently stops matching; a value with no family still matches itself.
    """
    if not choice:
        return []
    if choice not in _COLOR_FAMILIES and choice != _COLOR_OTHER:
        fam = _family_of(choice)
        if not fam:
            return [choice]
        choice = fam
    syns = set(_COLOR_FAMILIES.get(choice, ()))
    out = []
    for r in conn.execute("SELECT DISTINCT color FROM materials WHERE color <> ''"):
        words = _color_words(r["color"])
        in_family = bool(words & syns)
        if (choice == _COLOR_OTHER and not _family_of(r["color"])) or in_family:
            out.append(r["color"])
    return out


_NAME_WORDS: list[str] | None = None


def _name_words(conn) -> list[str]:
    """Every distinct word used in a material name, cached for the process.
    SQLite has no fuzzy matching, so near-miss recall is done in Python."""
    global _NAME_WORDS
    if _NAME_WORDS is None:
        words: set[str] = set()
        for r in conn.execute("SELECT DISTINCT name_norm FROM materials WHERE name_norm <> ''"):
            for w in re.split(r"[^A-Z0-9]+", r["name_norm"]):
                if len(w) > 3 and not w.isdigit():
                    words.add(w)
        _NAME_WORDS = sorted(words)
    return _NAME_WORDS


def _close_words(conn, term: str) -> list[str]:
    """Catalog words within a typo's distance of `term` ('calacata' -> CALACATTA)."""
    import difflib
    return difflib.get_close_matches(term.upper(), _name_words(conn), n=4, cutoff=0.82)


# Total in-stock area of a grouped product, in square feet (dims are inches).
# available_slabs is a genuine slab count ONLY for slab listings. For tiles the crawlers
# put a square-foot (or piece) quantity in that column, so a tile must never be summed as
# a "slab". product_form is the reliable signal (uom=SF also marks ~11k real slabs, so it
# can't be used). _SLABS = slab units only; _TILE_SF = the tile quantity, shown as area.
_IS_TILE = "UPPER(COALESCE(m.product_form,'')) LIKE '%TILE%'"
_SLABS = f"SUM(CASE WHEN {_IS_TILE} THEN 0 ELSE COALESCE(m.available_slabs,0) END)"
# Tile area only where the quantity is genuinely square feet (uom=SF). Other tile rows
# carry a piece/placeholder count that isn't an area, so don't label them "ft²"; a plain
# has-tile flag still lets such tile-only materials count as in-stock and read as "tile".
_TILE_SF = (f"SUM(CASE WHEN {_IS_TILE} AND UPPER(COALESCE(m.uom,'')) = 'SF' "
            "THEN COALESCE(m.available_slabs,0) ELSE 0 END)")
_HAS_TILE = f"MAX(CASE WHEN {_IS_TILE} THEN 1 ELSE 0 END)"

# Same tile-vs-slab rule for queries over the bare materials table (no "m." alias),
# used by the per-supplier breakdown on /item. Without the guard, a tile's
# square-foot quantity prints as a slab count under a "Slabs" column.
_IS_TILE_RAW = "UPPER(COALESCE(product_form,'')) LIKE '%TILE%'"
_SLABS_RAW = f"SUM(CASE WHEN {_IS_TILE_RAW} THEN 0 ELSE COALESCE(available_slabs,0) END)"
_TILE_SF_RAW = (f"SUM(CASE WHEN {_IS_TILE_RAW} AND UPPER(COALESCE(uom,'')) = 'SF' "
                "THEN COALESCE(available_slabs,0) ELSE 0 END)")
_HAS_TILE_RAW = f"MAX(CASE WHEN {_IS_TILE_RAW} THEN 1 ELSE 0 END)"

# Card image: prefer a real product photo over a SlabCloud thumbnail. SlabCloud tenants
# reuse a shared "coming soon" graphic (in many byte-variants) for un-photographed slabs,
# so whenever a material also has a Stone Profits photo, that real photo should win; the
# SlabCloud thumb is used only when it's the material's sole image. (The search query
# inlines this two-tier pick per supplier via real_img/any_img, so no shared constant.)

# Total in-stock SLAB area. Sum each slab listing's own slabs×length×width BEFORE
# aggregating (a group spans suppliers/variants, so MAX(length)×MAX(width) would
# cross-multiply dims from different slabs); tiles contribute no slab area.
_SQFT = (f"SUM(CASE WHEN {_IS_TILE} THEN 0 ELSE COALESCE(m.available_slabs,0) "
         "* COALESCE(m.avg_length,0) * COALESCE(m.avg_width,0) END) / 144.0")

# ORDER BY runs on the OUTER (per-material) query, so these reference its output
# columns. `available_slabs` is the deepest single yard (max per supplier), which is
# also the number shown — so "most slabs" ranks by the same figure the user reads.
SORTS = {
    # Material-centric relevance: surface the materials the market actually carries
    # widely (many suppliers, deep single-yard stock) before one-off/granular SKUs.
    "relevance": "suppliers DESC, available_slabs DESC, material_key",
    "slabs": "available_slabs DESC, material_key",
    "size": "avg_length DESC, material_key",
    "new": "new_arrival DESC, material_key",
    "area": "total_sqft DESC, material_key",
    "distance": "miles ASC, available_slabs DESC",  # only valid when near-active
}


def _register_nearest(conn, loc_miles: dict[str, float]):
    """Expose a SQL function mapping a material's comma-joined `locations` string to
    the distance of its nearest in-range stocking location (NULL if none)."""
    def nearest(csv):
        if not csv:
            return None
        best = None
        for part in csv.split(","):
            d = loc_miles.get(part.strip().lower())
            if d is not None and (best is None or d < best):
                best = d
        return best
    conn.create_function("nearest_miles", 1, nearest, deterministic=True)


# Search / What's-New results change only on a crawl, but the search view is a two-level
# GROUP BY over ~130k materials that was recomputed on every tab click. A short TTL cache
# makes repeat loads (especially the default Search tab) instant; a crawl shows within TTL.
_search_cache: dict = {}
_stock_cache: dict = {}
_quality_cache: dict = {}
_alert_cache: dict = {}
_RESULT_TTL = 60.0


def _invalidate_caches() -> None:
    """Drop the page-result caches after any data mutation (a crawl, or a merge/reject
    that changes cross-supplier grouping) so the UI reflects it immediately."""
    global _NAME_WORDS
    _search_cache.clear()
    _stock_cache.clear()
    _quality_cache.clear()
    _alert_cache.clear()  # a crawl adds a snapshot -> new change events / badge count
    _NAME_WORDS = None  # the fuzzy-match vocabulary is derived from the catalog too
    db._stats_cache.clear()
    db._residue_cache.clear()   # which suppliers serve residue changes only on a crawl


def _db_path(conn) -> str:
    """The connection's main DB file, so the result caches key PER database and never
    serve one DB's results for another (e.g. across tests' temporary DBs)."""
    try:
        return (conn.execute("PRAGMA database_list").fetchone() or ("", "", ""))[2] or ""
    except Exception:  # noqa: BLE001
        return ""


def _fts_match(terms) -> str:
    """Build an FTS5 prefix-AND query from parsed name terms: every term must appear as a
    word or word-prefix (implicit AND). Sanitized to bare tokens so the MATCH can never be
    a syntax error; returns '' when nothing usable is left, so the caller uses the live path."""
    toks = [re.sub(r"[^a-z0-9]", "", (t or "").lower()) for t in terms]
    return " ".join(f"{t}*" for t in toks if t)


def _search(conn, *, q, material_type, color, thickness, supplier, limit, offset,
            location="", min_length=0.0, min_width=0.0, new_only=False, finish="",
            sort="relevance", in_stock=False, fuzzy=True,
            near=None, radius_mi=0.0, min_sqft=0.0):
    """Free-text-aware search. The query box understands phrases like
    'blue marble slabs'; explicit dropdown filters take precedence."""
    _ck = (_db_path(conn), q, material_type, color, thickness, supplier, location,
           min_length, min_width, new_only, sort, in_stock, fuzzy, near, radius_mi,
           min_sqft, finish, limit, offset)
    _h = _search_cache.get(_ck)
    if _h and time.time() - _h[0] < _RESULT_TTL:
        return _h[1]
    parsed = smartsearch.parse_query(q)

    # Fast path: the default browse and the material-type facet — plus in_stock /
    # min_sqft / sort / pagination — are all PRODUCT-level (no filter changes a
    # product's cross-supplier aggregates), so they read the precomputed
    # product_rollup instead of the two-level GROUP BY over every material. Anything
    # that filters at the ROW level (q, color, thickness, supplier, location, near,
    # min_length/width, new_only) still runs the live query below, and an unbuilt or
    # empty rollup (e.g. a fresh seed DB) also falls through. Results are identical to
    # the live path for the cases handled here — see ProductRollupTests.
    # `finish` is a ROW-level filter and product_rollup holds no finish column, so the
    # rollup path would silently return unfiltered results for it (AC-3).
    fast_ok = (not (q or "").strip() and not color and not thickness and not supplier
               and not location and not near and not min_length and not min_width
               and not new_only and not finish)
    if fast_ok and conn.execute("SELECT 1 FROM product_rollup LIMIT 1").fetchone():
        rw, rp = ["1=1"], []
        if material_type:
            rw.append("material_type = ?"); rp.append(material_type)
        if material_type != "Accessory / Non-Slab":
            rw.append("material_type <> 'Accessory / Non-Slab'")
        if in_stock or min_sqft:
            rw.append("(total_slabs > 0 OR has_tile = 1)")
        if min_sqft:
            rw.append("total_sqft >= ?"); rp.append(min_sqft)
        rclause = " AND ".join(rw)
        rsort = "relevance" if sort == "distance" else sort  # no proximity on this path
        rorder = SORTS.get(rsort, SORTS["relevance"])
        r_total = conn.execute(
            f"SELECT COUNT(*) c FROM product_rollup WHERE {rclause}", rp).fetchone()["c"]
        r_rows = conn.execute(
            f"""SELECT id, material_key, item_name, material_type, color, new_arrival,
                       suppliers, available_slabs, total_slabs, tile_sf, has_tile,
                       avg_length, avg_width, total_sqft, image_url
                FROM product_rollup WHERE {rclause}
                ORDER BY {rorder} LIMIT ? OFFSET ?""",
            (*rp, limit, offset)).fetchall()
        _result = (r_total, [dict(r) for r in r_rows], parsed)
        _search_cache[_ck] = (time.time(), _result)
        if len(_search_cache) > 64:
            _search_cache.pop(min(_search_cache, key=lambda k: _search_cache[k][0]), None)
        return _result

    # FTS5 fast path: a free-text NAME query (q terms) with no row-filter / color /
    # location / proximity resolves candidate products from the FTS5 name index and
    # aggregates them via product_rollup — replacing the per-row LIKE scan. It hands back
    # to the live query when: FTS5 is absent/empty; the query carries a parsed color /
    # thickness / form (which the live path matches specially); or the match is low-recall
    # (< 5 hits), where the live LIKE + fuzzy near-miss recall may do better. So q never
    # returns worse-than-today results (see FtsSearchTests). Word/prefix matching means the
    # result SET may differ from the old substring LIKE — better for whole-word/prefix.
    fts_ok = (parsed["terms"] and not color and not parsed["color"]
              and not thickness and not parsed["thickness"] and not parsed["form"]
              and not supplier and not location and not near
              and not min_length and not min_width and not new_only and not finish)
    if fts_ok and db._has_fts(conn) and conn.execute("SELECT 1 FROM product_fts LIMIT 1").fetchone():
        match = _fts_match(parsed["terms"])
        if match:
            fw = ["grp IN (SELECT grp FROM product_fts WHERE product_fts MATCH ?)"]
            fp = [match]
            mt = material_type or parsed["material_type"]
            if mt:
                fw.append("material_type = ?"); fp.append(mt)
            if mt != "Accessory / Non-Slab":
                fw.append("material_type <> 'Accessory / Non-Slab'")
            if in_stock or min_sqft:
                fw.append("(total_slabs > 0 OR has_tile = 1)")
            if min_sqft:
                fw.append("total_sqft >= ?"); fp.append(min_sqft)
            fclause = " AND ".join(fw)
            fsort = "relevance" if sort == "distance" else sort
            forder = SORTS.get(fsort, SORTS["relevance"])
            f_total = conn.execute(
                f"SELECT COUNT(*) c FROM product_rollup WHERE {fclause}", fp).fetchone()["c"]
            if f_total >= 5:  # high recall: trust FTS5; else fall through for LIKE + fuzzy
                f_rows = conn.execute(
                    f"""SELECT id, material_key, item_name, material_type, color, new_arrival,
                               suppliers, available_slabs, total_slabs, tile_sf, has_tile,
                               avg_length, avg_width, total_sqft, image_url
                        FROM product_rollup WHERE {fclause}
                        ORDER BY {forder} LIMIT ? OFFSET ?""",
                    (*fp, limit, offset)).fetchall()
                _result = (f_total, [dict(r) for r in f_rows], parsed)
                _search_cache[_ck] = (time.time(), _result)
                if len(_search_cache) > 64:
                    _search_cache.pop(min(_search_cache, key=lambda k: _search_cache[k][0]), None)
                return _result

    where, params = ["1=1"], []

    mt = material_type or parsed["material_type"]
    if mt:
        where.append("m.material_type = ?"); params.append(mt)

    # The catalog is stone/tile materials only — never surface the accessory/non-slab
    # bucket (sinks, tools, chemicals, PPE). It stays in the DB for the Quality audit.
    if mt != "Accessory / Non-Slab":
        where.append("m.material_type <> 'Accessory / Non-Slab'")

    if color:  # explicit dropdown -> the whole colour family, not one spelling
        fam = _colors_for_choice(conn, color)
        where.append("m.color IN (%s)" % ",".join("?" * len(fam)) if fam else "1=0")
        params.extend(fam)
    elif parsed["color"]:  # parsed -> lenient (color field OR the name)
        base = smartsearch.color_base(parsed["color"])
        cols = _colors_matching(conn, base)
        cond = ["m.name_norm LIKE ?"]
        cparams = [f"%{base.upper()}%"]
        if cols:
            cond.insert(0, f"m.color IN ({','.join('?' for _ in cols)})")
            cparams = cols + cparams
        where.append("(" + " OR ".join(cond) + ")"); params.extend(cparams)

    thk = thickness or parsed["thickness"]
    if thk:
        where.append("m.thickness = ?"); params.append(thk)

    if finish:
        where.append("m.finish = ?"); params.append(finish)

    if supplier:
        where.append("(s.company = ? OR s.host = ?)"); params.extend([supplier, supplier])

    if min_length:
        where.append("m.avg_length >= ?"); params.append(min_length)
    if min_width:
        where.append("m.avg_width >= ?"); params.append(min_width)
    if new_only:
        where.append("m.new_arrival = 1")

    # Location filters are kept SEPARATE from the other clauses so the coverage
    # survey below can ask "what matched everything except location". They also
    # fall back to the SUPPLIER's known yards when the material has no slab-level
    # location of its own: slab locations only exist after a deep (--slabs) refresh,
    # so without the fallback these filters silently dropped 41% of the catalog —
    # including entire suppliers — with no hint anything was missing.
    _LOC = "COALESCE(NULLIF(m.locations,''), s.locations)"
    loc_where, loc_params = [], []
    if location:
        # Whole-token, not substring. Both location columns are GROUP_CONCAT lists, so
        # wrapping the column in commas and matching ',<value>,' pins the value to a
        # complete entry. A bare LIKE '%ES%' hit the "es" inside Charl-es-ton and put
        # 23.6% of the catalog under a two-letter yard code. The delimiters also keep a
        # value that contains a comma ("Atlanta, GA") intact, since the whole thing still
        # sits between them.
        members = db.location_members(conn, location)
        loc_where.append("(" + " OR ".join([f"(',' || {_LOC} || ',') LIKE ?"] * len(members)) + ")")
        loc_params.extend(f"%,{m},%" for m in members)

    # Proximity: keep only material with a stocking location inside the radius.
    near_active = bool(near) and radius_mi > 0
    if near_active:
        loc_miles = geocode.locations_within(conn, near[0], near[1], radius_mi)
        _register_nearest(conn, loc_miles)
        loc_where.append(f"nearest_miles({_LOC}) IS NOT NULL")

    form = parsed["form"]
    if form == "slab":
        where.append("m.product_form LIKE '%SLAB%'")
    elif form == "tile":
        where.append("(m.product_form LIKE '%TILE%' OR m.name_norm LIKE '%TILE%')")
    elif form == "remnant":
        where.append("(m.product_form LIKE '%REMNANT%' OR m.name_norm LIKE '%REMNANT%')")

    def term_sql(terms_variants):
        """AND together the name terms; each term may match any of its variants."""
        sql, ps = [], []
        for variants in terms_variants:
            sql.append("(" + " OR ".join("m.name_norm LIKE ?" for _ in variants) + ")")
            ps.extend(f"%{v.upper()}%" for v in variants)
        return sql, ps

    base_where, base_params = list(where), list(params)
    tw, tp = term_sql([[t] for t in parsed["terms"]])
    sel_tw, sel_tp = tw, tp  # the term clauses actually in effect (fuzzy may swap)
    where = base_where + loc_where + tw
    params = base_params + loc_params + tp

    # Group ACROSS suppliers: one row per material (its cross-supplier material_key),
    # so "Calacatta Gold" is a single card carrying a supplier count + a total slab
    # count — not one row per supplier/variant. The handful of empty-key materials
    # stay individual (grouped on their own id) rather than collapsing together.
    group_by = ("CASE WHEN COALESCE(m.material_key, '') = '' "
                "THEN 'id:' || m.id ELSE m.material_key END")

    # Two-level aggregation. The INNER query sums each supplier's stock for a material
    # (grp, supplier_id); the OUTER query then aggregates ACROSS suppliers. This is
    # what lets "slabs" mean the deepest single yard — MAX of the per-supplier sums —
    # instead of a cross-supplier SUM that reads as one buyable pile when it is really
    # N separate yards. The per-supplier tile guard rides along for free.
    sup_miles = (f", MIN(nearest_miles({_LOC})) AS sup_miles" if near_active else "")

    def inner_sql(clause: str) -> str:
        return f"""
            SELECT {group_by} AS grp, m.supplier_id AS sid,
                   MIN(m.id) AS id, MAX(m.material_key) AS material_key,
                   MIN(m.item_name) AS item_name, MAX(m.material_type) AS material_type,
                   MAX(m.color) AS color, MAX(m.new_arrival) AS new_arrival,
                   MAX(m.avg_length) AS avg_length, MAX(m.avg_width) AS avg_width,
                   {_SLABS} AS sup_slabs, {_TILE_SF} AS sup_tile_sf, {_HAS_TILE} AS sup_has_tile,
                   {_SQFT} AS sup_sqft,
                   MAX(CASE WHEN COALESCE(m.image_url,'') <> ''
                            AND m.image_url NOT LIKE '%slabcloud.com/slabs/%'
                            THEN m.image_url END) AS real_img,
                   MAX(m.image_url) AS any_img{sup_miles}
            FROM materials m JOIN suppliers s ON s.id = m.supplier_id
            WHERE {clause}
            GROUP BY grp, m.supplier_id
        """

    # HAVING now filters the per-material rollup (outer), so it references the inner
    # per-supplier columns. in_stock: stocked at any yard. min_sqft: a SINGLE yard must
    # hold the area — 40 ft² at each of ten yards is not a 400 ft² order.
    have = []
    if in_stock or min_sqft:
        have.append("(SUM(sup_slabs) > 0 OR MAX(sup_has_tile) = 1)")
    if min_sqft:
        have.append("MAX(sup_sqft) >= ?")
    having = (" HAVING " + " AND ".join(have)) if have else ""
    having_params = [min_sqft] if min_sqft else []

    if sort == "distance" and not near_active:
        sort = "relevance"  # distance is meaningless without an origin
    order_by = SORTS.get(sort, SORTS["relevance"])

    def count(clause, ps):
        return conn.execute(
            f"SELECT COUNT(*) c FROM (SELECT 1 FROM ({inner_sql(clause)}) "
            f"GROUP BY grp{having})",
            ps + having_params,
        ).fetchone()["c"]

    clause = " AND ".join(where)
    total = count(clause, params)

    # Near-miss recall: an unknown spelling matches nothing at all in SQL, so when
    # a name query comes up (nearly) empty, retry OR-ing in close catalog words.
    if fuzzy and parsed["terms"] and total < 5:
        variants = [[t] + [w for w in _close_words(conn, t) if w.upper() != t.upper()]
                    for t in parsed["terms"]]
        if any(len(v) > 1 for v in variants):
            tw, tp = term_sql(variants)
            fclause = " AND ".join(base_where + loc_where + tw)
            fparams = base_params + loc_params + tp
            ftotal = count(fclause, fparams)
            if ftotal > total:
                clause, params, total = fclause, fparams, ftotal
                sel_tw, sel_tp = tw, tp
                parsed["fuzzy"] = sorted({w for v in variants for w in v[1:]})

    # Coverage survey: when a location filter is active, say out loud what it CANNOT
    # see. Suppliers with no cached slab galleries have no location data at all, so
    # their materials fail every location clause — before this, a "within 150 mi"
    # search would confidently report N results while silently omitting the largest
    # supplier's entire catalog. The count of unplaceable matches is surfaced as a
    # chip beside the results instead of pretending the filter covered everything.
    if loc_where:
        sq = " AND ".join(
            base_where + sel_tw
            + ["COALESCE(NULLIF(m.locations,''), NULLIF(s.locations,''), '') = ''"])
        r = conn.execute(
            f"""SELECT COUNT(DISTINCT m.supplier_id) AS sups,
                       COUNT(DISTINCT CASE WHEN COALESCE(m.material_key,'') = ''
                             THEN 'id:' || m.id ELSE m.material_key END) AS mats
                FROM materials m JOIN suppliers s ON s.id = m.supplier_id
                WHERE {sq}""",
            base_params + sel_tp,
        ).fetchone()
        if r["sups"]:
            parsed["unlocated"] = {"suppliers": r["sups"], "materials": r["mats"]}

    # Outer rollup across suppliers. available_slabs = deepest single yard (MAX of the
    # per-supplier sums); total_slabs keeps the grand total for anything that wants it.
    # total_sqft is likewise the deepest yard's area, so it reads consistently with the
    # slab figure beside it. miles = distance to the NEAREST stocking supplier.
    miles_col = ", MIN(sup_miles) AS miles" if near_active else ""
    rows = conn.execute(
        f"""SELECT MIN(id) AS id, COALESCE(MAX(material_key), '') AS material_key,
                   MIN(item_name) AS item_name, MAX(material_type) AS material_type,
                   MAX(color) AS color, MAX(new_arrival) AS new_arrival,
                   COUNT(*) AS suppliers,
                   MAX(sup_slabs) AS available_slabs, SUM(sup_slabs) AS total_slabs,
                   SUM(sup_tile_sf) AS tile_sf, MAX(sup_has_tile) AS has_tile,
                   MAX(avg_length) AS avg_length, MAX(avg_width) AS avg_width,
                   MAX(sup_sqft) AS total_sqft,
                   COALESCE(MAX(real_img), MAX(any_img)) AS image_url{miles_col}
            FROM ({inner_sql(clause)})
            GROUP BY grp{having}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?""",
        (*params, *having_params, limit, offset),
    ).fetchall()
    _result = (total, [dict(r) for r in rows], parsed)
    _search_cache[_ck] = (time.time(), _result)
    if len(_search_cache) > 64:  # bound: evict the oldest entry
        _search_cache.pop(min(_search_cache, key=lambda k: _search_cache[k][0]), None)
    return _result


def _base_name(material_key) -> str:
    """Trade-name segment of a material_key ('river white|granite' -> 'river white')."""
    return (material_key or "").split("|")[0]


def _representative_photos(conn, bases) -> dict:
    """One real catalog photo per trade name (the most-stocked listing's image) for the
    given base names — a labeled 'representative' likeness for materials that have no
    photo of their own. Only specific (multi-word) names are looked up: a single generic
    word like 'white' would borrow an unrelated stone."""
    bases = {b for b in bases if b and len(b.split()) >= 2}
    if not bases:
        return {}
    # Match each base as a material_key range [<base>|, <base>}) so idx_mat_key is used —
    # a computed "base-of-key IN (...)" would full-scan the whole materials table.
    conds, params = [], []
    for b in bases:
        conds.append("(material_key >= ? AND material_key < ?)")
        params += [b + "|", b + "}"]
    best: dict[str, tuple] = {}
    for r in conn.execute(
        f"""SELECT material_key, image_url, available_slabs FROM materials
            WHERE ({' OR '.join(conds)})
                  AND material_type <> 'Accessory / Non-Slab'
                  AND COALESCE(image_url,'') <> ''""",
        params,
    ):
        b = _base_name(r["material_key"])
        slabs = r["available_slabs"] or 0
        if b not in best or slabs > best[b][1]:
            best[b] = (r["image_url"], slabs)
    return {b: v[0] for b, v in best.items()}


def _attach_rep_photos(conn, rows) -> None:
    """Set row['rep_image'] on any result row lacking a photo of its own, when another
    material with the same (multi-word) trade name has a real catalog photo to stand in."""
    reps = _representative_photos(conn, [_base_name(r.get("material_key")) for r in rows
                                         if not r.get("image_url")])
    for r in rows:
        if not r.get("image_url") and _base_name(r.get("material_key")) in reps:
            r["rep_image"] = reps[_base_name(r.get("material_key"))]


def _resolve_near(near: str):
    """Resolve a typed origin ('Dallas', 'Charlotte NC') to (lat, lon), offline.
    Returns ((lat, lon)|None, label). Same city dataset the map uses, so an origin
    it can't place (a bare zip, a tiny town) comes back unresolved with a note."""
    near = (near or "").strip()
    if not near:
        return None, ""
    hit = geocode.resolve(near)
    if hit:
        return (hit["lat"], hit["lon"]), hit.get("label") or near
    return None, near


def _here(request: Request) -> str:
    """This page as a path+query, minus the add-to-list feedback params."""
    url = request.url.remove_query_params(["added", "list"])
    return url.path + (f"?{url.query}" if url.query else "")


def _to_float(v) -> float:
    """Parse a query value to float; blank/invalid -> 0 (form sends '' when empty)."""
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _filter_qs(request: Request, drop: tuple[str, ...] = ("page", "view", "added", "list")) -> str:
    """Rebuild the query string from the request itself, minus transient keys, so a
    pager or view-toggle link carries EVERY active filter forward — including any filter
    param added to the route later, which a hand-built string would silently drop."""
    from urllib.parse import urlencode
    return urlencode([(k, v) for k, v in request.query_params.multi_items()
                      if k not in drop and v != ""])


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    material_type: str = "",
    color: str = "",
    thickness: str = "",
    finish: str = "",
    supplier: str = "",
    location: str = "",
    min_length: str = "",
    min_width: str = "",
    new_only: int = 0,
    in_stock: int = 0,
    sort: str = "relevance",
    near: str = "",
    radius: str = "",
    min_sqft: str = "",
    view: str = "grid",
    page: int = 1,
    added: int = -1,
    list: int = 0,
):
    conn = db.connect()
    view = view if view in ("table", "grid") else "grid"
    sort = sort if sort in SORTS else "relevance"
    limit = 60
    offset = max(page - 1, 0) * limit
    ml, mw = _to_float(min_length), _to_float(min_width)
    # Clamp to a positive radius: a 0/blank/negative value falls back to the default,
    # so near_ok (origin resolved) can't diverge from near_active (radius > 0) and leave
    # the template rendering a miles column the query never selected.
    radius_mi = _to_float(radius)
    radius_mi = radius_mi if radius_mi > 0 else 150.0
    origin, near_label = _resolve_near(near)
    total, rows, parsed = _search(
        conn, q=q, material_type=material_type, color=color,
        thickness=thickness, finish=finish, supplier=supplier, location=location,
        min_length=ml, min_width=mw, new_only=bool(new_only),
        sort=sort, in_stock=bool(in_stock),
        near=origin, radius_mi=radius_mi if origin else 0.0,
        min_sqft=_to_float(min_sqft),
        limit=limit, offset=offset,
    )
    _attach_rep_photos(conn, rows)
    # Mark rows drawing on a supplier that is serving residue. Applied here, after _search
    # returns, so it holds for all three search paths (rollup, FTS, live) — a column on
    # product_rollup would only have covered the first.
    #
    # Skipped once a filter narrows which suppliers contribute: the row then aggregates a
    # subset, while the residue lookup still sees the whole catalog, so the chip would
    # accuse a row whose visible contributors are all current. Filter to one healthy
    # supplier and the warning must not follow you there.
    _narrowed = db.narrows_contributors(
        supplier=supplier, location=location, near=origin, in_stock=in_stock,
        min_length=min_length, min_width=min_width, min_sqft=min_sqft)
    _stale = set() if _narrowed else db.groups_with_residue(conn, rows)
    for _r in rows:
        _r["has_residue"] = (_r.get("material_key") or f"id:{_r.get('id')}") in _stale
    ctx = {
        "request": request,
        "rows": rows,
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit,
        "q": q,
        "interpreted": smartsearch.summary(parsed) if q else [],
        "material_type": material_type,
        "color": color,
        "thickness": thickness,
        "finish": finish,
        "supplier": supplier,
        "location": location,
        "min_length": min_length or "",
        "min_width": min_width or "",
        "new_only": new_only,
        "in_stock": in_stock,
        "sort": sort,
        "near": near,
        "near_label": near_label,
        "near_ok": bool(origin),
        # Matches the other filters but has no location data — shown, not hidden.
        "unlocated": parsed.get("unlocated"),
        "radius": radius or (str(int(radius_mi)) if near else ""),
        "min_sqft": min_sqft or "",
        "view": view,
        # Every active filter, ready to prefix onto pager / view-toggle links.
        "qs": _filter_qs(request),
        "lists": db.get_lists(conn),
        "added": added,
        "added_list": list,
        # Where an add-to-list POST should send the user back to (path only, so the
        # redirect stays on this host).
        "here": _here(request),
        "types": _distinct(conn, "material_type"),
        "colors": _color_options(conn),
        "thicknesses": _distinct(conn, "thickness"),
        "finishes": _distinct(conn, "finish"),
        "locations": db.location_options(conn),
        "suppliers": [
            dict(r) for r in conn.execute(
                "SELECT COALESCE(NULLIF(company,''),host) AS name, item_count "
                # Exclude mirror storefronts so the same yard isn't offered as two suppliers.
                "FROM suppliers WHERE item_count > 0 AND mirror_of IS NULL ORDER BY name"
            ).fetchall()
        ],
        "stats": db.stats(conn),
    }
    conn.close()
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/material", response_class=HTMLResponse)
def material(request: Request, key: str, added: int = -1, list: int = 0):
    """Canonical page for a material: the whole market for it in one view —
    photos, spec ranges, every supplier that carries it, and similar stones."""
    conn = db.connect()
    rows = [dict(r) for r in conn.execute(
        """SELECT m.*, COALESCE(NULLIF(s.company,''), s.host) AS supplier_name,
                  s.host AS supplier_host, s.phone AS supplier_phone, s.email AS supplier_email
           FROM materials m JOIN suppliers s ON s.id = m.supplier_id
           WHERE m.material_key = ?
           ORDER BY supplier_name, m.thickness""",
        (key,),
    )]
    if not rows:
        conn.close()
        return HTMLResponse("<p style='padding:40px;font-family:sans-serif'>Unknown material. "
                            "<a href='/'>Back to search</a></p>", status_code=404)

    # The display name is the most common spelling suppliers use for it.
    names: dict[str, int] = {}
    for r in rows:
        names[r["item_name"]] = names.get(r["item_name"], 0) + 1
    name = max(names, key=lambda n: names[n])

    # One block per supplier: their listings collapsed, best photo, stock, contact.
    by_supplier: dict[str, dict] = {}
    for r in rows:
        s = by_supplier.setdefault(r["supplier_name"], {
            "supplier_name": r["supplier_name"], "supplier_host": r["supplier_host"],
            "phone": r["supplier_phone"], "email": r["supplier_email"],
            "slabs": 0, "tile_sf": 0, "has_tile": False,
            "image_url": "", "id": r["id"], "item_id": r["item_id"],
            # Each platform has its own public URL shape, so carry the one the
            # crawler actually saw rather than reconstructing it.
            "source_url": r["source_url"] or "",
            "listings": [], "locations": set(),
        })
        # available_slabs is a slab count only for slab listings; for tiles it's a square
        # foot (uom=SF) or piece/placeholder quantity — surface real areas as tile area,
        # everything else just as "tile", never as slabs.
        if "TILE" in (r["product_form"] or "").upper():
            s["has_tile"] = True
            if (r["uom"] or "").upper() == "SF":
                s["tile_sf"] += r["available_slabs"] or 0
        else:
            s["slabs"] += r["available_slabs"] or 0
        if not s["image_url"] and r["image_url"]:
            # Keep the outbound link on the same listing as the photo we show.
            s["image_url"], s["id"], s["item_id"] = r["image_url"], r["id"], r["item_id"]
            s["source_url"] = r["source_url"] or s["source_url"]
        for loc in (r["locations"] or "").split(","):
            if loc.strip():
                s["locations"].add(loc.strip())
        s["listings"].append(r)
    suppliers_list = sorted(by_supplier.values(), key=lambda s: (-s["slabs"], s["supplier_name"]))
    for s in suppliers_list:
        s["locations"] = sorted(s["locations"])

    def uniq(field):
        return sorted({(r[field] or "").strip() for r in rows if (r[field] or "").strip()})

    def clean_colors():
        """Colors aggregated over dozens of suppliers, some of whom paste a whole
        product description into the color field — keep only real color words."""
        out: set[str] = set()
        for r in rows:
            for part in (r["color"] or "").split(","):
                part = part.strip()
                if part and len(part) <= 18 and len(part.split()) <= 2 \
                        and not re.search(r"\d", part):
                    out.add(part.title())
        return sorted(out)[:8]

    def sorted_thicknesses():
        """'1.2cm','2cm','10cm' sort numerically, not as strings."""
        def cm(t):
            m = re.match(r"([\d.]+)", t)
            return float(m.group(1)) if m else 0.0
        return sorted(uniq("thickness"), key=cm)

    lengths = [r["avg_length"] for r in rows if r["avg_length"]]
    widths = [r["avg_width"] for r in rows if r["avg_width"]]
    facts = {
        "types": uniq("material_type"), "colors": clean_colors(),
        "finishes": uniq("finish"), "thicknesses": sorted_thicknesses(),
        "origins": uniq("origin"), "forms": uniq("product_form"),
        # Headline stock is the DEEPEST SINGLE YARD, not a cross-supplier sum: this
        # material spans up to dozens of yards, and "2,240 slabs" read as one pile is
        # a number you can't actually buy. suppliers_list is already sorted -slabs, so
        # its first entry is the deepest. The grand total and the yard's name ride
        # along for an honest "largest of N" caption.
        "slabs": max((s["slabs"] for s in suppliers_list), default=0),
        "total_slabs": sum(s["slabs"] for s in suppliers_list),
        "top_yard": next((s["supplier_name"] for s in suppliers_list if s["slabs"]), ""),
        "tile_sf": max((s["tile_sf"] for s in suppliers_list), default=0),
        "has_tile": any(s["has_tile"] for s in suppliers_list),
        "suppliers": len(suppliers_list),
        "listings": len(rows),
        "size_range": (
            f"{min(lengths):.0f}×{min(widths):.0f} – {max(lengths):.0f}×{max(widths):.0f}"
            if lengths and widths else ""
        ),
        "new": any(r["new_arrival"] for r in rows),
    }
    # Lead the hero strip with real product photos; SlabCloud thumbnails (which include a
    # shared "coming soon" placeholder for un-photographed slabs) sort last.
    photos = [*dict.fromkeys(
        sorted((r["image_url"] for r in rows if r["image_url"]),
               key=lambda u: "slabcloud.com/slabs/" in u))][:8]
    # No photo of its own? Borrow a labeled representative from the same trade name.
    rep_photo = "" if photos else _representative_photos(conn, [_base_name(key)]).get(_base_name(key), "")

    mtype = rows[0]["material_type"]
    # "Similar" means the same colour FAMILY, not the same spelling of it — otherwise a
    # Grey stone lists no Gray lookalikes. An uncoloured material keeps matching on type
    # alone, exactly as the old (? = '' OR ...) did.
    sim_colors = _colors_for_choice(conn, rows[0]["color"] or "")
    sim_cond = ("m.color IN (%s)" % ",".join("?" * len(sim_colors))) if sim_colors else "1=1"
    similar = [dict(x) for x in conn.execute(
        f"""SELECT m.item_name, m.material_key, m.color, MAX(m.image_url) AS image_url,
                  COUNT(DISTINCT m.supplier_id) AS suppliers, SUM(m.available_slabs) AS slabs
           FROM materials m
           WHERE m.material_type = ? AND m.material_key <> ? AND m.material_key <> ''
                 AND ({sim_cond})
           GROUP BY m.material_key
           ORDER BY (MAX(m.image_url) = '' OR MAX(m.image_url) IS NULL),
                    SUM(m.available_slabs) DESC
           LIMIT 12""",
        (mtype, key, *sim_colors),
    )]
    lists = db.get_lists(conn)
    conn.close()
    # External, cited reference facts (origin/quarries/price) — the same panel the
    # photo-ID page shows, surfaced on the canonical page where people actually browse
    # to a stone. lookup() matches on trade name, so a supplier type disagreement
    # ("Taj Mahal" as marble vs quartzite) still resolves to the one researched entry.
    ref = reference.summarize(reference.lookup(material_key=key, name=name))
    # General, category-level care guidance (not a per-stone fact) — the canonical type
    # is the part after the '|' in the material_key, falling back to the row's type.
    ctype = (key.split("|", 1)[1] if "|" in key else "") or mtype
    care = reference.care_by_type(ctype)
    # Typical market price for the CATEGORY — cited, and rendered inside the category
    # panel so it can never be misread as this stone's price or as catalog-observed money.
    band = reference.price_band_by_type(ctype)
    return templates.TemplateResponse(request, "material.html", {
        "rows": rows, "name": name, "key": key, "suppliers_list": suppliers_list,
        "facts": facts, "photos": photos, "rep_photo": rep_photo, "similar": similar,
        "lists": lists, "added": added, "added_list": list, "ref": ref,
        "care": care, "care_type": ctype,
        "price_band": band, "price_band_text": reference.price_band_label(band),
    })


@app.get("/item", response_class=HTMLResponse)
def item(request: Request, id: int, added: int = -1, list: int = 0):
    """Detail view for one material at one supplier (StoneProfits-style drill-down)."""
    conn = db.connect()
    m = conn.execute(
        """SELECT m.*, COALESCE(NULLIF(s.company,''), s.host) AS supplier_name,
                  s.host AS supplier_host, s.products AS supplier_products,
                  s.last_crawled AS supplier_attempted, s.last_error AS supplier_error,
                  s.phone AS supplier_phone, s.email AS supplier_email,
                  s.token AS supplier_token
           FROM materials m JOIN suppliers s ON s.id = m.supplier_id
           WHERE m.id = ?""",
        (id,),
    ).fetchone()
    if not m:
        conn.close()
        return HTMLResponse("<p style='padding:40px;font-family:sans-serif'>Item not found. "
                            "<a href='/'>Back to search</a></p>", status_code=404)
    m = dict(m)
    # Is this supplier serving rows its last crawl failed to replace? Cheap: the residue set
    # is a handful of suppliers out of ~141.
    m["is_residue"] = m.get("supplier_id") in db.residue_supplier_ids(conn)
    # Every supplier that carries this material (one row per supplier).
    others = conn.execute(
        f"""SELECT COALESCE(NULLIF(s.company,''), s.host) AS supplier_name, s.host AS supplier_host,
                  mm.id AS id, mm.item_id AS item_id, mm.source_url AS source_url,
                  g.variants AS variants, g.slabs AS slabs,
                  g.tile_sf AS tile_sf, g.has_tile AS has_tile, g.image_url AS image_url
           FROM (SELECT supplier_id, MIN(id) AS min_id, COUNT(*) AS variants,
                        {_SLABS_RAW} AS slabs, {_TILE_SF_RAW} AS tile_sf,
                        {_HAS_TILE_RAW} AS has_tile, MAX(image_url) AS image_url
                 FROM materials WHERE material_key = ? AND material_key <> ''
                 GROUP BY supplier_id) g
           JOIN materials mm ON mm.id = g.min_id
           JOIN suppliers s ON s.id = g.supplier_id
           ORDER BY g.slabs DESC, supplier_name""",
        (m["material_key"],),
    ).fetchall()
    # Other variants (e.g. different thickness/finish) of this material at THIS
    # supplier — grouped so per-slab duplicates don't appear.
    variants = conn.execute(
        f"""SELECT MIN(id) AS id, item_name, thickness, finish, product_form,
                  {_SLABS_RAW} AS available_slabs, {_TILE_SF_RAW} AS tile_sf,
                  {_HAS_TILE_RAW} AS has_tile, MAX(image_url) AS image_url
           FROM materials
           WHERE material_key = ? AND supplier_id = ?
                 AND NOT (name_norm = ? AND thickness = ? AND finish = ?)
           GROUP BY name_norm, thickness, finish, product_form
           ORDER BY thickness, finish""",
        (m["material_key"], m["supplier_id"], m["name_norm"], m["thickness"], m["finish"]),
    ).fetchall()

    # Similar materials: same type + same colour (or shared name word), other
    # materials, one row per material_key, prefer those with stock + a photo.
    first_word = (m["name_norm"] or "").split(" ")[0] if m["name_norm"] else ""
    # Same colour FAMILY (see _colors_for_choice), so a Grey stone still finds the Gray
    # ones. '\0' keeps the old "match nothing on colour" behaviour when it has none.
    sim_colors = _colors_for_choice(conn, m["color"] or "") or ["\0"]
    sim_cond = "m.color IN (%s)" % ",".join("?" * len(sim_colors))
    similar = conn.execute(
        f"""SELECT MIN(m.id) AS id, m.item_name, m.material_type, m.color, m.material_key,
                  MAX(m.image_url) AS image_url,
                  COUNT(DISTINCT m.supplier_id) AS suppliers,
                  {_SLABS} AS slabs
           FROM materials m
           WHERE m.material_type = ? AND m.material_key <> ? AND m.material_key <> ''
                 AND ({sim_cond} OR (? <> '' AND m.name_norm LIKE ?))
           GROUP BY m.material_key
           ORDER BY (MAX(m.image_url) = '' OR MAX(m.image_url) IS NULL),
                    {_SLABS} DESC
           LIMIT 12""",
        (m["material_type"], m["material_key"], *sim_colors,
         first_word, f"{first_word}%"),
    ).fetchall()
    # Fallback image for slabs (and the hero) with no photo of their own: the item's
    # own product photo, else a labeled representative from the same trade name.
    base = _base_name(m["material_key"])
    rep_photo = m["image_url"] or _representative_photos(conn, [base]).get(base, "")
    lists_avail = db.get_lists(conn)
    conn.close()
    return templates.TemplateResponse(
        request, "item.html",
        {"m": m, "others": [dict(o) for o in others],
         "variants": [dict(v) for v in variants], "similar": [dict(x) for x in similar],
         "lists": lists_avail, "added": added, "added_list": list, "rep_photo": rep_photo},
    )


@app.get("/api/slabs")
async def api_slabs(id: int, live: int = 0):
    """Per-slab gallery for a material.

    Default: serve the nightly pre-cache if present (instant), else fetch live.
    `?live=1` skips the cache and reads the supplier's catalog now, so the item
    page can paint the cached gallery immediately and then refresh it to a live,
    time-stamped reading. `n_items` (how many item_ids this product spans) lets the
    client tell whether it can safely restate the header slab count as live.
    """
    conn = db.connect()
    row = conn.execute(
        """SELECT m.item_id, m.supplier_id, m.name_norm, m.thickness, m.finish,
                  s.host, s.image_base, s.token
           FROM materials m JOIN suppliers s ON s.id = m.supplier_id WHERE m.id = ?""",
        (id,),
    ).fetchone()
    if not row or not row["item_id"]:
        conn.close()
        return JSONResponse({"slabs": [], "error": "unknown item"}, status_code=404)

    # The search list merges a product's batches (same name/thickness/finish) into
    # one row, so gather slabs across all of that product's item_ids at this supplier.
    item_ids = [r["item_id"] for r in conn.execute(
        """SELECT DISTINCT item_id FROM materials
           WHERE supplier_id = ? AND name_norm = ? AND thickness = ? AND finish = ?
                 AND item_id <> ''""",
        (row["supplier_id"], row["name_norm"], row["thickness"], row["finish"]),
    )] or [row["item_id"]]
    cached = []
    if not live:
        ph = ",".join("?" for _ in item_ids)
        cached = [dict(c) for c in conn.execute(
            f"""SELECT slab_no, location, length, width, qty, uom, barcode, image_url
                FROM slabs WHERE supplier_id = ? AND item_id IN ({ph})
                ORDER BY (image_url = '' OR image_url IS NULL), slab_no""",
            (row["supplier_id"], *item_ids),
        ).fetchall()]
    conn.close()
    if cached:
        slab_list = [{
            "photo": c["image_url"] or "", "thumb": c["image_url"] or "",
            "slab_no": c["slab_no"], "location": c["location"],
            "length": c["length"], "width": c["width"],
            "qty": c["qty"], "uom": c["uom"], "barcode": c["barcode"],
        } for c in cached]
        return JSONResponse({"slabs": slab_list, "cached": True, "n_items": len(item_ids)})

    # slabs.fetch_slabs is StoneProfits-only (it reads the page's SPSWebToken and the
    # SPS API). A provider supplier — UMI/SlabWare/SlabCloud/StoneTrash, no SPS token —
    # has no live per-slab feed, so don't burn ~30s driving a browser to a doomed fetch
    # (which returns nothing and could blank a good cached gallery). Say so instead.
    if not row["token"]:
        return JSONResponse({"slabs": [], "cached": False, "live_supported": False})

    # Forced live (?live=1), or nothing pre-cached -> read the supplier's catalog now.
    try:
        found = await slabs.fetch_slabs(row["host"], row["item_id"], row["image_base"] or "")
        return JSONResponse({"slabs": found, "cached": False, "n_items": len(item_ids)})
    except Exception as e:  # noqa: BLE001 - never 500 the detail page over a gallery fetch
        return JSONResponse({"slabs": [], "error": str(e), "cached": False})


_refresh = {"running": False, "done": False, "summary": "", "done_count": 0, "total": 0}


def _run_refresh_job(with_slabs: bool) -> None:
    from .. import discover as disc
    from ..ingest import run_all
    entries = disc.load_suppliers()
    _refresh.update(running=True, done=False, summary="Starting…",
                    done_count=0, total=len(entries), materials=0)

    def progress(label: str, n: int) -> None:
        _refresh["done_count"] += 1
        _refresh["materials"] += max(n, 0)
        shown = min(_refresh["done_count"], _refresh["total"])
        _refresh["summary"] = (f"Crawled {shown}/{_refresh['total']} catalogs · "
                               f"{_refresh['materials']} materials · now: {label[:30]}")

    try:
        # run_all, not run: suppliers.json is no longer all Stone Profits, and a
        # UMI/SlabWare entry sent to the Playwright crawler just fails.
        # Materials only by default — slab galleries load on demand (see /api/slabs).
        # `with_slabs` is the opt-in deep refresh, and even then it's bounded
        # (slab_item_cap): only each supplier's most-stocked items are pre-cached, so
        # the deep refresh stays quick while still seeding the map's yard locations.
        asyncio.run(run_all(
            entries, concurrency=4, delay=1.0, headless=True,
            db_path=str(db.DEFAULT_DB), with_slabs=with_slabs, slab_item_cap=40,
            retry_errored=True, progress=progress,
        ))
        # The crawl changed the data — drop the page caches so the UI reflects it now
        # rather than after the TTL, including the "Done — N materials" line below.
        _invalidate_caches()
        conn = db.connect()
        s = db.stats(conn)
        conn.close()
        _refresh["summary"] = (f"Done — {s['materials']} materials, "
                               f"{s['suppliers']} suppliers updated.")
    except Exception as e:  # noqa: BLE001
        _refresh["summary"] = f"Refresh failed: {e}"
    finally:
        _refresh["running"] = False
        _refresh["done"] = True


@app.post("/api/refresh")
def api_refresh(slabs: bool = False):
    """Kick off a full re-crawl in the background (the UI's Refresh button). Defaults
    to materials only (fast); pass ?slabs=1 for a deep refresh that also pre-caches
    slab galleries + locations (much slower — used by the nightly task)."""
    if _refresh["running"]:
        return JSONResponse({"running": True, "summary": _refresh["summary"]})
    threading.Thread(target=_run_refresh_job, args=(slabs,), daemon=True).start()
    return JSONResponse({"running": True, "summary": "Refresh started."})


@app.get("/api/refresh/status")
def api_refresh_status():
    return JSONResponse(_refresh)


@app.get("/api/search")
def api_search(
    q: str = "", material_type: str = "", color: str = "", thickness: str = "",
    finish: str = "",
    supplier: str = "", location: str = "", min_length: str = "", min_width: str = "",
    new_only: int = 0, in_stock: int = 0, sort: str = "relevance",
    near: str = "", radius: str = "", min_sqft: str = "",
    limit: int = Query(100, le=500), offset: int = 0,
):
    conn = db.connect()
    origin, near_label = _resolve_near(near)
    total, rows, parsed = _search(
        conn, q=q, material_type=material_type, color=color, thickness=thickness,
        finish=finish,
        supplier=supplier, location=location, min_length=_to_float(min_length),
        min_width=_to_float(min_width), new_only=bool(new_only),
        in_stock=bool(in_stock), sort=sort if sort in SORTS else "relevance",
        near=origin, radius_mi=(_to_float(radius) or 150.0) if origin else 0.0,
        min_sqft=_to_float(min_sqft), limit=limit, offset=offset,
    )
    conn.close()
    return JSONResponse({"total": total, "count": len(rows), "results": rows,
                         "near": near_label if origin else None,
                         "near_unresolved": bool(near) and not origin,
                         "interpreted": smartsearch.summary(parsed) if q else []})


def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    """A downloadable CSV. Excel-friendly (UTF-8 BOM) so accented stone names render."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/export.csv")
def export_search_csv(
    q: str = "", material_type: str = "", color: str = "", thickness: str = "",
    finish: str = "",
    supplier: str = "", location: str = "", min_length: str = "", min_width: str = "",
    new_only: int = 0, in_stock: int = 0, sort: str = "relevance",
    near: str = "", radius: str = "", min_sqft: str = "",
):
    """The current search, one row per material, as a spreadsheet."""
    conn = db.connect()
    origin, _ = _resolve_near(near)
    _t, rows, _p = _search(
        conn, q=q, material_type=material_type, color=color, thickness=thickness,
        finish=finish,
        supplier=supplier, location=location, min_length=_to_float(min_length),
        min_width=_to_float(min_width), new_only=bool(new_only),
        in_stock=bool(in_stock), sort=sort if sort in SORTS else "relevance",
        near=origin, radius_mi=(_to_float(radius) or 150.0) if origin else 0.0,
        min_sqft=_to_float(min_sqft), limit=5000, offset=0,
    )
    conn.close()
    header = ["Material", "Type", "Color", "Suppliers", "Slabs (best yard)",
              "Total ft²", "New", "Material key"]
    out = [[
        ((r.get("material_key") or "").split("|")[0].title() or r.get("item_name", "")),
        r.get("material_type", ""), r.get("color", ""), r.get("suppliers", 0),
        r.get("available_slabs", 0) or 0, round(r.get("total_sqft") or 0, 1),
        "yes" if r.get("new_arrival") else "", r.get("material_key", ""),
    ] for r in rows]
    return _csv_response("stone-search.csv", header, out)


@app.get("/photo", response_class=HTMLResponse)
def photo_page(request: Request, material_type: str = ""):
    """Search-by-photo: upload a stone photo, get visually-similar catalog materials."""
    conn = db.connect()
    ctx = {
        "request": request, "results": [], "queried": False, "error": "", "query_uri": "",
        "material_type": material_type, "model_ready": imagesearch.available(),
        "index": imagesearch.index_stats(conn), "types": _distinct(conn, "material_type"),
        "stats": db.stats(conn),
        "ident": {"known": False}, "evidence": {}, "ref": {"known": False},
        "ref_stats": reference.stats(),
    }
    conn.close()
    return templates.TemplateResponse(request, "photo.html", ctx)


_PER_SQFT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*/\s*(?:sf|sq\.?\s*ft)", re.I)
_BARE_AMOUNT_RE = re.compile(r"^\$\s*([\d,]+(?:\.\d+)?)$")


def _observed_prices(rows) -> dict:
    """Real money the catalog actually contains, separated by BASIS.

    `price_range` is 76% supplier tier codes — "Group 5", "Level 2",
    "Caesarstone B - Standard", and bare "$"/"$$$" symbols that are price *bands*,
    not amounts. Averaging that into "typical price" would invent a number. Only
    two shapes are real, and they mean different things, so they are never mixed:

      $11.90/sf   an explicit per-square-foot ask (StoneTrash marketplace listings)
      $570        a whole-slab total (Unbuilt / Marble Systems) — NOT per sq ft

    Everything else is dropped rather than coerced.
    """
    per_sqft, slab_total = [], []
    for r in rows:
        raw = (r["price_range"] or "").strip()
        if not raw:
            continue
        sup = r["supplier_name"]
        m = _PER_SQFT_RE.search(raw)
        if m:
            per_sqft.append((float(m.group(1).replace(",", "")), sup))
            continue
        m = _BARE_AMOUNT_RE.match(raw)
        if m:
            slab_total.append((float(m.group(1).replace(",", "")), sup))

    def band(vals):
        if not vals:
            return None
        nums = sorted(v for v, _ in vals)
        return {
            "low": nums[0], "high": nums[-1],
            "median": nums[len(nums) // 2],
            "n": len(nums),
            "suppliers": sorted({s for _, s in vals}),
        }

    return {"per_sqft": band(per_sqft), "slab_total": band(slab_total)}


def _catalog_evidence(conn, material_key: str) -> dict:
    """What the app can prove about a stone from its own data — the tier that links
    to a real supplier, kept visually separate from external reference facts."""
    if not material_key:
        return {}
    rows = conn.execute(
        """SELECT m.available_slabs, m.thickness, m.finish, m.product_form, m.uom,
                  m.price_range, m.locations, m.material_type, m.color,
                  COALESCE(NULLIF(s.company,''), s.host) AS supplier_name,
                  m.supplier_id
           FROM materials m JOIN suppliers s ON s.id = m.supplier_id
           WHERE m.material_key = ?""",
        (material_key,),
    ).fetchall()
    if not rows:
        return {}
    per_supplier: dict[str, float] = {}
    thicknesses, finishes, locations, types = set(), set(), set(), set()
    for r in rows:
        if "TILE" not in (r["product_form"] or "").upper():
            per_supplier[r["supplier_name"]] = (
                per_supplier.get(r["supplier_name"], 0) + (r["available_slabs"] or 0))
        for field, sink in (("thickness", thicknesses), ("finish", finishes),
                            ("material_type", types)):
            if (r[field] or "").strip():
                sink.add(r[field].strip())
        for loc in (r["locations"] or "").split(","):
            if loc.strip():
                locations.add(loc.strip())
    best = max(per_supplier.items(), key=lambda kv: kv[1]) if per_supplier else ("", 0)
    return {
        "suppliers": len({r["supplier_id"] for r in rows}),
        "listings": len(rows),
        "deepest_yard": {"supplier": best[0], "slabs": int(best[1])} if best[1] else None,
        "total_slabs": int(sum(per_supplier.values())),
        "thicknesses": sorted(thicknesses,
                              key=lambda t: float(re.match(r"([\d.]+)", t).group(1))
                              if re.match(r"([\d.]+)", t) else 99),
        "finishes": sorted(finishes)[:6],
        "locations": sorted(locations)[:8],
        "types": sorted(types),
        "prices": _observed_prices(rows),
    }


# --- Compare: side-by-side canonical materials --------------------------------

def _compare_column(conn, entry: dict, loc_miles: dict | None) -> dict:
    """Assemble one /compare column for a tray entry, building on _catalog_evidence().

    Follows a Quality merge (material_aliases) to the surviving canonical key first. If the
    material resolves to no catalog rows (gone from every catalog), returns a `gone` column
    carrying the stored name/photo snapshot so it still renders — same honesty as a sourcing
    list. `loc_miles` (location -> distance from a `near` origin) is None unless a near city
    was given; when present, `miles` is the nearest stocking yard, else None ("—")."""
    orig_key = entry["material_key"]
    key = db.resolve_alias(conn, orig_key)
    ev = _catalog_evidence(conn, key)
    if not ev:
        return {"gone": True, "orig_key": orig_key, "key": key,
                "name": entry.get("item_name") or _base_name(orig_key).title(),
                "image": entry.get("image_url") or ""}

    # Catalog fields _catalog_evidence doesn't carry (name/photo/colors/forms/size/tile).
    rows = conn.execute(
        """SELECT m.item_name, m.color, m.product_form, m.uom, m.available_slabs,
                  m.avg_length, m.avg_width, m.image_url,
                  COALESCE(NULLIF(m.locations,''), s.locations) AS locs
           FROM materials m JOIN suppliers s ON s.id = m.supplier_id
           WHERE m.material_key = ?""",
        (key,),
    ).fetchall()

    names: dict[str, int] = {}
    for r in rows:
        if (r["item_name"] or "").strip():
            names[r["item_name"]] = names.get(r["item_name"], 0) + 1
    name = max(names, key=names.get) if names else _base_name(key).title()

    # Best photo: a real one wins; SlabCloud "coming soon" placeholders sort last.
    imgs = sorted((r["image_url"] for r in rows if r["image_url"]),
                  key=lambda u: "slabcloud.com/slabs/" in u)
    image = imgs[0] if imgs else (entry.get("image_url") or "")

    # Colors: keep only real color words (some suppliers paste a whole description in).
    colors: set[str] = set()
    for r in rows:
        for part in (r["color"] or "").split(","):
            part = part.strip()
            if part and len(part) <= 18 and len(part.split()) <= 2 and not re.search(r"\d", part):
                colors.add(part.title())

    forms = sorted({(r["product_form"] or "").strip() for r in rows if (r["product_form"] or "").strip()})
    lengths = [r["avg_length"] for r in rows if r["avg_length"]]
    widths = [r["avg_width"] for r in rows if r["avg_width"]]
    size_range = (f"{min(lengths):.0f}×{min(widths):.0f} – {max(lengths):.0f}×{max(widths):.0f}"
                  if lengths and widths else "")
    # Tile area, kept separate from slab counts exactly as _catalog_evidence does.
    tile_sf = sum((r["available_slabs"] or 0) for r in rows
                  if "TILE" in (r["product_form"] or "").upper() and (r["uom"] or "").upper() == "SF")

    miles = None
    if loc_miles is not None:
        for r in rows:
            for loc in (r["locs"] or "").split(","):
                d = loc_miles.get(loc.strip().lower())
                if d is not None and (miles is None or d < miles):
                    miles = d

    # Canonical type is the part after '|' in the key, falling back to the row type — the
    # same rule /material uses for the care panel.
    ctype = (key.split("|", 1)[1] if "|" in key else "") or (ev["types"][0] if ev["types"] else "")
    ref = reference.summarize(reference.lookup(material_key=key, name=name))
    care = reference.care_by_type(ctype)
    return {
        "gone": False, "orig_key": orig_key, "key": key, "name": name, "image": image,
        "types": ev["types"], "colors": sorted(colors)[:8], "finishes": ev["finishes"],
        "thicknesses": ev["thicknesses"], "forms": forms, "size_range": size_range,
        "suppliers": ev["suppliers"], "deepest_yard": ev["deepest_yard"],
        "total_slabs": ev["total_slabs"], "tile_sf": tile_sf, "locations": ev["locations"],
        "prices": ev["prices"], "miles": miles, "ref": ref, "care": care, "care_type": ctype,
    }


def _compare_winner(cols: list[dict], getter, mode: str):
    """Index of the single column that leads a countable row, or None. None when fewer
    than two columns carry a value, or when the top value is shared (a tie is not a
    winner). `mode` is 'max' (more is better) or 'min' (lower $ / nearer is better)."""
    pairs = []
    for i, c in enumerate(cols):
        if c.get("gone"):
            continue
        v = getter(c)
        if v is not None:
            pairs.append((i, v))
    if len(pairs) < 2:
        return None
    best = (min if mode == "min" else max)(v for _, v in pairs)
    leaders = [i for i, v in pairs if v == best]
    return leaders[0] if len(leaders) == 1 else None


def _compare_columns(conn, near_origin=None):
    """Build every compare column plus the winner-highlight map. `near_origin` is a
    (lat, lon) tuple or None; when set, a nearest-yard distance is computed per column
    over ALL resolved locations (a large radius, so it informs rather than filters)."""
    tray = db.get_compare(conn)
    loc_miles = (geocode.locations_within(conn, near_origin[0], near_origin[1], 1e12)
                 if near_origin else None)
    cols = [_compare_column(conn, e, loc_miles) for e in tray]
    winners = {
        "suppliers":  _compare_winner(cols, lambda c: c.get("suppliers"), "max"),
        "deepest":    _compare_winner(cols, lambda c: (c.get("deepest_yard") or {}).get("slabs"), "max"),
        "total":      _compare_winner(cols, lambda c: c.get("total_slabs") or None, "max"),
        "price_sqft": _compare_winner(cols, lambda c: (c.get("prices", {}).get("per_sqft") or {}).get("low"), "min"),
        "nearest":    _compare_winner(cols, lambda c: c.get("miles"), "min"),
    }
    return cols, winners


@app.post("/photo", response_class=HTMLResponse)
async def photo_search(request: Request, photo: UploadFile = File(...),
                       material_type: str = Form("")):
    import base64
    conn = db.connect()
    results, error, query_uri = [], "", ""
    if not imagesearch.available():
        error = "The image-search model isn't installed in this build."
    else:
        try:
            data = await photo.read()
            query_uri = "data:{};base64,{}".format(
                photo.content_type or "image/jpeg", base64.b64encode(data).decode())
            qvec = await asyncio.to_thread(imagesearch.embed_bytes, data)  # CPU work off the loop
            results = imagesearch.search(conn, qvec, top_k=60, material_type=material_type)
            # CLIP cosine sits ~0.5–1.0; rescale to a friendlier 0–100 "match" for display.
            for r in results:
                r["match"] = max(0, min(100, round((r["score"] - 0.5) * 200)))
        except Exception as e:  # noqa: BLE001 - never 500 the page over a bad upload
            error = f"Couldn't read that image: {e}"

    # Identify from the consensus of the top matches, then hang both fact tiers off
    # the winner: what the catalog can prove, and what external research established.
    ident = imagesearch.identify(results) if results else {"known": False}
    # A "none" verdict has no subject. Both fact tiers hang off the named stone, so
    # filling them anyway would smuggle back the answer the verdict just declined
    # to give — a page that says "no confident identification" above a full dossier
    # on Calacatta Gold has still told the user it is Calacatta Gold.
    named = bool(ident.get("known")) and ident.get("confidence") != "none"
    key = ident.get("best", {}).get("material_key", "") if named else ""
    evidence = _catalog_evidence(conn, key) if key else {}
    ref = reference.summarize(
        reference.lookup(material_key=key, name=ident.get("best", {}).get("name", ""))
    ) if key else {"known": False}

    ctx = {
        "request": request, "results": results, "queried": True, "error": error,
        "query_uri": query_uri, "material_type": material_type,
        "ident": ident, "evidence": evidence, "ref": ref,
        "model_ready": imagesearch.available(), "index": imagesearch.index_stats(conn),
        "types": _distinct(conn, "material_type"), "stats": db.stats(conn),
        "ref_stats": reference.stats(),
    }
    conn.close()
    return templates.TemplateResponse(request, "photo.html", ctx)


@app.post("/photo/lookup", response_class=HTMLResponse)
async def photo_lookup(request: Request, key: str = Form(""), name: str = Form(""),
                       stone_type: str = Form("")):
    """Research a stone we have no curated entry for, on demand, and remember it.

    Runs off the event loop because it makes real network calls. What it can
    establish is deliberately narrow (see reference.lookup_live) — it will not
    invent a quarry list or a price to fill the panel.
    """
    entry = await asyncio.to_thread(reference.lookup_live, name, stone_type, key)
    if entry:
        await asyncio.to_thread(reference.remember, entry)
    return RedirectResponse(f"/material?key={quote(key)}" if key else "/photo",
                            status_code=303)


def _stock_changes(conn) -> tuple[list[dict], list[dict], bool]:
    """What changed, compared PER SUPPLIER — each supplier's newest snapshot
    against that same supplier's own previous one.

    Snapshots are written per-supplier as each is crawled, so any two *global*
    dates rarely cover the same suppliers. The first fix guarded against that by
    requiring presence in both of the two most recent dates — which honestly
    returned zero, forever, because those two dates shared no suppliers while real
    signal (a supplier crawled today with its own baseline from three days ago)
    was discarded. The comparison each supplier actually supports is against its
    own history, so that is the one we run.

    Two distinct kinds of change, and the split matters:
      restock — the product was LISTED at the previous snapshot with zero stock
                everywhere, and has stock now. "Back in stock" means it.
      listed  — the product was ABSENT from the previous snapshot: a new listing,
                not a restock. These used to be silently dropped (the supplier-
                flagged New Arrivals section only shows what suppliers mark NEW),
                so real detected change went nowhere.

    Returns (restock, newly_listed, has_baseline); has_baseline False means no
    supplier has two snapshots yet, so the page can explain the empty state.
    """
    _sk = _db_path(conn)
    _h = _stock_cache.get(_sk)
    if _h and time.time() - _h[0] < _RESULT_TTL:
        return _h[1]
    has_baseline = conn.execute(
        "SELECT 1 FROM history GROUP BY supplier_id "
        "HAVING COUNT(DISTINCT snapshot_date) >= 2 LIMIT 1"
    ).fetchone() is not None
    if not has_baseline:
        _stock_cache[_sk] = (time.time(), ([], [], False))
        return _stock_cache[_sk][1]
    rows = conn.execute(
        """WITH latest AS (
               SELECT supplier_id, MAX(snapshot_date) AS d
               FROM history GROUP BY supplier_id
           ), prev AS (
               SELECT h.supplier_id, MAX(h.snapshot_date) AS d
               FROM history h JOIN latest l
                 ON l.supplier_id = h.supplier_id AND h.snapshot_date < l.d
               GROUP BY h.supplier_id
           )
           SELECT h.name_norm, h.material_type, h.color, h.thickness, h.slabs,
                  h.image_url, pr.d AS since,
                  COALESCE(NULLIF(s.company,''), s.host) AS supplier_name, s.host AS supplier_host,
                  EXISTS (
                      SELECT 1 FROM history p WHERE p.snapshot_date = pr.d
                        AND p.supplier_id = h.supplier_id AND p.name_norm = h.name_norm
                        AND p.thickness = h.thickness) AS was_listed,
                  (SELECT MIN(mm.id) FROM materials mm
                     WHERE mm.supplier_id = h.supplier_id AND mm.name_norm = h.name_norm
                           AND mm.thickness = h.thickness) AS id
           FROM history h
           JOIN latest l ON l.supplier_id = h.supplier_id AND h.snapshot_date = l.d
           JOIN prev pr ON pr.supplier_id = h.supplier_id
           JOIN suppliers s ON s.id = h.supplier_id
           WHERE h.slabs > 0
                 AND NOT EXISTS (
                     SELECT 1 FROM history p WHERE p.snapshot_date = pr.d
                       AND p.supplier_id = h.supplier_id AND p.name_norm = h.name_norm
                       AND p.thickness = h.thickness AND p.slabs > 0)
           ORDER BY h.slabs DESC LIMIT 600""",
    ).fetchall()
    restock = [dict(r) for r in rows if r["id"] and r["was_listed"]][:300]
    listed = [dict(r) for r in rows if r["id"] and not r["was_listed"]][:300]
    _stock_cache[_sk] = (time.time(), (restock, listed, True))
    return _stock_cache[_sk][1]


# --- Alerts: catalog-change digest (all four kinds, with unread signatures) ----
#
# Separate from _stock_changes (which /new and /watchlist keep using unchanged): the
# alert digest adds the DOWN direction (sharp drops + sold-outs) and carries a per-change
# signature for unread tracking. It compares each supplier's latest snapshot against its
# own previous one at the alert grain (name_norm + thickness), aggregating the finish/form
# variants that snapshot_history writes as separate rows — so a listing's stock is one
# number per date and a variant split can't masquerade as a drop.

_ALERT_ROWS_SQL = """
WITH agg AS (
    SELECT supplier_id, snapshot_date, name_norm, thickness,
           SUM(slabs) AS slabs, MAX(material_type) AS material_type,
           MAX(color) AS color, MAX(image_url) AS image_url,
           MAX(COALESCE(rebased,0)) AS rebased
    FROM history GROUP BY supplier_id, snapshot_date, name_norm, thickness
),
latest AS (SELECT supplier_id, MAX(snapshot_date) AS d FROM history GROUP BY supplier_id),
prev AS (
    SELECT h.supplier_id, MAX(h.snapshot_date) AS d
    FROM history h JOIN latest l ON l.supplier_id = h.supplier_id AND h.snapshot_date < l.d
    GROUP BY h.supplier_id
)
SELECT a.supplier_id, a.name_norm, a.thickness, a.material_type, a.color, a.image_url,
       a.slabs AS latest_slabs, p.slabs AS prev_slabs, l.d AS latest_date, pr.d AS prev_date,
       a.rebased AS latest_rebased, COALESCE(p.rebased, 0) AS prev_rebased,
       COALESCE(NULLIF(s.company,''), s.host) AS supplier_name, s.host AS supplier_host,
       (SELECT MIN(mm.id) FROM materials mm
          WHERE mm.supplier_id = a.supplier_id AND mm.name_norm = a.name_norm
                AND mm.thickness = a.thickness) AS id
FROM agg a
JOIN latest l ON l.supplier_id = a.supplier_id AND a.snapshot_date = l.d
JOIN prev pr  ON pr.supplier_id = a.supplier_id
LEFT JOIN agg p ON p.supplier_id = a.supplier_id AND p.snapshot_date = pr.d
              AND p.name_norm = a.name_norm AND p.thickness = a.thickness
JOIN suppliers s ON s.id = a.supplier_id
"""

# A sharp drop: fell to <= half AND by at least this many slabs (the absolute floor keeps
# routine 2->1 sales out). A sold-out (in-stock -> 0) is its own kind, not a "drop".
_ALERT_DROP_FLOOR = 5


def _classify_alert(latest: int, prev, *, crossed_rebase: bool = False):
    """The alert kind for one listing's latest-vs-previous slab counts, or None.
    `prev` is None when the listing was absent at the previous snapshot.

    `crossed_rebase` marks a comparison that spans the change in how per-item slab counts
    are totalled (AIL-12). Across that boundary almost every listing appears to fall — the
    old snapshot multiplied each item's total by its row count — so the DOWN kinds are
    suppressed for that one comparison rather than reporting a catalog-wide sell-off that
    never happened. The UP kinds stay: a restock or a new listing is still real, and the
    thresholds themselves are untouched. Self-healing — once both snapshots are on the new
    accounting the flag is false again and drops report normally.
    """
    if prev is None:
        return "listed" if latest > 0 else None
    if prev == 0:
        return "restock" if latest > 0 else None
    # prev > 0
    if latest == 0:
        return None if crossed_rebase else "soldout"
    if latest * 2 <= prev and (prev - latest) >= _ALERT_DROP_FLOOR:
        return None if crossed_rebase else "dropped"
    return None


_ALERT_KINDS = ("restock", "listed", "dropped", "soldout")


def _alert_digest(conn) -> dict:
    """The full catalog-change digest: {restock, listed, dropped, soldout, has_baseline}.
    Each change item carries a stable `sig` for unread tracking. TTL-cached per DB (a crawl
    clears it via _invalidate_caches)."""
    sk = _db_path(conn)
    hit = _alert_cache.get(sk)
    if hit and time.time() - hit[0] < _RESULT_TTL:
        return hit[1]
    has_baseline = conn.execute(
        "SELECT 1 FROM history GROUP BY supplier_id HAVING COUNT(DISTINCT snapshot_date) >= 2 LIMIT 1"
    ).fetchone() is not None
    digest = {k: [] for k in _ALERT_KINDS}
    digest["has_baseline"] = has_baseline
    if not has_baseline:
        _alert_cache[sk] = (time.time(), digest)
        return digest
    for r in conn.execute(_ALERT_ROWS_SQL).fetchall():
        latest = r["latest_slabs"] or 0
        prev = r["prev_slabs"]  # None => absent at previous snapshot
        # Only the first comparison after the collapse spans the two accountings.
        crossed = bool(r["latest_rebased"]) and not bool(r["prev_rebased"])
        kind = _classify_alert(latest, prev, crossed_rebase=crossed)
        if not kind:
            continue
        item = dict(r)
        item["kind"] = kind
        item["latest_slabs"] = latest
        item["prev_slabs"] = prev
        item["sig"] = f"{kind}|{r['supplier_id']}|{r['name_norm']}|{r['thickness']}|{r['latest_date']}"
        digest[kind].append(item)
    # Order each bucket most-significant first, and cap (as _stock_changes does).
    digest["restock"].sort(key=lambda x: -(x["latest_slabs"] or 0))
    digest["listed"].sort(key=lambda x: -(x["latest_slabs"] or 0))
    digest["dropped"].sort(key=lambda x: -((x["prev_slabs"] or 0) - (x["latest_slabs"] or 0)))
    digest["soldout"].sort(key=lambda x: -(x["prev_slabs"] or 0))
    for k in _ALERT_KINDS:
        digest[k] = digest[k][:300]
    _alert_cache[sk] = (time.time(), digest)
    return digest


def _stock_drops(conn) -> tuple[list[dict], list[dict]]:
    """The DOWN direction of the digest: (sharp drops, sold outs). A thin view over
    _alert_digest so the down-direction logic has a named, separately-testable entry point
    while _stock_changes stays untouched."""
    d = _alert_digest(conn)
    return d["dropped"], d["soldout"]


def _alert_unread_count(conn) -> int:
    """How many current digest changes have not been marked seen — the nav badge number."""
    digest = _alert_digest(conn)
    seen = db.seen_alert_sigs(conn)
    return sum(1 for k in _ALERT_KINDS for it in digest[k] if it["sig"] not in seen)


@app.get("/new", response_class=HTMLResponse)
def whats_new(request: Request, page: int = 1):
    """New arrivals (catalog-flagged) plus anything newly back in stock."""
    conn = db.connect()
    limit = 60
    offset = max(page - 1, 0) * limit
    total, rows, _ = _search(
        conn, q="", material_type="", color="", thickness="", supplier="",
        new_only=True, limit=limit, offset=offset,
    )
    _attach_rep_photos(conn, rows)
    restock, newly_listed, has_baseline = _stock_changes(conn)
    stats = db.stats(conn)
    conn.close()
    return templates.TemplateResponse(request, "whatsnew.html", {
        "rows": rows, "total": total, "page": page, "pages": (total + limit - 1) // limit,
        "restock": restock, "newly_listed": newly_listed,
        "has_baseline": has_baseline, "stats": stats,
    })


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    """Catalog-change digest since each supplier's previous crawl — restocks, new listings,
    sharp drops and sold-outs — with per-change unread tracking. Opening the page marks the
    shown changes as seen (auto-clear), while still flagging which were unread at load."""
    conn = db.connect()
    digest = _alert_digest(conn)
    seen = db.seen_alert_sigs(conn)
    sigs = []
    for kind in _ALERT_KINDS:
        for it in digest[kind]:
            it["unread"] = it["sig"] not in seen  # captured BEFORE marking, for the NEW badge
            sigs.append(it["sig"])
    db.mark_alerts_seen(conn, sigs)  # auto-clear: the nav badge reads 0 on the next render
    total = sum(len(digest[k]) for k in _ALERT_KINDS)
    stats = db.stats(conn)
    conn.close()
    return templates.TemplateResponse(request, "alerts.html", {
        "digest": digest, "total": total, "stats": stats,
    })


@app.get("/api/alerts/count")
def api_alerts_count():
    """Unread change count for the 🔔 badge — polled by the refresh flow to relight it
    live when a crawl finishes, without a page reload."""
    conn = db.connect()
    try:
        n = _alert_unread_count(conn)
    finally:
        conn.close()
    return JSONResponse({"unread": n})


@app.get("/locations", response_class=HTMLResponse)
def locations_page(request: Request):
    """Browse by stocking location: a pin map plus the full list. Locations that
    aren't places (internal warehouse names) are reported, never guessed at."""
    conn = db.connect()
    locs = db.location_counts(conn)
    geo = db.location_geo(conn)

    # Resolve anything new since the last visit (a fresh crawl adds locations).
    missing = [l["location"] for l in locs if l["location"] not in geo]
    if missing:
        from .. import geocode as gc
        for loc in missing:
            db.save_location_geo(conn, loc, gc.resolve(loc))
        geo = db.location_geo(conn)

    pins, unmapped = [], []
    for l in locs:
        g = geo.get(l["location"]) or {}
        if g.get("lat") is None:
            unmapped.append(l)
            continue
        pins.append({**l, "lat": g["lat"], "lon": g["lon"],
                     "label": g["label"], "source": g["source"],
                     "approx": g["source"] == "ambiguous"})
    stats = db.stats(conn)
    conn.close()
    return templates.TemplateResponse(request, "locations.html", {
        "locs": locs, "pins": pins, "unmapped": unmapped, "stats": stats,
        "overrides_file": str(gcode_path()),
    })


def gcode_path():
    from ..geocode import OVERRIDES_PATH
    return OVERRIDES_PATH


@app.get("/health", response_class=HTMLResponse)
def health(request: Request):
    """Per-supplier crawl health: what's fresh, stale, broken, or empty."""
    from .. import discover
    conn = db.connect()
    rows = db.supplier_health(conn)
    provider_of = {s["host"]: (s.get("provider") or "stoneprofits")
                   for s in discover.load_suppliers()}
    for r in rows:
        r["provider"] = provider_of.get(r["host"], "stoneprofits")
    order = {"broken": 0, "stale": 1, "empty": 2, "blocked": 3, "ok": 4}
    rows.sort(key=lambda r: (order[r["status"]], -(r["item_count"] or 0)))
    counts = {k: sum(1 for r in rows if r["status"] == k)
              for k in ("ok", "stale", "broken", "empty", "blocked")}
    mirrors = db.mirror_report(conn)
    proposals = db.supplier_filter_proposals(conn)
    stats = db.stats(conn)
    # The nightly refresh runs in a separate process, so the in-memory _refresh dict above
    # is blind to it and every scheduled run has been invisible here. This is the durable
    # record; it returns [] rather than raising on a snapshot DB predating the table.
    runs = db.recent_refresh_runs(conn)
    conn.close()
    # What the SCHEDULER thinks, which is a different question from what the runs recorded:
    # a task that never fired leaves no run at all, so the ledger above cannot report it.
    # Read-only and never raises — see taskcheck's module docstring.
    from .. import taskcheck
    task = taskcheck.check()
    return templates.TemplateResponse(request, "health.html", {
        "rows": rows, "counts": counts, "stats": stats, "total": len(rows),
        "mirrors": mirrors, "proposals": proposals, "runs": runs, "task": task,
    })


@app.post("/health/filter/confirm")
def health_filter_confirm(host: str = Form(...), material_type: str = Form(...)):
    """Confirm a mirror's proposed storefront filter: re-scope it to that type so it stops
    being a mirror and returns to the facet with its true count."""
    conn = db.connect()
    kept = db.add_supplier_filter(conn, host, material_type)
    if kept:
        db.detect_mirrors(conn)          # the re-scoped host is no longer a mirror
        db.rebuild_product_rollup(conn)  # counts/grouping changed -> refresh the fast path
    conn.close()
    _invalidate_caches()
    return RedirectResponse("/health", status_code=303)


@app.post("/health/filter/reject")
def health_filter_reject(host: str = Form(...), material_type: str = Form(...)):
    """Decline a proposed storefront filter so it isn't offered again (the host stays a mirror)."""
    conn = db.connect()
    db.reject_supplier_filter(conn, host, material_type)
    conn.close()
    _invalidate_caches()
    return RedirectResponse("/health", status_code=303)


_MERGE_PER_PAGE = 30
_ALIAS_DELIM = "||"  # material_keys hold single pipes, never a double — safe joiner


def _cluster_vm(c: dict) -> dict:
    """Add the derived fields the merge template needs (the alias_keys to fold)."""
    aliases = [m["mk"] for m in c["members"] if m["mk"] != c["canonical_key"]]
    return {**c, "alias_keys": _ALIAS_DELIM.join(aliases), "n_fold": len(aliases)}


@app.get("/quality", response_class=HTMLResponse)
def quality(request: Request):
    """Data-quality hub: how clean the cross-supplier grouping is, and what's left to
    curate — type conflicts, spelling variants, the Other bucket, missing colours."""
    conn = db.connect()
    _qk = _db_path(conn)
    _h = _quality_cache.get(_qk)
    if _h and time.time() - _h[0] < _RESULT_TTL:
        qs, conflicts, fuzzy = _h[1]
    else:
        qs = db.quality_stats(conn)
        conflicts = dedupe.conflict_clusters(conn, limit=100000)
        fuzzy = dedupe.fuzzy_clusters(conn, limit=100000)
        _quality_cache[_qk] = (time.time(), (qs, conflicts, fuzzy))
    ctx = {
        "request": request,
        "qs": qs,
        "n_conflicts": len(conflicts),
        "n_fuzzy": len(fuzzy),
        "conflict_preview": [_cluster_vm(c) for c in conflicts[:4]],
        "fuzzy_preview": [_cluster_vm(c) for c in fuzzy[:4]],
        "aliases": db.list_aliases(conn)[:12],
        "stats": db.stats(conn),
    }
    conn.close()
    return templates.TemplateResponse(request, "quality.html", ctx)


@app.get("/quality/merge", response_class=HTMLResponse)
def quality_merge(request: Request, kind: str = "conflict", page: int = 1, merged: int = -1):
    """Review queue: confirm or reject candidate merges, one cluster at a time."""
    kind = kind if kind in ("conflict", "fuzzy") else "conflict"
    conn = db.connect()
    gen = dedupe.conflict_clusters if kind == "conflict" else dedupe.fuzzy_clusters
    clusters = gen(conn, limit=100000)
    total = len(clusters)
    page = max(page, 1)
    start = (page - 1) * _MERGE_PER_PAGE
    view = [_cluster_vm(c) for c in clusters[start:start + _MERGE_PER_PAGE]]
    conn.close()
    return templates.TemplateResponse(request, "quality_merge.html", {
        "request": request, "kind": kind, "clusters": view, "total": total,
        "page": page, "pages": (total + _MERGE_PER_PAGE - 1) // _MERGE_PER_PAGE,
        "merged": merged,
    })


def _merge_redirect(kind: str, page: int, merged: int) -> RedirectResponse:
    return RedirectResponse(f"/quality/merge?kind={kind}&page={page}&merged={merged}",
                            status_code=303)


@app.post("/quality/merge/apply")
def quality_merge_apply(
    canonical_key: str = Form(...), alias_keys: str = Form(""),
    canonical_type: str = Form(""), kind: str = Form("conflict"), page: int = Form(1),
):
    """Fold a cluster's members into its canonical key (and re-apply so it takes
    effect immediately, before the next crawl)."""
    conn = db.connect()
    folded = 0
    for ak in [k for k in alias_keys.split(_ALIAS_DELIM) if k]:
        db.add_alias(conn, ak, canonical_key, canonical_type or None, note="curated")
        folded += 1
    db.apply_aliases(conn)
    db.rebuild_product_rollup(conn)  # grouping changed -> refresh the fast-path table
    conn.close()
    _invalidate_caches()  # merges change cross-supplier grouping -> search/quality/stats
    return _merge_redirect(kind if kind in ("conflict", "fuzzy") else "conflict", page, folded)


@app.post("/quality/merge/reject")
def quality_merge_reject(sig: str = Form(...), kind: str = Form("conflict"),
                         page: int = Form(1)):
    """Mark a cluster 'not the same' so it stops being proposed."""
    conn = db.connect()
    db.add_rejection(conn, sig)
    db.rebuild_product_rollup(conn)  # keep the fast-path table current with the queue
    conn.close()
    _invalidate_caches()
    return _merge_redirect(kind if kind in ("conflict", "fuzzy") else "conflict", page, 0)


@app.post("/quality/alias/remove")
def quality_alias_remove(alias_key: str = Form(...)):
    """Undo a confirmed merge. The fold is only reversed on the next crawl/reclassify
    (we can't un-rewrite the rows in place), so note that in the UI."""
    conn = db.connect()
    db.remove_alias(conn, alias_key)
    db.rebuild_product_rollup(conn)  # unmerge changes grouping -> refresh the fast-path table
    conn.close()
    _invalidate_caches()
    return RedirectResponse("/quality", status_code=303)


@app.get("/quality/types", response_class=HTMLResponse)
def quality_types(request: Request):
    """Classifier audit: type distribution plus the Other/Accessory buckets — the
    worklist for spotting real stones the rules missed (add them to normalize.py)."""
    conn = db.connect()
    stats = db.stats(conn)
    ctx = {
        "request": request,
        "stats": stats,
        "by_type": stats["by_type"],
        "other": dedupe.other_samples(conn, "Other", limit=80),
        "accessory": dedupe.other_samples(conn, "Accessory / Non-Slab", limit=40),
        "qs": db.quality_stats(conn),
    }
    conn.close()
    return templates.TemplateResponse(request, "quality_types.html", ctx)


def discovery_status(*, probed: bool, items: int, error: str) -> str:
    """Which triage bucket a discovery candidate belongs in (a triage rejection wins earlier).

    The ERROR is examined before the item count, and that order is the whole point. Six
    SlabWare hosts carry items > 0 alongside a 403 on their latest attempt, because a failed
    crawl leaves the previous rows in place — up to 18 days old. Counting those as "live"
    told this page SlabWare had 9 working catalogs when it had 3, which is the opposite of
    what a triage view is for.

    A pure function so the ordering is testable; the route only maps the result to a list.
    """
    from ..robots import is_block_error
    if not probed:
        return "unprobed"
    if is_block_error(error):
        # Declined, not broken. Filing a supplier who told us not to crawl them under
        # "errored" invites someone to go fix it.
        return "blocked"
    if error:
        return "broken"
    return "live" if items > 0 else "empty"


@app.get("/discovery", response_class=HTMLResponse)
def discovery(request: Request):
    """Triage the discovery pipeline: which suppliers.json candidates are live public
    catalogs, which came back empty (private/login-gated), which errored, and which
    haven't been probed yet. Turns fire-and-forget discovery into a curation queue."""
    from .. import discover
    conn = db.connect()
    entries = discover.load_suppliers()
    rows = {r["host"]: dict(r) for r in conn.execute(
        "SELECT host, company, item_count, slab_count, last_crawled, last_error FROM suppliers")}
    from datetime import date
    today = date.today()
    cats: dict[str, list] = {"unprobed": [], "empty": [], "broken": [], "blocked": [],
                             "live": [], "rejected": []}
    by_provider: dict[str, dict] = {}
    for e in entries:
        r = rows.get(e["host"])
        rec = {
            "host": e["host"],
            "name": e.get("name") or (r or {}).get("company") or "",
            "provider": e.get("provider") or "stoneprofits",
            "items": (r or {}).get("item_count") or 0,
            "slabs": (r or {}).get("slab_count") or 0,
            "last_crawled": (r or {}).get("last_crawled"),
            "error": (r or {}).get("last_error") or "",
        }
        # A triage rejection takes priority over the crawl-status buckets: the host was
        # judged not worth probing, so it belongs in its own list, not under "errored".
        try:
            rej = discover.Rejection.from_entry(e)
        except ValueError as ex:
            rej = None
            rec.update(status="rejected", reason=f"⚠ invalid rejected block: {ex}",
                       rejected_at="", days_left=None, active=True)
        if rej:
            rec.update(status="rejected", reason=rej.reason, rejected_at=rej.at.isoformat(),
                       days_left=rej.days_until_lapse(today), active=rej.is_active(today))
        else:
            rec["status"] = discovery_status(probed=r is not None, items=rec["items"],
                                             error=rec["error"])
        cats[rec["status"]].append(rec)
        p = by_provider.setdefault(rec["provider"],
                                   {"provider": rec["provider"], "total": 0, "live": 0, "materials": 0})
        p["total"] += 1
        p["materials"] += rec["items"]
        if rec["status"] == "live":
            p["live"] += 1
    cats["live"].sort(key=lambda r: -r["items"])
    for k in ("unprobed", "empty", "broken", "blocked", "rejected"):
        cats[k].sort(key=lambda r: r["host"])
    stats = db.stats(conn)
    conn.close()
    return templates.TemplateResponse(request, "discovery.html", {
        "request": request,
        "cats": cats,
        "counts": {k: len(v) for k, v in cats.items()},
        "by_provider": sorted(by_provider.values(), key=lambda p: -p["total"]),
        "total": len(entries),
        "unprobed_hosts": ",".join(r["host"] for r in cats["unprobed"]),
        "stats": stats,
    })


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request):
    """Saved searches with live match counts + how many are new arrivals."""
    conn = db.connect()
    items = []
    for w in db.list_watchlist(conn):
        total, rows, _p = _search(
            conn, q=w["query"], material_type="", color="", thickness="", supplier="",
            limit=6, offset=0,
        )
        new_total, _nr, _np = _search(
            conn, q=w["query"], material_type="", color="", thickness="", supplier="",
            new_only=True, limit=1, offset=0,
        )
        items.append({"watch": w, "total": total, "preview": rows, "new_count": new_total})
    # Turn the passive watchlist into an active digest: what changed across the whole
    # catalog since each supplier's previous crawl (restocks + brand-new listings).
    restock, listed, has_baseline = _stock_changes(conn)
    digest = {"restock": restock[:8], "listed": listed[:8],
              "n_restock": len(restock), "n_listed": len(listed),
              "has_baseline": has_baseline}
    stats = db.stats(conn)
    conn.close()
    return templates.TemplateResponse(request, "watchlist.html",
                                      {"items": items, "stats": stats, "digest": digest})


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/lists", response_class=HTMLResponse)
def lists_page(request: Request):
    conn = db.connect()
    ctx = {"lists": db.get_lists(conn), "stats": db.stats(conn)}
    conn.close()
    return templates.TemplateResponse(request, "lists.html", ctx)


@app.post("/lists/create")
def lists_create(name: str = Form(""), material_id: int = Form(0)):
    conn = db.connect()
    lid = db.create_list(conn, name or "Untitled list", _now())
    if material_id:
        db.add_to_list(conn, lid, material_id, _now())
    conn.close()
    return RedirectResponse(f"/list?id={lid}", status_code=303)


@app.post("/lists/add")
def lists_add(material_id: int = Form(...), list_id: int = Form(...),
              back: str = Form("/")):
    """Add a material to a list from any result row; return where the user was."""
    conn = db.connect()
    added = db.add_to_list(conn, list_id, material_id, _now())
    conn.close()
    # `back` is submitted data: only ever bounce to a path on this app.
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}added={1 if added else 0}&list={list_id}",
                            status_code=303)


@app.post("/lists/remove")
def lists_remove(item_id: int = Form(...), list_id: int = Form(...)):
    conn = db.connect()
    db.remove_list_item(conn, item_id)
    conn.close()
    return RedirectResponse(f"/list?id={list_id}", status_code=303)


@app.post("/lists/note")
def lists_note(item_id: int = Form(...), list_id: int = Form(...), note: str = Form("")):
    conn = db.connect()
    db.set_item_note(conn, item_id, note)
    conn.close()
    return RedirectResponse(f"/list?id={list_id}", status_code=303)


@app.post("/lists/delete")
def lists_delete(list_id: int = Form(...)):
    conn = db.connect()
    db.delete_list(conn, list_id)
    conn.close()
    return RedirectResponse("/lists", status_code=303)


# --- Compare -----------------------------------------------------------------

def _safe_back(back: str) -> str:
    """A submitted `back` is user data: only ever bounce to a path on this app."""
    return back if (back.startswith("/") and not back.startswith("//")) else "/"


@app.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request, near: str = ""):
    """Side-by-side comparison of the canonical materials in the compare tray."""
    conn = db.connect()
    origin, near_label = _resolve_near(near)
    cols, winners = _compare_columns(conn, origin)
    conn.close()
    return templates.TemplateResponse(request, "compare.html", {
        "cols": cols, "winners": winners, "near": near, "near_label": near_label,
        "near_ok": bool(origin), "max": db.COMPARE_MAX,
    })


@app.get("/compare/print", response_class=HTMLResponse)
def compare_print(request: Request, near: str = ""):
    """Print-optimized board of the comparison — the same columns and rows, minus the
    nav/tray/checkbox chrome. Browser Print → Save as PDF, like the sourcing-list board."""
    conn = db.connect()
    origin, near_label = _resolve_near(near)
    cols, winners = _compare_columns(conn, origin)
    conn.close()
    return templates.TemplateResponse(request, "compare_print.html", {
        "cols": cols, "winners": winners, "near_ok": bool(origin),
        "near_label": near_label, "printed": _now()[:10],
    })


@app.post("/compare/toggle")
def compare_toggle(material_key: str = Form(...), item_name: str = Form(""),
                   image_url: str = Form(""), on: str = Form(""),
                   back: str = Form("/"), ajax: str = Form("")):
    """Set a material's tray membership to the checkbox's new state: `on` present → add
    (subject to the 4-item cap), absent → remove. Answers with JSON for the background
    (no-reload) path, or a 303 back to the originating page for the no-JS fallback."""
    conn = db.connect()
    if on:
        status = db.add_to_compare(conn, material_key, item_name, image_url, _now())
    else:
        db.remove_from_compare(conn, material_key)
        status = "removed"
    tray = db.get_compare(conn)
    conn.close()
    if ajax:
        return JSONResponse({"status": status, "count": len(tray), "tray": tray})
    back = _safe_back(back)
    if status == "full":  # surface the refusal on reload for the no-JS path
        sep = "&" if "?" in back else "?"
        back = f"{back}{sep}cmp=full"
    return RedirectResponse(back, status_code=303)


@app.post("/compare/clear")
def compare_clear(back: str = Form("/"), ajax: str = Form("")):
    conn = db.connect()
    db.clear_compare(conn)
    conn.close()
    if ajax:
        return JSONResponse({"status": "cleared", "count": 0, "tray": []})
    return RedirectResponse(_safe_back(back), status_code=303)


def _group_by_supplier(items: list[dict]) -> list[dict]:
    groups: dict[int, dict] = {}
    for it in items:
        g = groups.setdefault(it["supplier_id"], {
            "supplier_id": it["supplier_id"], "supplier_name": it["supplier_name"],
            "supplier_host": it["supplier_host"], "email": it["supplier_email"],
            "phone": it["supplier_phone"], "items": [],
        })
        g["items"].append(it)
    return sorted(groups.values(), key=lambda g: (g["supplier_name"] or "").lower())


@app.get("/list", response_class=HTMLResponse)
def list_view(request: Request, id: int, added: int = -1):
    conn = db.connect()
    meta = db.get_list(conn, id)
    if not meta:
        conn.close()
        return HTMLResponse("<p style='padding:40px;font-family:sans-serif'>List not found. "
                            "<a href='/lists'>All lists</a></p>", status_code=404)
    items = db.get_list_items(conn, id)
    ctx = {
        "meta": meta, "items": items, "groups": _group_by_supplier(items),
        "lists": db.get_lists(conn), "stats": db.stats(conn), "added": added,
    }
    conn.close()
    return templates.TemplateResponse(request, "list.html", ctx)


@app.get("/list/print", response_class=HTMLResponse)
def list_print(request: Request, id: int):
    """Print-optimized board: the browser's Print dialog saves it as a PDF to send
    to a client. The desktop app has no server to host a shareable URL."""
    conn = db.connect()
    meta = db.get_list(conn, id)
    if not meta:
        conn.close()
        return HTMLResponse("Not found", status_code=404)
    items = db.get_list_items(conn, id)
    conn.close()
    return templates.TemplateResponse(request, "list_print.html", {
        "meta": meta, "items": items, "printed": _now()[:10],
    })


@app.get("/list/export")
def list_export(id: int):
    """A sourcing list as a spreadsheet: one row per item, with live stock + contacts."""
    conn = db.connect()
    meta = db.get_list(conn, id)
    if not meta:
        conn.close()
        return Response("List not found", status_code=404)
    items = db.get_list_items(conn, id)
    conn.close()
    header = ["Supplier", "Material", "Thickness", "Slabs now", "Status",
              "Note", "Email", "Phone"]
    out = [[
        it.get("supplier_name", ""), it.get("item_name", ""), it.get("thickness", ""),
        it.get("live_slabs") or 0, "gone" if it.get("gone") else "in catalog",
        it.get("note", ""), it.get("supplier_email", ""), it.get("supplier_phone", ""),
    ] for it in items]
    slug = re.sub(r"[^a-z0-9]+", "-", (meta["name"] or "list").lower()).strip("-") or "list"
    return _csv_response(f"{slug}.csv", header, out)


@app.get("/list/rfq", response_class=HTMLResponse)
def list_rfq(request: Request, id: int):
    """One quote request per supplier: a prefilled mailto plus a printable sheet."""
    conn = db.connect()
    meta = db.get_list(conn, id)
    if not meta:
        conn.close()
        return HTMLResponse("Not found", status_code=404)
    items = db.get_list_items(conn, id)
    conn.close()
    groups = _group_by_supplier(items)
    for g in groups:
        lines = [f'- {i["item_name"]}'
                 + (f' ({i["thickness"]})' if i["thickness"] else "")
                 + (f' [{i["note"]}]' if i["note"] else "")
                 for i in g["items"]]
        body = (f'Hello {g["supplier_name"] or ""},\n\n'
                f'We are sourcing material for a project and would like a quote and '
                f'current availability on the following:\n\n' + "\n".join(lines)
                + '\n\nPlease include price, slab dimensions and lead time.\n\nThank you.')
        g["subject"] = f'Quote request — {meta["name"]}'
        g["body"] = body
    return templates.TemplateResponse(request, "list_rfq.html", {
        "meta": meta, "groups": groups,
    })


@app.post("/watchlist/add")
def watchlist_add(q: str = Form("")):
    # The Save-search button submits the filter form, so the query arrives as form
    # data, not a query param.
    if q.strip():
        conn = db.connect()
        db.add_watch(conn, q, _now())
        conn.close()
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/remove")
def watchlist_remove(id: int = Form(...)):
    conn = db.connect()
    db.remove_watch(conn, id)
    conn.close()
    return RedirectResponse("/watchlist", status_code=303)
