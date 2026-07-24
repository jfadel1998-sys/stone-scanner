"""SQLite storage for suppliers and normalized materials.

One database file (stonescan.db by default). Two tables:
  - suppliers: one row per crawled catalog
  - materials: one row per catalog item, with normalized fields for search

Materials are linked to a supplier and carry a `material_key` so the same
material offered by several suppliers can be grouped for comparison.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Writable DB location. Overridable via env so the packaged app can point at a
# per-user data dir (the bundle itself is read-only).
DEFAULT_DB = Path(os.environ.get("STONESCAN_DB") or (Path(__file__).resolve().parent.parent / "stonescan.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    host          TEXT UNIQUE NOT NULL,
    token         TEXT,
    company       TEXT,
    products      TEXT,
    image_base    TEXT,
    item_count    INTEGER DEFAULT 0,
    last_crawled  TEXT,
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS materials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    item_id         TEXT,
    item_name       TEXT NOT NULL,
    name_norm       TEXT,          -- normalized display name
    material_key    TEXT,          -- canonical key for cross-supplier matching
    material_type   TEXT,          -- Granite / Marble / Quartz / Quartzite / Porcelain / ...
    category        TEXT,          -- raw CategoryName from the catalog
    subcategory     TEXT,          -- raw SubCategoryName
    color           TEXT,
    finish          TEXT,
    thickness       TEXT,          -- e.g. "3cm"
    product_form    TEXT,          -- SLAB / REMNANT / ...
    origin          TEXT,
    available_qty   REAL,
    available_slabs INTEGER,
    avg_length      REAL,
    avg_width       REAL,
    uom             TEXT,
    sku             TEXT,
    idone           TEXT,
    price_range     TEXT,
    new_arrival     INTEGER DEFAULT 0,
    image_filename  TEXT,
    image_url       TEXT,
    source_url      TEXT,
    crawled_at      TEXT,
    UNIQUE(supplier_id, item_id, idone)
);

CREATE INDEX IF NOT EXISTS idx_mat_key        ON materials(material_key);
CREATE INDEX IF NOT EXISTS idx_mat_type       ON materials(material_type);
CREATE INDEX IF NOT EXISTS idx_mat_name       ON materials(name_norm);
CREATE INDEX IF NOT EXISTS idx_mat_color      ON materials(color);
CREATE INDEX IF NOT EXISTS idx_mat_thickness  ON materials(thickness);
CREATE INDEX IF NOT EXISTS idx_mat_supplier   ON materials(supplier_id);

CREATE TABLE IF NOT EXISTS slabs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id    INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    item_id        TEXT NOT NULL,     -- catalog ItemID (links to materials.item_id)
    slab_no        TEXT,
    location       TEXT,
    length         REAL,
    width          REAL,
    qty            REAL,
    uom            TEXT,
    barcode        TEXT,
    image_filename TEXT,
    image_url      TEXT,
    crawled_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_slab_item ON slabs(supplier_id, item_id);
CREATE INDEX IF NOT EXISTS idx_slab_loc  ON slabs(location);

-- One row per product group per crawl date: enables trends, restock + new detection.
CREATE TABLE IF NOT EXISTS history (
    snapshot_date TEXT NOT NULL,
    supplier_id   INTEGER NOT NULL,
    material_key  TEXT,
    name_norm     TEXT,
    thickness     TEXT,
    material_type TEXT,
    color         TEXT,
    slabs         INTEGER,
    image_url     TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_date ON history(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_hist_key  ON history(material_key);
CREATE INDEX IF NOT EXISTS idx_hist_grp  ON history(supplier_id, name_norm, thickness);

-- Saved searches the user wants to keep an eye on.
CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL UNIQUE,
    created_at  TEXT
);

-- Curator-confirmed merges: a computed material_key (alias_key) is rewritten to a
-- canonical one so the same stone offered under a different spelling — or under a
-- different material_type across suppliers ("Taj Mahal" as granite vs quartzite) —
-- collapses into ONE canonical material. Keyed on the *computed* key (not a row id,
-- which is reassigned every crawl), so a merge survives re-crawls: apply_aliases()
-- re-collapses the freshly-normalized rows after each ingest/reclassify.
CREATE TABLE IF NOT EXISTS material_aliases (
    alias_key       TEXT PRIMARY KEY,   -- the material_key to fold away
    canonical_key   TEXT NOT NULL,      -- the material_key to fold it into
    canonical_type  TEXT,               -- if set, also override material_type on folded rows
    note            TEXT,
    created_at      TEXT
);

-- Pairs/clusters a curator marked "not the same", so the candidate generator stops
-- proposing them. `sig` is a stable signature ("conflict:<base>" for a type-conflict
-- cluster, "fuzzy:<keyA>||<keyB>" for a near-duplicate pair).
CREATE TABLE IF NOT EXISTS merge_rejections (
    sig         TEXT PRIMARY KEY,
    created_at  TEXT
);

-- Search-by-photo: one CLIP embedding per distinct catalog image. Keyed on the image
-- URL so a re-crawl that keeps the same image reuses its vector (no re-embed). `vec`
-- is a raw float32 blob; `imagesearch` reads it back with numpy.
CREATE TABLE IF NOT EXISTS image_vectors (
    image_url   TEXT PRIMARY KEY,
    vec         BLOB NOT NULL,
    updated_at  TEXT
);

-- Resolved coordinates per distinct slab location. A row with NULL lat/lon means
-- "we looked and it isn't a place" (an internal warehouse name), which is cached
-- so the map doesn't retry it every load.
CREATE TABLE IF NOT EXISTS location_geo (
    location    TEXT PRIMARY KEY,
    lat         REAL,
    lon         REAL,
    label       TEXT,
    source      TEXT,          -- override | exact | state | parsed | ambiguous
    updated_at  TEXT
);

-- Named sourcing lists: materials collected across suppliers for a job/client.
CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT
);

-- A list entry keys off (supplier_id, item_id) — the only identity that survives
-- a re-crawl, since materials.id is reassigned every time. The name/photo/spec
-- snapshot keeps the list readable even if the item later leaves the catalog.
CREATE TABLE IF NOT EXISTS list_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id      INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    supplier_id  INTEGER,
    item_id      TEXT,
    supplier_host TEXT,
    supplier_name TEXT,
    item_name    TEXT,
    material_key TEXT,
    thickness    TEXT,
    finish       TEXT,
    image_url    TEXT,
    note         TEXT,
    added_at     TEXT,
    UNIQUE(list_id, supplier_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_listitem_list ON list_items(list_id);

-- Compare tray: the small set of canonical materials the user is weighing side by side
-- on /compare, keyed on material_key. One global tray — this is the single-user desktop
-- app, so no per-user scoping. Capped at COMPARE_MAX in add_to_compare(). Snapshots
-- name/photo like list_items so a column still renders if the material later leaves every
-- catalog (the /compare column then reads "no longer listed").
CREATE TABLE IF NOT EXISTS compare_tray (
    material_key TEXT PRIMARY KEY,
    item_name    TEXT,
    image_url    TEXT,
    added_at     TEXT
);

-- Seen catalog-change alerts: one row per acknowledged change event, keyed by a stable
-- signature (kind|supplier_id|name_norm|thickness|latest_snapshot_date). The /alerts page
-- and the nav badge treat any current digest change whose signature is absent here as
-- unread; opening /alerts inserts the shown signatures. A later crawl that re-detects the
-- same product change stamps a new latest_snapshot_date, hence a new signature — so it is
-- unread again. Derived/prunable: safe to clear.
CREATE TABLE IF NOT EXISTS alert_seen (
    sig      TEXT PRIMARY KEY,
    seen_at  TEXT
);

-- Materialized per-product rollup: one row per search 'grp' (material_key, or
-- 'id:<id>' for empty-key products), holding the cross-supplier aggregates the
-- search's outer query computes. Lets the default/type browse be an index scan
-- instead of a two-level GROUP BY over the whole materials table. A derived cache:
-- safe to drop and rebuild, and rebuilt after every crawl / reclassify / merge.
CREATE TABLE IF NOT EXISTS product_rollup (
    grp             TEXT PRIMARY KEY,
    id              INTEGER,
    material_key    TEXT,
    item_name       TEXT,
    material_type   TEXT,
    color           TEXT,
    new_arrival     INTEGER,
    suppliers       INTEGER,
    available_slabs INTEGER,
    total_slabs     INTEGER,
    tile_sf         REAL,
    has_tile        INTEGER,
    avg_length      REAL,
    avg_width       REAL,
    total_sqft      REAL,
    image_url       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rollup_type ON product_rollup(material_type);
"""


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The packaged app runs the UI and a refresh as separate processes against one file.
    # WAL lets readers keep serving while a crawl writes; busy_timeout waits out the brief
    # writer locks instead of raising "database is locked" up through the request handlers.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
    except sqlite3.Error:  # read-only/locked media — fall back to defaults rather than fail
        pass
    return conn


