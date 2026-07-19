"""Local search UI over the aggregated material database.

Run from the project root:

    uvicorn stonescan.web.app:app --reload

Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db, dedupe, geocode, imagesearch, slabs, smartsearch

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

# Card image: prefer a real product photo over a SlabCloud thumbnail. SlabCloud tenants
# reuse a shared "coming soon" graphic (in many byte-variants) for un-photographed slabs,
# so whenever a material also has a Stone Profits photo, that real photo should win; the
# SlabCloud thumb is used only when it's the material's sole image.
_CARD_IMG = ("COALESCE("
             "MAX(CASE WHEN COALESCE(m.image_url,'') <> '' "
             "AND m.image_url NOT LIKE '%slabcloud.com/slabs/%' THEN m.image_url END), "
             "MAX(m.image_url))")

# Total in-stock SLAB area. Sum each slab listing's own slabs×length×width BEFORE
# aggregating (a group spans suppliers/variants, so MAX(length)×MAX(width) would
# cross-multiply dims from different slabs); tiles contribute no slab area.
_SQFT = (f"SUM(CASE WHEN {_IS_TILE} THEN 0 ELSE COALESCE(m.available_slabs,0) "
         "* COALESCE(m.avg_length,0) * COALESCE(m.avg_width,0) END) / 144.0")

SORTS = {
    # Material-centric relevance: surface the materials the market actually carries
    # widely (many suppliers, lots of slab stock) before one-off/granular SKUs.
    "relevance": f"COUNT(DISTINCT m.supplier_id) DESC, {_SLABS} DESC, m.material_key",
    "slabs": f"{_SLABS} DESC, m.material_key",
    "size": "MAX(m.avg_length) DESC, m.material_key",
    "new": "MAX(m.new_arrival) DESC, m.material_key",
    "area": f"{_SQFT} DESC, m.material_key",
    "distance": f"miles ASC, {_SLABS} DESC",  # only valid when near-active
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


def _search(conn, *, q, material_type, color, thickness, supplier, limit, offset,
            location="", min_length=0.0, min_width=0.0, new_only=False,
            sort="relevance", in_stock=False, fuzzy=True,
            near=None, radius_mi=0.0, min_sqft=0.0):
    """Free-text-aware search. The query box understands phrases like
    'blue marble slabs'; explicit dropdown filters take precedence."""
    parsed = smartsearch.parse_query(q)
    where, params = ["1=1"], []

    mt = material_type or parsed["material_type"]
    if mt:
        where.append("m.material_type = ?"); params.append(mt)

    # The catalog is stone/tile materials only — never surface the accessory/non-slab
    # bucket (sinks, tools, chemicals, PPE). It stays in the DB for the Quality audit.
    if mt != "Accessory / Non-Slab":
        where.append("m.material_type <> 'Accessory / Non-Slab'")

    if color:  # explicit dropdown -> exact
        where.append("m.color = ?"); params.append(color)
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

    if supplier:
        where.append("(s.company = ? OR s.host = ?)"); params.extend([supplier, supplier])

    if location:
        where.append("m.locations LIKE ?"); params.append(f"%{location}%")
    if min_length:
        where.append("m.avg_length >= ?"); params.append(min_length)
    if min_width:
        where.append("m.avg_width >= ?"); params.append(min_width)
    if new_only:
        where.append("m.new_arrival = 1")

    # Proximity: keep only material with a stocking location inside the radius.
    near_active = bool(near) and radius_mi > 0
    if near_active:
        loc_miles = geocode.locations_within(conn, near[0], near[1], radius_mi)
        _register_nearest(conn, loc_miles)
        where.append("nearest_miles(m.locations) IS NOT NULL")

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
    where, params = base_where + tw, base_params + tp

    # Group ACROSS suppliers: one row per material (its cross-supplier material_key),
    # so "Calacatta Gold" is a single card carrying a supplier count + a total slab
    # count — not one row per supplier/variant. The handful of empty-key materials
    # stay individual (grouped on their own id) rather than collapsing together.
    group_by = ("CASE WHEN COALESCE(m.material_key, '') = '' "
                "THEN 'id:' || m.id ELSE m.material_key END")
    # Aggregates (slab total, total area) are grouped, so filter them at HAVING time.
    have = []
    if in_stock or min_sqft:  # needing enough area implies needing stock
        have.append(f"({_SLABS} > 0 OR {_HAS_TILE} = 1)")
    if min_sqft:
        have.append(f"{_SQFT} >= ?")
    having = (" HAVING " + " AND ".join(have)) if have else ""
    having_params = [min_sqft] if min_sqft else []

    if sort == "distance" and not near_active:
        sort = "relevance"  # distance is meaningless without an origin
    order_by = SORTS.get(sort, SORTS["relevance"])

    def count(clause, ps):
        return conn.execute(
            f"SELECT COUNT(*) c FROM (SELECT 1 FROM materials m JOIN suppliers s "
            f"ON s.id=m.supplier_id WHERE {clause} GROUP BY {group_by}{having})",
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
            fclause = " AND ".join(base_where + tw)
            fparams = base_params + tp
            ftotal = count(fclause, fparams)
            if ftotal > total:
                clause, params, total = fclause, fparams, ftotal
                parsed["fuzzy"] = sorted({w for v in variants for w in v[1:]})

    # miles = distance to the NEAREST stocking supplier for this material.
    miles_col = ", MIN(nearest_miles(m.locations)) AS miles" if near_active else ""
    rows = conn.execute(
        f"""SELECT MIN(m.id) AS id, COALESCE(m.material_key, '') AS material_key,
                   MIN(m.item_name) AS item_name, MAX(m.material_type) AS material_type,
                   MAX(m.color) AS color, MAX(m.new_arrival) AS new_arrival,
                   COUNT(DISTINCT m.supplier_id) AS suppliers,
                   {_SLABS} AS available_slabs, {_TILE_SF} AS tile_sf, {_HAS_TILE} AS has_tile,
                   MAX(m.avg_length) AS avg_length, MAX(m.avg_width) AS avg_width,
                   {_SQFT} AS total_sqft,
                   {_CARD_IMG} AS image_url{miles_col}
            FROM materials m JOIN suppliers s ON s.id = m.supplier_id
            WHERE {clause}
            GROUP BY {group_by}{having}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?""",
        (*params, *having_params, limit, offset),
    ).fetchall()
    return total, [dict(r) for r in rows], parsed


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


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    material_type: str = "",
    color: str = "",
    thickness: str = "",
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
        thickness=thickness, supplier=supplier, location=location,
        min_length=ml, min_width=mw, new_only=bool(new_only),
        sort=sort, in_stock=bool(in_stock),
        near=origin, radius_mi=radius_mi if origin else 0.0,
        min_sqft=_to_float(min_sqft),
        limit=limit, offset=offset,
    )
    _attach_rep_photos(conn, rows)
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
        "radius": radius or (str(int(radius_mi)) if near else ""),
        "min_sqft": min_sqft or "",
        "view": view,
        "lists": db.get_lists(conn),
        "added": added,
        "added_list": list,
        # Where an add-to-list POST should send the user back to (path only, so the
        # redirect stays on this host).
        "here": _here(request),
        "types": _distinct(conn, "material_type"),
        "colors": _distinct(conn, "color"),
        "thicknesses": _distinct(conn, "thickness"),
        "locations": db.distinct_locations(conn),
        "suppliers": [
            dict(r) for r in conn.execute(
                "SELECT COALESCE(NULLIF(company,''),host) AS name, item_count "
                "FROM suppliers WHERE item_count > 0 ORDER BY name"
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
        "slabs": sum(r["available_slabs"] or 0 for r in rows
                     if "TILE" not in (r["product_form"] or "").upper()),
        "tile_sf": sum(r["available_slabs"] or 0 for r in rows
                       if "TILE" in (r["product_form"] or "").upper()
                       and (r["uom"] or "").upper() == "SF"),
        "has_tile": any("TILE" in (r["product_form"] or "").upper() for r in rows),
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
    similar = [dict(x) for x in conn.execute(
        """SELECT m.item_name, m.material_key, m.color, MAX(m.image_url) AS image_url,
                  COUNT(DISTINCT m.supplier_id) AS suppliers, SUM(m.available_slabs) AS slabs
           FROM materials m
           WHERE m.material_type = ? AND m.material_key <> ? AND m.material_key <> ''
                 AND (? = '' OR m.color = ?)
           GROUP BY m.material_key
           ORDER BY (MAX(m.image_url) = '' OR MAX(m.image_url) IS NULL),
                    SUM(m.available_slabs) DESC
           LIMIT 12""",
        (mtype, key, rows[0]["color"] or "", rows[0]["color"] or ""),
    )]
    lists = db.get_lists(conn)
    conn.close()
    return templates.TemplateResponse(request, "material.html", {
        "rows": rows, "name": name, "key": key, "suppliers_list": suppliers_list,
        "facts": facts, "photos": photos, "rep_photo": rep_photo, "similar": similar,
        "lists": lists, "added": added, "added_list": list,
    })


@app.get("/item", response_class=HTMLResponse)
def item(request: Request, id: int, added: int = -1, list: int = 0):
    """Detail view for one material at one supplier (StoneProfits-style drill-down)."""
    conn = db.connect()
    m = conn.execute(
        """SELECT m.*, COALESCE(NULLIF(s.company,''), s.host) AS supplier_name,
                  s.host AS supplier_host, s.products AS supplier_products,
                  s.last_crawled AS supplier_updated,
                  s.phone AS supplier_phone, s.email AS supplier_email
           FROM materials m JOIN suppliers s ON s.id = m.supplier_id
           WHERE m.id = ?""",
        (id,),
    ).fetchone()
    if not m:
        conn.close()
        return HTMLResponse("<p style='padding:40px;font-family:sans-serif'>Item not found. "
                            "<a href='/'>Back to search</a></p>", status_code=404)
    m = dict(m)
    # Every supplier that carries this material (one row per supplier).
    others = conn.execute(
        """SELECT COALESCE(NULLIF(s.company,''), s.host) AS supplier_name, s.host AS supplier_host,
                  mm.id AS id, mm.item_id AS item_id, mm.source_url AS source_url,
                  g.variants AS variants, g.slabs AS slabs, g.image_url AS image_url
           FROM (SELECT supplier_id, MIN(id) AS min_id, COUNT(*) AS variants,
                        MAX(available_slabs) AS slabs, MAX(image_url) AS image_url
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
        """SELECT MIN(id) AS id, item_name, thickness, finish, product_form,
                  SUM(available_slabs) AS available_slabs, MAX(image_url) AS image_url
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
    similar = conn.execute(
        """SELECT MIN(m.id) AS id, m.item_name, m.material_type, m.color, m.material_key,
                  MAX(m.image_url) AS image_url,
                  COUNT(DISTINCT m.supplier_id) AS suppliers,
                  SUM(m.available_slabs) AS slabs
           FROM materials m
           WHERE m.material_type = ? AND m.material_key <> ? AND m.material_key <> ''
                 AND (m.color = ? OR (? <> '' AND m.name_norm LIKE ?))
           GROUP BY m.material_key
           ORDER BY (MAX(m.image_url) = '' OR MAX(m.image_url) IS NULL),
                    SUM(m.available_slabs) DESC
           LIMIT 12""",
        (m["material_type"], m["material_key"], m["color"] or "\0",
         first_word, f"{first_word}%"),
    ).fetchall()
    lists_avail = db.get_lists(conn)
    conn.close()
    return templates.TemplateResponse(
        request, "item.html",
        {"m": m, "others": [dict(o) for o in others],
         "variants": [dict(v) for v in variants], "similar": [dict(x) for x in similar],
         "lists": lists_avail, "added": added, "added_list": list},
    )


@app.get("/api/slabs")
async def api_slabs(id: int):
    """Per-slab gallery for a material: pre-cached from the nightly crawl if
    available, otherwise fetched live from the supplier's catalog."""
    conn = db.connect()
    row = conn.execute(
        """SELECT m.item_id, m.supplier_id, m.name_norm, m.thickness, m.finish,
                  s.host, s.image_base
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
    ph = ",".join("?" for _ in item_ids)
    cached = conn.execute(
        f"""SELECT slab_no, location, length, width, qty, uom, barcode, image_url
            FROM slabs WHERE supplier_id = ? AND item_id IN ({ph})
            ORDER BY (image_url = '' OR image_url IS NULL), slab_no""",
        (row["supplier_id"], *item_ids),
    ).fetchall()
    cached = [dict(c) for c in cached]
    conn.close()
    if cached:
        slab_list = [{
            "photo": c["image_url"] or "", "thumb": c["image_url"] or "",
            "slab_no": c["slab_no"], "location": c["location"],
            "length": c["length"], "width": c["width"],
            "qty": c["qty"], "uom": c["uom"], "barcode": c["barcode"],
        } for c in cached]
        return JSONResponse({"slabs": slab_list, "cached": True})

    # Not pre-cached (or nightly crawl didn't include slabs) -> fetch live.
    try:
        found = await slabs.fetch_slabs(row["host"], row["item_id"], row["image_base"] or "")
        return JSONResponse({"slabs": found, "cached": False})
    except Exception as e:  # noqa: BLE001 - never 500 the detail page over a gallery fetch
        return JSONResponse({"slabs": [], "error": str(e)})


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
        # Materials only by default — slab galleries load on demand (see /api/slabs),
        # so prefetching every gallery would turn a click into a multi-hour job now
        # that the catalog is ~130k materials. `with_slabs` is the opt-in deep refresh.
        asyncio.run(run_all(
            entries, concurrency=4, delay=1.0, headless=True,
            db_path=str(db.DEFAULT_DB), with_slabs=with_slabs,
            retry_errored=True, progress=progress,
        ))
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
    supplier: str = "", location: str = "", min_length: str = "", min_width: str = "",
    new_only: int = 0, in_stock: int = 0, sort: str = "relevance",
    near: str = "", radius: str = "", min_sqft: str = "",
    limit: int = Query(100, le=500), offset: int = 0,
):
    conn = db.connect()
    origin, near_label = _resolve_near(near)
    total, rows, parsed = _search(
        conn, q=q, material_type=material_type, color=color, thickness=thickness,
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


@app.get("/photo", response_class=HTMLResponse)
def photo_page(request: Request, material_type: str = ""):
    """Search-by-photo: upload a stone photo, get visually-similar catalog materials."""
    conn = db.connect()
    ctx = {
        "request": request, "results": [], "queried": False, "error": "", "query_uri": "",
        "material_type": material_type, "model_ready": imagesearch.available(),
        "index": imagesearch.index_stats(conn), "types": _distinct(conn, "material_type"),
        "stats": db.stats(conn),
    }
    conn.close()
    return templates.TemplateResponse(request, "photo.html", ctx)


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
    ctx = {
        "request": request, "results": results, "queried": True, "error": error,
        "query_uri": query_uri, "material_type": material_type,
        "model_ready": imagesearch.available(), "index": imagesearch.index_stats(conn),
        "types": _distinct(conn, "material_type"), "stats": db.stats(conn),
    }
    conn.close()
    return templates.TemplateResponse(request, "photo.html", ctx)


def _restock_since_last(conn) -> list[dict]:
    """Products newly in stock at the latest snapshot vs the previous one."""
    dates = [r["snapshot_date"] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM history ORDER BY snapshot_date DESC LIMIT 2"
    )]
    if len(dates) < 2:
        return []
    latest, prev = dates[0], dates[1]
    rows = conn.execute(
        """SELECT h.name_norm, h.material_type, h.color, h.thickness, h.slabs, h.image_url,
                  COALESCE(NULLIF(s.company,''), s.host) AS supplier_name, s.host AS supplier_host,
                  (SELECT MIN(mm.id) FROM materials mm
                     WHERE mm.supplier_id = h.supplier_id AND mm.name_norm = h.name_norm
                           AND mm.thickness = h.thickness) AS id
           FROM history h JOIN suppliers s ON s.id = h.supplier_id
           WHERE h.snapshot_date = ? AND h.slabs > 0
                 AND NOT EXISTS (
                     SELECT 1 FROM history p WHERE p.snapshot_date = ?
                       AND p.supplier_id = h.supplier_id AND p.name_norm = h.name_norm
                       AND p.thickness = h.thickness AND p.slabs > 0)
           ORDER BY h.slabs DESC LIMIT 300""",
        (latest, prev),
    ).fetchall()
    return [dict(r) for r in rows if r["id"]]


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
    restock = _restock_since_last(conn)
    stats = db.stats(conn)
    conn.close()
    return templates.TemplateResponse(request, "whatsnew.html", {
        "rows": rows, "total": total, "page": page, "pages": (total + limit - 1) // limit,
        "restock": restock, "stats": stats,
    })


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
    order = {"broken": 0, "stale": 1, "empty": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r["status"]], -(r["item_count"] or 0)))
    counts = {k: sum(1 for r in rows if r["status"] == k)
              for k in ("ok", "stale", "broken", "empty")}
    stats = db.stats(conn)
    conn.close()
    return templates.TemplateResponse(request, "health.html", {
        "rows": rows, "counts": counts, "stats": stats, "total": len(rows),
    })


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
    qs = db.quality_stats(conn)
    conflicts = dedupe.conflict_clusters(conn, limit=100000)
    fuzzy = dedupe.fuzzy_clusters(conn, limit=100000)
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
    conn.close()
    return _merge_redirect(kind if kind in ("conflict", "fuzzy") else "conflict", page, folded)


@app.post("/quality/merge/reject")
def quality_merge_reject(sig: str = Form(...), kind: str = Form("conflict"),
                         page: int = Form(1)):
    """Mark a cluster 'not the same' so it stops being proposed."""
    conn = db.connect()
    db.add_rejection(conn, sig)
    conn.close()
    return _merge_redirect(kind if kind in ("conflict", "fuzzy") else "conflict", page, 0)


@app.post("/quality/alias/remove")
def quality_alias_remove(alias_key: str = Form(...)):
    """Undo a confirmed merge. The fold is only reversed on the next crawl/reclassify
    (we can't un-rewrite the rows in place), so note that in the UI."""
    conn = db.connect()
    db.remove_alias(conn, alias_key)
    conn.close()
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
    cats: dict[str, list] = {"unprobed": [], "empty": [], "broken": [], "live": []}
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
        if r is None:
            rec["status"] = "unprobed"
        elif rec["items"] > 0:
            rec["status"] = "live"
        elif rec["error"]:
            rec["status"] = "broken"
        else:
            rec["status"] = "empty"
        cats[rec["status"]].append(rec)
        p = by_provider.setdefault(rec["provider"],
                                   {"provider": rec["provider"], "total": 0, "live": 0, "materials": 0})
        p["total"] += 1
        p["materials"] += rec["items"]
        if rec["status"] == "live":
            p["live"] += 1
    cats["live"].sort(key=lambda r: -r["items"])
    for k in ("unprobed", "empty", "broken"):
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
    stats = db.stats(conn)
    conn.close()
    return templates.TemplateResponse(request, "watchlist.html", {"items": items, "stats": stats})


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