# Columns added after the first release; applied to pre-existing databases.
_MIGRATIONS = {
    "suppliers": {"image_base": "TEXT", "slabs_cached_at": "TEXT", "slab_count": "INTEGER DEFAULT 0",
                  "phone": "TEXT", "email": "TEXT", "locations": "TEXT",
                  # A host that mirrors another's catalog (same tenant token, near-identical
                  # item set): mirror_of = the canonical host, mirror_source = computed|curated.
                  "mirror_of": "TEXT", "mirror_source": "TEXT",
                  # Consecutive zero-item crawls, for auto-rejecting dead discovery candidates.
                  "empty_streak": "INTEGER DEFAULT 0"},
    "materials": {"new_arrival": "INTEGER DEFAULT 0", "image_url": "TEXT", "locations": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()
    fixed = fix_mm_thickness(conn)
    if fixed:
        print(f"  thickness: repaired {fixed} millimetre-as-centimetre value(s).")


# A thickness written before normalize_thickness understood units: the old code appended
# 'cm' to a bare number, so a 12 mm porcelain slab was stored as '12cm'. The row still
# carries the catalog's own uom, so this is a deterministic repair, not a guess.
_THICK_CM_RE = re.compile(r"^\s*([\d.]+)\s*cm\s*$", re.IGNORECASE)
_MM_FIX_MIN_CM = 10.0    # no slab is >= 10cm thick — the signature of the old bug
_MM_FIX_LOW, _MM_FIX_HIGH = 0.1, 10.0   # plausible slab thickness, in cm


def fix_mm_thickness(conn: sqlite3.Connection) -> int:
    """Repair thicknesses stored as '<mm value>cm'. Returns the number of rows changed.

    Only touches rows where the catalog said the unit was MILLIMETRES *and* the stored
    value is >= 10cm — physically impossible for a slab, hence the fingerprint of the bug.
    Anything whose unit can't be proven (blank or non-mm uom) is left alone rather than
    guessed at.

    Deliberately self-limiting, which is what makes it safe to run on every startup: after
    the repair the value is < 10cm, so it no longer matches the guard and a second pass
    cannot turn '3cm' into '0.3cm'. (That re-run hazard is why this is a targeted repair
    rather than simply re-running normalize_thickness — the corrupted value already carries
    an explicit 'cm', which would win over the row's uom and leave it unchanged.)
    """
    rows = conn.execute(
        "SELECT id, thickness FROM materials "
        "WHERE LOWER(TRIM(COALESCE(uom, ''))) = 'mm' AND thickness LIKE '%cm'"
    ).fetchall()
    updates = []
    for r in rows:
        m = _THICK_CM_RE.match(r["thickness"] or "")
        if not m:
            continue
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        if val < _MM_FIX_MIN_CM:
            continue                      # already plausible — not a corrupted row
        cm = val / 10.0                   # the number was millimetres all along
        if not (_MM_FIX_LOW <= cm <= _MM_FIX_HIGH):
            continue                      # would still be implausible — leave it alone
        updates.append((f"{round(cm, 2):g}cm", r["id"]))
    if updates:
        conn.executemany("UPDATE materials SET thickness = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


def init_db(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    _ensure_fts(conn)
    conn.commit()
    return conn


def backup_database(db_path: str | Path = DEFAULT_DB) -> bool:
    """Write a consistent snapshot of the DB to `<db_path>.bak`.

    Checkpoints the WAL into the main file, then copies it. This is WAL-safe (the copy
    isn't a torn read of a live WAL database) and — unlike SQLite's page-by-page online
    backup API or `VACUUM INTO` (which rebuilds every index) — fast enough to run before
    every refresh even on a large DB on a slow disk: on the ~300 MB catalog on an external
    drive, both of those ran for minutes while a checkpoint+copy is ~15 s. Safe because a
    refresh takes the snapshot BEFORE it writes, so no writer is active; the app's reader
    connections don't block a file copy. The copy goes to a temp file and is atomically
    renamed over any existing `.bak`, so a failed backup never corrupts the previous good one.

    Returns True on success, or False when there's no DB to back up yet (first run). Raises
    on a real failure (disk full, permission) so the caller can warn and proceed rather
    than silently skip the safety net.
    """
    src = Path(db_path)
    if not src.exists():
        return False
    bak = src.with_name(src.name + ".bak")
    tmp = src.with_name(src.name + ".bak.tmp")
    ok = False
    try:
        # Flush committed WAL frames into the main file so the plain copy is complete.
        cx = sqlite3.connect(str(src))
        try:
            cx.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            cx.close()
        shutil.copy2(src, tmp)
        os.replace(tmp, bak)  # atomic within the same directory
        ok = True
        return True
    finally:
        if not ok and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def upsert_supplier(conn: sqlite3.Connection, host: str, **fields: Any) -> int:
    """Insert or update a supplier by host; return its id."""
    cols = {k: v for k, v in fields.items() if v is not None}
    conn.execute(
        "INSERT INTO suppliers (host) VALUES (?) ON CONFLICT(host) DO NOTHING", (host,)
    )
    if cols:
        assignments = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(
            f"UPDATE suppliers SET {assignments} WHERE host = ?",
            (*cols.values(), host),
        )
    conn.commit()
    row = conn.execute("SELECT id FROM suppliers WHERE host = ?", (host,)).fetchone()
    return int(row["id"])


def record_crawl_streak(conn: sqlite3.Connection, host: str, n_items: int,
                        error: str | None) -> None:
    """Track consecutive zero-item crawls per host, so discovery can auto-reject dead
    candidates (see discover.reconcile_rejections).

    A crawl that stored >=1 item resets the streak; a genuine zero-item crawl bumps it. A
    robots.txt block is the supplier's decision, not a failure, so it never moves the streak
    (NG-2). Must be called at the store site with THIS crawl's count — suppliers.item_count
    is stale on a failed crawl (replace_materials only runs on success), so it can't be
    reconstructed afterwards.
    """
    from .robots import is_block_error
    if is_block_error(error or ""):
        return
    if n_items > 0:
        conn.execute("UPDATE suppliers SET empty_streak = 0 WHERE host = ?", (host,))
    else:
        conn.execute(
            "UPDATE suppliers SET empty_streak = COALESCE(empty_streak, 0) + 1 WHERE host = ?",
            (host,))
    conn.commit()


def purge_supplier(conn: sqlite3.Connection, host: str) -> dict[str, int]:
    """Erase a supplier's collected catalog — for honoring a removal request.

    The denylist stops us *crawling* a host again. On its own that leaves everything
    already collected sitting in the database and still fully searchable, which is
    not what "stop indexing us" means; a supplier who asked to be removed would keep
    finding themselves in the app indefinitely. This removes the catalog rows, the
    per-slab cache, the history snapshots, and the CLIP embeddings derived from their
    photos, then scrubs the contact details the crawler scraped.

    The `suppliers` row is zeroed rather than deleted, and `list_items` is left
    alone, because those are the *user's* sourcing lists referencing this supplier by
    id — their own saved work, which already snapshots name/photo so the entries
    still render (flagged "gone"). Deleting it would also break the foreign key.
    The remaining count is returned so the caller can say so out loud instead of
    quietly leaving it there.
    """
    row = conn.execute("SELECT id FROM suppliers WHERE host = ?", (host,)).fetchone()
    if row is None:
        return {"materials": 0, "slabs": 0, "history": 0, "image_vectors": 0,
                "list_items_kept": 0}
    sid = int(row["id"])

    def _count(sql: str) -> int:
        return int(conn.execute(sql, (sid,)).fetchone()[0])

    out = {
        "materials": _count("SELECT COUNT(*) FROM materials WHERE supplier_id = ?"),
        "slabs": _count("SELECT COUNT(*) FROM slabs WHERE supplier_id = ?"),
        "history": _count("SELECT COUNT(*) FROM history WHERE supplier_id = ?"),
        "list_items_kept": _count("SELECT COUNT(*) FROM list_items WHERE supplier_id = ?"),
    }
    # Embeddings are keyed by image_url, so they must be resolved through materials
    # BEFORE those rows go — afterwards there is nothing left to join against and the
    # vectors would linger, still answering search-by-photo with this supplier's stone.
    out["image_vectors"] = int(conn.execute(
        "SELECT COUNT(*) FROM image_vectors WHERE image_url IN "
        "(SELECT image_url FROM materials WHERE supplier_id = ? AND COALESCE(image_url,'') <> '')",
        (sid,)).fetchone()[0])
    conn.execute(
        "DELETE FROM image_vectors WHERE image_url IN "
        "(SELECT image_url FROM materials WHERE supplier_id = ? AND COALESCE(image_url,'') <> '')",
        (sid,))
    for table in ("materials", "slabs", "history"):
        conn.execute(f"DELETE FROM {table} WHERE supplier_id = ?", (sid,))
    conn.execute(
        "UPDATE suppliers SET item_count = 0, slab_count = 0, token = NULL, "
        "company = NULL, products = NULL, phone = NULL, email = NULL, "
        "image_base = NULL, locations = NULL, last_error = ? WHERE id = ?",
        ("removed at the supplier's request (denylisted)", sid))
    conn.commit()
    return out


def replace_materials(
    conn: sqlite3.Connection, supplier_id: int, rows: Iterable[dict[str, Any]]
) -> int:
    """Replace all materials for a supplier with a fresh set (idempotent re-crawl)."""
    conn.execute("DELETE FROM materials WHERE supplier_id = ?", (supplier_id,))
    cols = [
        "supplier_id", "item_id", "item_name", "name_norm", "material_key",
        "material_type", "category", "subcategory", "color", "finish", "thickness",
        "product_form", "origin", "available_qty", "available_slabs", "avg_length",
        "avg_width", "uom", "sku", "idone", "price_range", "new_arrival",
        "image_filename", "image_url", "locations", "source_url", "crawled_at",
    ]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT OR IGNORE INTO materials ({', '.join(cols)}) VALUES ({placeholders})"
    # Some tenants return one row per SLAB, so many rows repeat a single
    # (supplier_id, item_id, idone) — collapsing them to one material is correct. Counting
    # the attempts was not: item_count is shown on Health, drives "N suppliers" stats, and
    # is the number we'd quote back to a supplier about their own catalog. Count what
    # actually landed.
    before = conn.total_changes
    for r in rows:
        conn.execute(sql, tuple(r.get(c) for c in cols))
    n = conn.total_changes - before
    conn.execute("UPDATE suppliers SET item_count = ? WHERE id = ?", (n, supplier_id))
    conn.commit()
    return n


def replace_slabs(
    conn: sqlite3.Connection, supplier_id: int, rows: Iterable[dict[str, Any]], crawled_at: str
) -> int:
    """Replace all pre-cached slabs for a supplier with a fresh set."""
    conn.execute("DELETE FROM slabs WHERE supplier_id = ?", (supplier_id,))
    cols = ["supplier_id", "item_id", "slab_no", "location", "length", "width",
            "qty", "uom", "barcode", "image_filename", "image_url", "crawled_at"]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO slabs ({', '.join(cols)}) VALUES ({placeholders})"
    n = 0
    for r in rows:
        conn.execute(sql, tuple(r.get(c) for c in cols))
        n += 1
    conn.execute(
        "UPDATE suppliers SET slab_count = ?, slabs_cached_at = ? WHERE id = ?",
        (n, crawled_at, supplier_id),
    )
    conn.commit()
    return n


def get_slabs(conn: sqlite3.Connection, supplier_id: int, item_id: str) -> list[dict[str, Any]]:
    """Pre-cached slabs for a material, most-photographed first."""
    rows = conn.execute(
        """SELECT slab_no, location, length, width, qty, uom, barcode, image_url
           FROM slabs WHERE supplier_id = ? AND item_id = ?
           ORDER BY (image_url = '' OR image_url IS NULL), slab_no""",
        (supplier_id, str(item_id)),
    ).fetchall()
    return [dict(r) for r in rows]


_stats_cache: dict[str, tuple[float, dict]] = {}
_STATS_TTL = 30.0  # stats change only on a crawl; a page view never needs them fresher


def stats(conn: sqlite3.Connection, *, use_cache: bool = True) -> dict[str, Any]:
    # This recomputes six aggregates over ~130k materials and runs on nearly every page,
    # yet only changes on a crawl — so cache it briefly, keyed by DB file. That alone
    # takes ~200ms off every tab load. use_cache=False forces a fresh recompute.
    path = ""
    if use_cache:
        try:
            path = (conn.execute("PRAGMA database_list").fetchone() or ("", "", ""))[2] or ""
        except sqlite3.Error:
            path = ""
        hit = _stats_cache.get(path)
        if hit and time.monotonic() - hit[0] < _STATS_TTL:
            return hit[1]
    s = {}
    # Mirror storefronts (one tenant published under two supplier names) are excluded so a
    # duplicated catalog isn't counted as a second supplier — see detect_mirrors().
    s["suppliers"] = conn.execute(
        "SELECT COUNT(*) c FROM suppliers WHERE item_count > 0 AND mirror_of IS NULL"
    ).fetchone()["c"]
    # User-facing counts are the stone/tile CATALOG — the accessory/non-slab bucket
    # (sinks, tools, chemicals) is excluded here (it's still on the Quality audit).
    _cat = "material_type <> 'Accessory / Non-Slab'"
    s["materials"] = conn.execute(f"SELECT COUNT(*) c FROM materials WHERE {_cat}").fetchone()["c"]
    s["unique_materials"] = conn.execute(
        f"SELECT COUNT(DISTINCT material_key) c FROM materials WHERE material_key <> '' AND {_cat}"
    ).fetchone()["c"]
    s["by_type"] = [
        dict(r) for r in conn.execute(
            f"SELECT material_type, COUNT(*) n FROM materials "
            f"WHERE material_type <> '' AND {_cat} GROUP BY material_type ORDER BY n DESC"
        ).fetchall()
    ]
    # Freshness comes from materials.crawled_at — when the shown data was actually
    # collected — not suppliers.last_crawled, which (a) is set by the single freshest
    # supplier while most of the catalog may be days older, and (b) is stamped even on
    # FAILED crawls, so a night of 104 Cloudflare errors would advance the "updated"
    # claim over an entirely unchanged catalog. The range (oldest live supplier's data
    # → newest) is what the header shows; a single MAX was overstating freshness for
    # 65% of items.
    row = conn.execute(
        """SELECT MIN(t) oldest, MAX(t) newest FROM
             (SELECT MAX(crawled_at) t FROM materials GROUP BY supplier_id)"""
    ).fetchone()
    s["last_updated"] = row["newest"] if row else None
    s["oldest_updated"] = row["oldest"] if row else None
    s["with_images"] = conn.execute(
        f"SELECT COUNT(*) c FROM materials WHERE image_url <> '' AND {_cat}"
    ).fetchone()["c"]
    if use_cache:
        _stats_cache[path] = (time.monotonic(), s)
    return s


def backfill_locations(conn: sqlite3.Connection, supplier_id: int | None = None) -> None:
    """Set materials.locations + suppliers.locations from cached slab locations."""
    where = "WHERE supplier_id = ?" if supplier_id else ""
    args = (supplier_id,) if supplier_id else ()
    conn.execute(
        f"""UPDATE materials SET locations = (
                SELECT GROUP_CONCAT(DISTINCT sl.location)
                FROM slabs sl
                WHERE sl.supplier_id = materials.supplier_id
                      AND sl.item_id = materials.item_id AND sl.location <> ''
            ) {where}""",
        args,
    )
    conn.execute(
        f"""UPDATE suppliers SET locations = (
                SELECT GROUP_CONCAT(DISTINCT sl.location)
                FROM slabs sl WHERE sl.supplier_id = suppliers.id AND sl.location <> ''
            ) {('WHERE id = ?' if supplier_id else '')}""",
        args,
    )
    conn.commit()


def snapshot_history(conn: sqlite3.Connection, supplier_id: int, snapshot_date: str) -> int:
    """Record today's per-product slab totals for a supplier (idempotent per date)."""
    conn.execute(
        "DELETE FROM history WHERE supplier_id = ? AND snapshot_date = ?",
        (supplier_id, snapshot_date),
    )
    cur = conn.execute(
        """INSERT INTO history
             (snapshot_date, supplier_id, material_key, name_norm, thickness,
              material_type, color, slabs, image_url)
           SELECT ?, supplier_id, MAX(material_key), name_norm, thickness,
                  MAX(material_type), MAX(color),
                  -- Slabs only: a TILE row's available_slabs is square feet (or a
                  -- piece count), and a restock alert reading "9,446 slabs" for a
                  -- tile listing would be flatly wrong. Same rule as the search UI.
                  SUM(CASE WHEN UPPER(COALESCE(product_form,'')) LIKE '%TILE%'
                           THEN 0 ELSE COALESCE(available_slabs,0) END),
                  MAX(image_url)
           FROM materials WHERE supplier_id = ?
           GROUP BY name_norm, thickness, finish, product_form""",
        (snapshot_date, supplier_id),
    )
    conn.commit()
    return cur.rowcount


def distinct_locations(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT location FROM slabs WHERE location <> '' ORDER BY location"
    ).fetchall()
    return [r["location"] for r in rows]


# --- Materialized per-product rollup (search fast path) ------------------------
#
# The SELECT below MUST stay in lockstep with _search's inner/outer aggregation in
# web/app.py: it reproduces exactly what the live outer query computes per product,
# so the fast path can read this table and return byte-identical rows.
# tests/test_quality.py::ProductRollupTests asserts that parity across the matrix.
_ROLLUP_IS_TILE = "UPPER(COALESCE(m.product_form,'')) LIKE '%TILE%'"
_ROLLUP_REBUILD = f"""
INSERT INTO product_rollup
    (grp, id, material_key, item_name, material_type, color, new_arrival, suppliers,
     available_slabs, total_slabs, tile_sf, has_tile, avg_length, avg_width,
     total_sqft, image_url)
SELECT grp, MIN(id), COALESCE(MAX(material_key), ''), MIN(item_name), MAX(material_type),
       MAX(color), MAX(new_arrival), COUNT(*),
       MAX(sup_slabs), SUM(sup_slabs), SUM(sup_tile_sf), MAX(sup_has_tile),
       MAX(avg_length), MAX(avg_width), MAX(sup_sqft),
       COALESCE(MAX(real_img), MAX(any_img))
FROM (
    SELECT
        CASE WHEN COALESCE(m.material_key,'') = '' THEN 'id:' || m.id
             ELSE m.material_key END AS grp,
        MIN(m.id) AS id, MAX(m.material_key) AS material_key,
        MIN(m.item_name) AS item_name, MAX(m.material_type) AS material_type,
        MAX(m.color) AS color, MAX(m.new_arrival) AS new_arrival,
        MAX(m.avg_length) AS avg_length, MAX(m.avg_width) AS avg_width,
        SUM(CASE WHEN {_ROLLUP_IS_TILE} THEN 0 ELSE COALESCE(m.available_slabs,0) END) AS sup_slabs,
        SUM(CASE WHEN {_ROLLUP_IS_TILE} AND UPPER(COALESCE(m.uom,'')) = 'SF'
                 THEN COALESCE(m.available_slabs,0) ELSE 0 END) AS sup_tile_sf,
        MAX(CASE WHEN {_ROLLUP_IS_TILE} THEN 1 ELSE 0 END) AS sup_has_tile,
        SUM(CASE WHEN {_ROLLUP_IS_TILE} THEN 0 ELSE COALESCE(m.available_slabs,0)
                 * COALESCE(m.avg_length,0) * COALESCE(m.avg_width,0) END) / 144.0 AS sup_sqft,
        MAX(CASE WHEN COALESCE(m.image_url,'') <> ''
                 AND m.image_url NOT LIKE '%slabcloud.com/slabs/%'
                 THEN m.image_url END) AS real_img,
        MAX(m.image_url) AS any_img
    FROM materials m JOIN suppliers s ON s.id = m.supplier_id
    GROUP BY grp, m.supplier_id
)
GROUP BY grp
"""

# FTS5 name index for the free-text (q) fast path. One row per product `grp` (same key
# as product_rollup), whose `name` column is every distinct name variant of that product
# — so an FTS5 word/prefix MATCH resolves straight back to a set of grps to aggregate via
# the rollup. Kept in lockstep with the rollup's grp definition.
_FTS_REBUILD = """
INSERT INTO product_fts (grp, name)
SELECT CASE WHEN COALESCE(material_key,'') = '' THEN 'id:' || id ELSE material_key END AS grp,
       GROUP_CONCAT(DISTINCT name_norm)
FROM materials
GROUP BY grp
"""


def _has_fts(conn: sqlite3.Connection) -> bool:
    """Whether the FTS5 name index exists — it doesn't when the runtime SQLite was built
    without FTS5, in which case q keeps using the live LIKE path."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'product_fts'").fetchone() is not None


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS5 name index if this SQLite build has FTS5. Deliberately NOT part of
    SCHEMA/executescript: a missing fts5 module would abort the whole schema. Here a
    failure is swallowed and simply means the FTS table never exists (q falls back)."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS product_fts USING fts5(grp UNINDEXED, name)")
    except sqlite3.OperationalError:
        pass  # FTS5 not compiled in


def rebuild_product_rollup(conn: sqlite3.Connection) -> int:
    """Rebuild the product_rollup materialized table (and, when present, the FTS5 name
    index) from materials, in one pass.

    Returns the number of product rows written. Idempotent and cheap enough to run after
    every crawl / reclassify / merge — a single INSERT…SELECT each. The rollup rows are
    inserted in GROUP BY (grp) order, which is the same order the live outer query feeds
    its ORDER BY, so the fast path's tie-breaking matches the live path's.
    """
    conn.execute("DELETE FROM product_rollup")
    conn.execute(_ROLLUP_REBUILD)
    if _has_fts(conn):
        conn.execute("DELETE FROM product_fts")
        conn.execute(_FTS_REBUILD)
    conn.commit()
    return int(conn.execute("SELECT COUNT(*) FROM product_rollup").fetchone()[0])


def ensure_product_rollup(conn: sqlite3.Connection) -> int:
    """Build the search fast-path tables when they're empty while materials exist — e.g.
    a shipped seed DB predating them, or an install upgraded to add the FTS index after
    the rollup already existed. Returns rows built, or 0 when nothing is needed. The fast
    paths fall back to the live query until this has run, so empty never means an empty page."""
    rollup_empty = conn.execute("SELECT 1 FROM product_rollup LIMIT 1").fetchone() is None
    fts_empty = _has_fts(conn) and conn.execute("SELECT 1 FROM product_fts LIMIT 1").fetchone() is None
    if not (rollup_empty or fts_empty):
        return 0
    if not conn.execute("SELECT 1 FROM materials LIMIT 1").fetchone():
        return 0
    return rebuild_product_rollup(conn)


def supplier_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-supplier crawl state for the health page.

    `status` separates the cases that matter operationally:
      ok      — has data, last crawl clean
      stale   — has data but the last refresh errored (old data still serving)
      broken  — errored and no data at all
      empty   — no error, but the catalog returned nothing (may be legitimately empty)
      blocked — the supplier's robots.txt disallows crawling; not a failure, a decision
    """
    from datetime import datetime, timezone

    from .robots import BLOCK_MARKER
    now = datetime.now(timezone.utc)

    def _hours(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return (now - t).total_seconds() / 3600
        except ValueError:
            return None

    # When each supplier's DATA was collected — distinct from last_crawled, which is
    # the last crawl ATTEMPT and is stamped on failures too. A stale supplier can show
    # "last crawl: just now" beside data that is weeks old; both belong on the page.
    data_ts = {r["supplier_id"]: r["t"] for r in conn.execute(
        "SELECT supplier_id, MAX(crawled_at) t FROM materials GROUP BY supplier_id")}

    out = []
    for r in conn.execute(
        """SELECT id, host, COALESCE(NULLIF(company,''), host) AS name, item_count,
                  slab_count, last_crawled, last_error
           FROM suppliers ORDER BY name"""
    ):
        d = dict(r)
        err = bool(d["last_error"])
        has = (d["item_count"] or 0) > 0
        if (d["last_error"] or "").startswith(BLOCK_MARKER):
            # robots.txt said no — the supplier's own answer, not a crawl fault.
            d["status"] = "blocked"
        else:
            d["status"] = ("ok" if not err else "stale") if has else ("broken" if err else "empty")
        d["age_hours"] = _hours(d["last_crawled"])
        d["data_age_hours"] = _hours(data_ts.get(d["id"]))
        out.append(d)
    return out


def save_location_geo(conn: sqlite3.Connection, location: str, hit: dict | None) -> None:
    """Cache a resolution. `hit` of None records a location that isn't a place."""
    from datetime import datetime, timezone
    conn.execute(
        """INSERT INTO location_geo (location, lat, lon, label, source, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(location) DO UPDATE SET
             lat=excluded.lat, lon=excluded.lon, label=excluded.label,
             source=excluded.source, updated_at=excluded.updated_at""",
        (location, (hit or {}).get("lat"), (hit or {}).get("lon"),
         (hit or {}).get("label"), (hit or {}).get("source"),
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


def location_geo(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["location"]: dict(r) for r in conn.execute("SELECT * FROM location_geo")}


def location_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every stocking location with its slab/supplier totals, biggest first."""
    return [dict(r) for r in conn.execute(
        """SELECT location, COUNT(*) AS slabs,
                  COUNT(DISTINCT supplier_id) AS suppliers,
                  COUNT(DISTINCT supplier_id || '/' || item_id) AS products
           FROM slabs WHERE location <> ''
           GROUP BY location ORDER BY slabs DESC, location"""
    ).fetchall()]


def create_list(conn: sqlite3.Connection, name: str, created_at: str, note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO lists (name, note, created_at) VALUES (?, ?, ?)",
        (name.strip() or "Untitled list", note, created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_lists(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every list with its item count and a few thumbnails for the index page."""
    rows = [dict(r) for r in conn.execute(
        """SELECT l.id, l.name, l.note, l.created_at, COUNT(li.id) AS items,
                  COUNT(DISTINCT li.supplier_id) AS suppliers
           FROM lists l LEFT JOIN list_items li ON li.list_id = l.id
           GROUP BY l.id ORDER BY l.created_at DESC, l.id DESC"""
    )]
    for r in rows:
        r["thumbs"] = [x["image_url"] for x in conn.execute(
            "SELECT image_url FROM list_items WHERE list_id = ? AND image_url <> '' LIMIT 5",
            (r["id"],),
        )]
    return rows


def get_list(conn: sqlite3.Connection, list_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT id, name, note, created_at FROM lists WHERE id = ?",
                     (list_id,)).fetchone()
    return dict(r) if r else None


def rename_list(conn: sqlite3.Connection, list_id: int, name: str) -> None:
    conn.execute("UPDATE lists SET name = ? WHERE id = ?", (name.strip(), list_id))
    conn.commit()


def delete_list(conn: sqlite3.Connection, list_id: int) -> None:
    conn.execute("DELETE FROM list_items WHERE list_id = ?", (list_id,))
    conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    conn.commit()


def add_to_list(conn: sqlite3.Connection, list_id: int, material_id: int,
                added_at: str, note: str = "") -> bool:
    """Snapshot a material into a list. Returns False if the material is gone or
    already on the list."""
    m = conn.execute(
        """SELECT m.supplier_id, m.item_id, m.item_name, m.material_key, m.thickness,
                  m.finish, m.image_url, s.host AS supplier_host,
                  COALESCE(NULLIF(s.company,''), s.host) AS supplier_name
           FROM materials m JOIN suppliers s ON s.id = m.supplier_id WHERE m.id = ?""",
        (material_id,),
    ).fetchone()
    if not m:
        return False
    cur = conn.execute(
        """INSERT OR IGNORE INTO list_items
             (list_id, supplier_id, item_id, supplier_host, supplier_name, item_name,
              material_key, thickness, finish, image_url, note, added_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (list_id, m["supplier_id"], m["item_id"], m["supplier_host"], m["supplier_name"],
         m["item_name"], m["material_key"], m["thickness"], m["finish"],
         m["image_url"] or "", note, added_at),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_list_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM list_items WHERE id = ?", (item_id,))
    conn.commit()


def set_item_note(conn: sqlite3.Connection, item_id: int, note: str) -> None:
    conn.execute("UPDATE list_items SET note = ? WHERE id = ?", (note, item_id))
    conn.commit()


def get_list_items(conn: sqlite3.Connection, list_id: int) -> list[dict[str, Any]]:
    """List entries with live stock re-resolved from the current crawl. The join is
    on (supplier_id, item_id) because materials.id changes every crawl; anything
    that no longer resolves falls back to its stored snapshot and is flagged gone."""
    rows = [dict(r) for r in conn.execute(
        """SELECT li.*,
                  (SELECT MIN(m.id) FROM materials m
                    WHERE m.supplier_id = li.supplier_id AND m.item_id = li.item_id) AS live_id,
                  (SELECT SUM(m.available_slabs) FROM materials m
                    WHERE m.supplier_id = li.supplier_id AND m.item_id = li.item_id) AS live_slabs,
                  (SELECT s.email FROM suppliers s WHERE s.id = li.supplier_id) AS supplier_email,
                  (SELECT s.phone FROM suppliers s WHERE s.id = li.supplier_id) AS supplier_phone
           FROM list_items li WHERE li.list_id = ?
           ORDER BY li.supplier_name, li.item_name""",
        (list_id,),
    )]
    for r in rows:
        r["gone"] = r["live_id"] is None
    return rows


def list_watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, query, created_at FROM watchlist ORDER BY created_at DESC"
    ).fetchall()]


def add_watch(conn: sqlite3.Connection, query: str, created_at: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (query, created_at) VALUES (?, ?)",
        (query.strip(), created_at),
    )
    conn.commit()


def remove_watch(conn: sqlite3.Connection, watch_id: int) -> None:
    conn.execute("DELETE FROM watchlist WHERE id = ?", (watch_id,))
    conn.commit()


# --- Compare tray -------------------------------------------------------------

COMPARE_MAX = 4  # side-by-side columns that fit the desktop window without scrolling


def get_compare(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The compare tray, oldest first — the order columns appear on /compare. Returns
    [] (never raises) when the table is absent, so an old DB snapshot degrades gracefully."""
    try:
        return [dict(r) for r in conn.execute(
            "SELECT material_key, item_name, image_url, added_at FROM compare_tray "
            "ORDER BY added_at, rowid"
        ).fetchall()]
    except sqlite3.OperationalError:
        return []


def compare_count(conn: sqlite3.Connection) -> int:
    """How many materials are in the tray; 0 (never an error) when the table is absent."""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM compare_tray").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def add_to_compare(conn: sqlite3.Connection, material_key: str, item_name: str,
                   image_url: str, added_at: str) -> str:
    """Add a canonical material to the compare tray. Returns one of:
      'exists' — already present (idempotent, no duplicate row) or key blank
      'full'   — the tray already holds COMPARE_MAX; nothing was added
      'added'  — inserted

    The cap lives HERE, not in the route, so every caller (web, tests) enforces it
    identically."""
    key = (material_key or "").strip()
    if not key:
        return "exists"
    if conn.execute("SELECT 1 FROM compare_tray WHERE material_key = ?", (key,)).fetchone():
        return "exists"
    if compare_count(conn) >= COMPARE_MAX:
        return "full"
    conn.execute(
        "INSERT OR IGNORE INTO compare_tray (material_key, item_name, image_url, added_at) "
        "VALUES (?,?,?,?)",
        (key, item_name or "", image_url or "", added_at),
    )
    conn.commit()
    return "added"


def remove_from_compare(conn: sqlite3.Connection, material_key: str) -> None:
    """Drop a material from the tray. A no-op when it isn't present."""
    conn.execute("DELETE FROM compare_tray WHERE material_key = ?",
                 ((material_key or "").strip(),))
    conn.commit()


def clear_compare(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM compare_tray")
    conn.commit()


def resolve_alias(conn: sqlite3.Connection, material_key: str) -> str:
    """Follow material_aliases to the terminal canonical key. apply_aliases() stores
    terminal canonicals (add_alias normalizes chains), so one hop usually suffices; the
    short loop is defense in depth and is cycle-guarded. Returns the input unchanged when
    the key isn't aliased — including when the aliases table is absent."""
    key = material_key or ""
    seen = {key}
    try:
        for _ in range(10):
            row = conn.execute(
                "SELECT canonical_key FROM material_aliases WHERE alias_key = ?", (key,)
            ).fetchone()
            if not row or row["canonical_key"] in seen:
                break
            key = row["canonical_key"]
            seen.add(key)
    except sqlite3.OperationalError:
        pass
    return key


# --- Alerts: seen-tracking for the catalog-change digest ----------------------

def seen_alert_sigs(conn: sqlite3.Connection) -> set[str]:
    """Signatures of change events the user has already seen. Returns an empty set
    (never raises) when the table is absent, so a DB predating it treats everything as
    unread rather than 500-ing."""
    try:
        return {r[0] for r in conn.execute("SELECT sig FROM alert_seen")}
    except sqlite3.OperationalError:
        return set()


def mark_alerts_seen(conn: sqlite3.Connection, sigs) -> None:
    """Record change-event signatures as seen (idempotent). A no-op on an empty list."""
    sigs = [s for s in sigs if s]
    if not sigs:
        return
    now = _now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO alert_seen (sig, seen_at) VALUES (?, ?)",
        [(s, now) for s in sigs],
    )
    conn.commit()


# --- Data quality: cross-supplier material_key merges -------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_aliases(conn: sqlite3.Connection) -> int:
    """Fold every aliased material_key into its canonical one, in place.

    Idempotent and safe to run after any crawl/reclassify: once folded, an
    alias_key matches no rows on the next pass, and a fresh crawl that re-derives
    the same alias_key simply gets re-folded. Returns the number of rows rewritten.

    Runs to a fixpoint so an accidental a->b->c chain resolves regardless of the row
    order in the alias table (add_alias normalizes to terminals, so this is usually a
    single effective pass; the loop is defense in depth, capped against a cycle).
    """
    rows = conn.execute(
        "SELECT alias_key, canonical_key, canonical_type FROM material_aliases"
    ).fetchall()
    total = 0
    for _ in range(10):
        changed = 0
        for a in rows:
            cur = conn.execute(
                "UPDATE materials SET material_key = ?, "
                "material_type = COALESCE(?, material_type) WHERE material_key = ?",
                (a["canonical_key"], a["canonical_type"], a["alias_key"]),
            )
            changed += cur.rowcount
        total += changed
        if not changed:
            break
    conn.commit()
    return total


def add_alias(conn: sqlite3.Connection, alias_key: str, canonical_key: str,
              canonical_type: str | None = None, note: str = "") -> None:
    """Record (or update) a merge. Does NOT rewrite materials — call apply_aliases()
    for that (the web route, ingest, reclassify and auto_conflicts all do)."""
    if not alias_key or not canonical_key or alias_key == canonical_key:
        return
    # Resolve the target to a terminal canonical, so we never store a->b while b->c
    # exists (which would make apply_aliases order-dependent). Guard against a cycle.
    seen = {canonical_key}
    row = conn.execute("SELECT canonical_key FROM material_aliases WHERE alias_key = ?",
                       (canonical_key,)).fetchone()
    while row and row["canonical_key"] not in seen:
        canonical_key = row["canonical_key"]
        seen.add(canonical_key)
        row = conn.execute("SELECT canonical_key FROM material_aliases WHERE alias_key = ?",
                           (canonical_key,)).fetchone()
    if alias_key == canonical_key:
        return
    conn.execute(
        """INSERT INTO material_aliases (alias_key, canonical_key, canonical_type, note, created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(alias_key) DO UPDATE SET
             canonical_key = excluded.canonical_key,
             canonical_type = excluded.canonical_type, note = excluded.note""",
        (alias_key, canonical_key, canonical_type, note, _now_iso()),
    )
    # Repoint any existing alias that folded INTO this now-folded key, so chains
    # collapse to a single terminal canonical (a -> b, then b -> c  ==>  a -> c).
    conn.execute(
        "UPDATE material_aliases SET canonical_key = ? WHERE canonical_key = ?",
        (canonical_key, alias_key),
    )
    conn.commit()


def remove_alias(conn: sqlite3.Connection, alias_key: str) -> None:
    conn.execute("DELETE FROM material_aliases WHERE alias_key = ?", (alias_key,))
    conn.commit()


def list_aliases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT alias_key, canonical_key, canonical_type, note, created_at "
        "FROM material_aliases ORDER BY created_at DESC, alias_key"
    ).fetchall()]


def add_rejection(conn: sqlite3.Connection, sig: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO merge_rejections (sig, created_at) VALUES (?, ?)",
        (sig, _now_iso()),
    )
    conn.commit()


def rejections(conn: sqlite3.Connection) -> set[str]:
    return {r["sig"] for r in conn.execute("SELECT sig FROM merge_rejections")}


# ---------------------------------------------------------------------------
# Mirror detection: two supplier hosts serving the SAME tenant catalog.
#
# Some Stone Profits tenants publish more than one storefront on the same token — a
# vanity/brand host beside the primary — and each stores the identical item set under
# its own supplier name, so one physical yard is counted twice in supplier totals and
# facets. We detect it from the data: within a shared tenant token (item_id is unique
# only PER-tenant, so an overlap across different tokens would be meaningless — two
# unrelated catalogs that both number from 1), two hosts whose distinct item_id sets
# overlap by >= MIRROR_MIN_OVERLAP are the same catalog. The non-canonical host is
# flagged mirror_of=<canonical> and dropped from the supplier facet and the "N
# suppliers" stat. Nothing is deleted — its rows stay fully searchable.
# ---------------------------------------------------------------------------

MIRROR_MIN_OVERLAP = 0.98   # >=98%: absorbs the slab-or-two drift between two crawls of
                            # one live catalog, yet far above the real filtered/partial
                            # storefront pairs, which sit below ~0.45.


@dataclass
class MirrorOverride:
    """A human-reviewed override of automatic mirror detection, from suppliers.json.

    Two shapes on a supplier entry:
        "mirror_of": "<canonical host>"          force this host to be a mirror
        "mirror_of": false, "reason": "..."      declare it genuinely NOT a mirror

    The second is invalid without a reason — the same discipline as robots.Override:
    an undocumented "these look identical but aren't" is exactly the decision that
    rots silently, so it must be written where anyone editing the file can see it.
    """

    host: str
    canonical: str | None   # host to fold into; None means "declared distinct"
    reason: str

    @classmethod
    def from_entry(cls, entry: dict) -> "MirrorOverride | None":
        if not entry or "mirror_of" not in entry:
            return None
        raw = entry.get("mirror_of")
        host = (entry.get("host") or "").strip().lower()
        if raw is False:
            reason = (entry.get("reason") or "").strip()
            if not reason:
                raise ValueError(
                    f"mirror_of:false for {host!r} needs a 'reason' recording why two "
                    "hosts that look identical are genuinely separate catalogs")
            return cls(host, None, reason)
        if isinstance(raw, str) and raw.strip():
            return cls(host, raw.strip().lower(), (entry.get("reason") or "").strip())
        raise ValueError(
            f"mirror_of for {host!r} must be a canonical host string or false, got {raw!r}")


def _distinct_item_ids(conn: sqlite3.Connection, supplier_id: int) -> set:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT item_id FROM materials "
        "WHERE supplier_id = ? AND COALESCE(item_id, '') <> ''", (supplier_id,))}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _pick_canonical(members: list[str], items: dict[str, set], token: str) -> str:
    """Deterministic canonical host for a mirror cluster: most distinct items wins; a tie
    goes to the host whose leftmost DNS label is the tenant token; a remaining tie is
    broken alphabetically. Stable no matter what order the suppliers were inserted in."""
    def key(h: str):
        label = h.split(".")[0].lower()
        return (-len(items[h]), 0 if label == token else 1, h)
    return sorted(members, key=key)[0]


def detect_mirrors(conn: sqlite3.Connection, entries: list[dict] | None = None) -> list[dict]:
    """Flag duplicate-catalog supplier hosts and return the current mirror report.

    Recomputed from scratch on every crawl / reclassify, so a newly duplicated host is
    caught and a pair that no longer matches is automatically un-flagged. `entries`
    supplies the curated suppliers.json overrides; None loads suppliers.json itself.
    """
    if entries is None:
        try:
            from .discover import load_suppliers
            entries = load_suppliers()
        except Exception:   # noqa: BLE001 - detection must never fail a crawl over a bad read
            entries = []
    overrides: dict[str, MirrorOverride] = {}
    for e in entries or []:
        ov = MirrorOverride.from_entry(e)   # raises on false-without-reason (auditable)
        if ov:
            overrides[ov.host] = ov

    # Only same-token suppliers with data can be computed mirrors — see the module note.
    sups = [dict(r) for r in conn.execute(
        "SELECT id, host, token FROM suppliers "
        "WHERE item_count > 0 AND COALESCE(token, '') <> ''")]
    by_token: dict[str, list] = {}
    for s in sups:
        by_token.setdefault(s["token"].strip().lower(), []).append(s)

    computed: dict[str, str] = {}
    for token, group in by_token.items():
        if len(group) < 2:
            continue
        items = {s["host"]: _distinct_item_ids(conn, s["id"]) for s in group}
        hosts = [s["host"] for s in group]
        parent = {h: h for h in hosts}

        def find(x, _parent=parent):
            while _parent[x] != x:
                _parent[x] = _parent[_parent[x]]
                x = _parent[x]
            return x

        for i in range(len(hosts)):
            for j in range(i + 1, len(hosts)):
                if _jaccard(items[hosts[i]], items[hosts[j]]) >= MIRROR_MIN_OVERLAP:
                    parent[find(hosts[i])] = find(hosts[j])
        clusters: dict[str, list] = {}
        for h in hosts:
            clusters.setdefault(find(h), []).append(h)
        for members in clusters.values():
            if len(members) < 2:
                continue
            canon = _pick_canonical(members, items, token)
            for h in members:
                if h != canon:
                    computed[h] = canon

    final: dict[str, tuple[str, str]] = {h: (c, "computed") for h, c in computed.items()}
    for host, ov in overrides.items():
        if ov.canonical is None:
            final.pop(host, None)                       # declared distinct: never a mirror
        else:
            final[host] = (ov.canonical, "curated")     # a curated value beats the computed one

    # Rewrite every flag so a pair that stopped matching (or was un-declared) is cleared.
    conn.execute("UPDATE suppliers SET mirror_of = NULL, mirror_source = NULL")
    for host, (canon, source) in final.items():
        conn.execute("UPDATE suppliers SET mirror_of = ?, mirror_source = ? WHERE host = ?",
                     (canon, source, host))
    conn.commit()
    return mirror_report(conn)


def mirror_report(conn: sqlite3.Connection) -> list[dict]:
    """One entry per flagged mirror, with the numbers /health shows: both hosts, the
    overlap, both distinct-item counts, the canonical host, and computed-vs-curated."""
    info = {r["host"]: r for r in conn.execute(
        "SELECT id, host, company FROM suppliers")}
    out = []
    for r in conn.execute(
        "SELECT host, company, mirror_of, mirror_source FROM suppliers "
        "WHERE mirror_of IS NOT NULL ORDER BY host"
    ):
        m_row = info.get(r["host"])
        c_row = info.get(r["mirror_of"])
        m_items = _distinct_item_ids(conn, m_row["id"]) if m_row else set()
        c_items = _distinct_item_ids(conn, c_row["id"]) if c_row else set()
        out.append({
            "mirror_host": r["host"],
            "mirror_name": r["company"] or r["host"],
            "canonical_host": r["mirror_of"],
            "canonical_name": (c_row["company"] if c_row else None) or r["mirror_of"],
            "source": r["mirror_source"] or "computed",
            "overlap": _jaccard(m_items, c_items),
            "mirror_items": len(m_items),
            "canonical_items": len(c_items),
        })
    return out


def quality_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Headline data-quality counts for the dashboard."""
    q = lambda sql: conn.execute(sql).fetchone()["c"]  # noqa: E731
    return {
        "materials": q("SELECT COUNT(*) c FROM materials"),
        "unique_materials": q("SELECT COUNT(DISTINCT material_key) c "
                              "FROM materials WHERE material_key <> ''"),
        "other": q("SELECT COUNT(*) c FROM materials WHERE material_type = 'Other'"),
        "accessory": q("SELECT COUNT(*) c FROM materials "
                       "WHERE material_type = 'Accessory / Non-Slab'"),
        "no_color": q("SELECT COUNT(*) c FROM materials WHERE color = '' OR color IS NULL"),
        "aliases": q("SELECT COUNT(*) c FROM material_aliases"),
        "rejections": q("SELECT COUNT(*) c FROM merge_rejections"),
    }


if __name__ == "__main__":
    conn = init_db()
    print(json.dumps(stats(conn), indent=2))
