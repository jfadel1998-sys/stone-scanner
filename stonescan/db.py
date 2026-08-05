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
CREATE INDEX IF NOT EXISTS idx_mat_finish     ON materials(finish);
CREATE INDEX IF NOT EXISTS idx_mat_supplier   ON materials(supplier_id);
-- The alert digest resolves each changed listing back to a material with a correlated
-- (SELECT MIN(mm.id) … WHERE supplier_id = ? AND name_norm = ? AND thickness = ?), once per
-- output row. On idx_mat_supplier alone that re-scans the supplier's whole catalog every
-- time: measured at 24.62s of the query's 25.66s, i.e. 96% of /alerts. The history index
-- below is the fix the issue predicted and it made no difference to this page at all —
-- worth remembering before optimising the half of a join you happen to be looking at.
CREATE INDEX IF NOT EXISTS idx_mat_grp        ON materials(supplier_id, name_norm, thickness);

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
-- Every reader of this table works PER SUPPLIER — each supplier's latest snapshot against
-- its own previous one — and none of the three indexes above leads with supplier_id alone,
-- so the alert digest and _stock_changes both scanned all 211,238 rows repeatedly. /alerts
-- measured 30.7s cold and 29.2s warm before this.
CREATE INDEX IF NOT EXISTS idx_hist_sup_date ON history(supplier_id, snapshot_date);

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

-- A curator-confirmed material_type scope for a mirror storefront (e.g. American Quartz
-- serves only the Quartz slice of the KLZ tenant). Keyed on host so it survives a re-crawl:
-- apply_supplier_filters() re-scopes the freshly-stored rows to this type on every ingest,
-- the same discipline as material_aliases. A rejected proposal is remembered in
-- supplier_filter_rejections by "<host>|<material_type>" so it is not re-offered.
CREATE TABLE IF NOT EXISTS supplier_filters (
    host          TEXT PRIMARY KEY,
    material_type TEXT NOT NULL,
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS supplier_filter_rejections (
    sig         TEXT PRIMARY KEY,   -- "<host>|<material_type>"
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

-- One row per refresh run. refresh-history.log only ever gets a terminal line if the
-- process reaches the end of run_all, so a run killed mid-crawl leaves no record at all
-- and is indistinguishable from a night the task never fired: 4 of 11 logged runs have a
-- start line and no outcome. `heartbeat_at` is what separates the two — it advances once
-- per supplier, so a run that stopped moving is visibly interrupted while a legitimately
-- slow one (the 07-24 run took two hours) is not.
--
-- `attempts` counts supplier COMPLETIONS, not distinct suppliers: the --retry pass crawls
-- some of them a second time and both attempts are real work. Hosts the circuit breaker
-- skips are never counted, because they were never asked.
CREATE TABLE IF NOT EXISTS refresh_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT,
    finished_at  TEXT,
    outcome      TEXT,              -- 'done' | 'failed'; NULL while running or interrupted
    planned      INTEGER DEFAULT 0, -- entries this run intended to crawl
    attempts     INTEGER DEFAULT 0, -- supplier completions so far
    materials    INTEGER,
    detail       TEXT               -- error type + message on failure
);
CREATE INDEX IF NOT EXISTS idx_refresh_runs_started ON refresh_runs(started_at DESC);

-- One row per spill merge (see spill.py). Bookkeeping only: what stops a spill being merged
-- twice is that merging copies its crawled_at stamps, so the strictly-newer test can never
-- fire again for the same data. This table is what /health and the log read afterwards, and
-- a fast pre-check that skips a watermark already dealt with.
CREATE TABLE IF NOT EXISTS spill_merges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    merged_at  TEXT NOT NULL,
    source     TEXT NOT NULL,     -- the spill database this came from
    watermark  TEXT,              -- the spill's newest materials.crawled_at at merge time
    moved      INTEGER DEFAULT 0, -- suppliers taken from the spill
    skipped    INTEGER DEFAULT 0, -- suppliers left alone because the primary was fresher
    detail     TEXT
);
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
    # Marks a snapshot taken AFTER per-item slab counts were collapsed. Rows written
    # before that carry the old inflated accounting, so a latest-vs-previous comparison
    # across the boundary is a units change, not a stock change — see snapshot_history.
    "history": {"rebased": "INTEGER DEFAULT 0"},
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

# How recent a supplier's data must be to count as current. One nightly cycle plus slack:
# the run starts at 03:00 and a long one has taken three hours, so anything inside a day
# was refreshed by the last successful run.
FRESH_HOURS = 24


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
    #
    # The range alone still overstates things, because it says nothing about the
    # DISTRIBUTION between its two ends: "crawled 2026-07-16 → 2026-07-31" reads as fresh
    # while 114 of 140 stocked suppliers sit at the old end of it. So count how many
    # suppliers are actually current, from the same per-supplier scan, and let the header
    # say that instead of implying the newest date applies to everything.
    from datetime import datetime, timedelta, timezone
    _cutoff = (datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)).isoformat(
        timespec="seconds")
    row = conn.execute(
        """SELECT MIN(t) oldest, MAX(t) newest, COUNT(*) n,
                  SUM(CASE WHEN t >= ? THEN 1 ELSE 0 END) current FROM
             (SELECT MAX(crawled_at) t FROM materials GROUP BY supplier_id)""",
        (_cutoff,),
    ).fetchone()
    s["last_updated"] = row["newest"] if row else None
    s["oldest_updated"] = row["oldest"] if row else None
    s["suppliers_with_data"] = (row["n"] or 0) if row else 0
    s["suppliers_current"] = (row["current"] or 0) if row else 0
    s["fresh_hours"] = FRESH_HOURS   # so the header can name the window it is claiming
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


def snapshot_history(conn: sqlite3.Connection, supplier_id: int, snapshot_date: str,
                     *, rebased: bool = False) -> int:
    """Record today's per-product slab totals for a supplier (idempotent per date).

    `rebased` marks the row as taken after per-item slab counts were collapsed, so the
    alert digest can tell a change in how stock is counted from a change in the stock.
    """
    conn.execute(
        "DELETE FROM history WHERE supplier_id = ? AND snapshot_date = ?",
        (supplier_id, snapshot_date),
    )
    cur = conn.execute(
        """INSERT INTO history
             (snapshot_date, supplier_id, material_key, name_norm, thickness,
              material_type, color, slabs, image_url, rebased)
           SELECT ?, supplier_id, MAX(material_key), name_norm, thickness,
                  MAX(material_type), MAX(color),
                  -- Slabs only: a TILE row's available_slabs is square feet (or a
                  -- piece count), and a restock alert reading "9,446 slabs" for a
                  -- tile listing would be flatly wrong. Same rule as the search UI.
                  SUM(CASE WHEN UPPER(COALESCE(product_form,'')) LIKE '%TILE%'
                           THEN 0 ELSE COALESCE(available_slabs,0) END),
                  MAX(image_url), ?
           FROM materials WHERE supplier_id = ?
           GROUP BY name_norm, thickness, finish, product_form""",
        (snapshot_date, 1 if rebased else 0, supplier_id),
    )
    conn.commit()
    return cur.rowcount


# Snapshots to keep PER SUPPLIER. Every reader compares a supplier's latest against its own
# immediately previous one, so two is the working set and three leaves one spare for anything
# that wants to look one step further back.
#
# Per supplier, never per global date, and the difference is not cosmetic: the 9 dates in the
# live table cover 65/83/6/18/3/85/17/115/19 suppliers respectively, so "keep the 3 most
# recent dates" would delete the entire history of a supplier last crawled on one of the
# older ones. Its next alert digest would then have no baseline at all.
HISTORY_KEEP = 3


def prune_history(conn: sqlite3.Connection, keep: int = HISTORY_KEEP) -> int:
    """Drop each supplier's snapshots beyond its `keep` most recent. Returns rows deleted.

    DENSE_RANK over DISTINCT (supplier_id, snapshot_date) so the rank counts snapshots, not
    rows — a supplier with 800 listings on one date must not have that date rank 800 times.
    """
    keep = max(int(keep), 2)          # two is the working set; below that the digest breaks
    before = conn.total_changes
    conn.execute(
        """WITH ranked AS (
               SELECT supplier_id, snapshot_date,
                      DENSE_RANK() OVER (PARTITION BY supplier_id
                                             ORDER BY snapshot_date DESC) rk
                 FROM (SELECT DISTINCT supplier_id, snapshot_date FROM history)
           )
           DELETE FROM history WHERE (supplier_id, snapshot_date) IN
               (SELECT supplier_id, snapshot_date FROM ranked WHERE rk > ?)""",
        (keep,))
    conn.commit()
    return conn.total_changes - before


def resnapshot_history(conn: sqlite3.Connection, snapshot_date: str) -> int:
    """Re-take today's snapshot for every supplier that already has one.

    The per-supplier snapshot is written DURING the crawl, before the tail collapses each
    item's slab count — so without this, history would preserve the pre-collapse numbers
    the rest of the app no longer uses. Restricted to suppliers already snapshotted today
    so a supplier that wasn't crawled doesn't gain a row claiming today's data.

    The rewritten rows are marked `rebased`, which is what lets the alert digest tell a
    units change from a stock change. Returns the number of suppliers re-snapshotted.
    """
    ids = [r["supplier_id"] for r in conn.execute(
        "SELECT DISTINCT supplier_id FROM history WHERE snapshot_date = ?", (snapshot_date,))]
    for sid in ids:
        snapshot_history(conn, sid, snapshot_date, rebased=True)
    return len(ids)


def distinct_locations(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT location FROM slabs WHERE location <> '' ORDER BY location"
    ).fetchall()
    return [r["location"] for r in rows]


# --- Location options -----------------------------------------------------------
#
# `slabs.location` is free text: real cities sit beside internal yard names (KLZ, HG-NJ)
# and outright junk (a URL, "not indicated"). Two variants of one city — "Atlanta" and
# "ATLANTA" — are separate raw values, so a city gets several dropdown entries that each
# return part of its stock.
#
# Variants are folded onto the city label `geocode` already resolved, but only when BOTH
# hold: the resolution is not `ambiguous`, and the raw value IS the city rather than
# merely containing it. Containment is what puts "Baldwin Hills" (Los Angeles) under
# "Baldwin, NY", and it is the same failure mode as the alternatenames trap in CLAUDE.md
# that pinned "Arca - Warehouse" to Arsk, Russia. Anything that fails either test stays a
# standalone option — nothing is ever dropped, because KLZ alone reaches 715 products.

def _loc_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def location_options(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Dropdown options: city groups first, then unresolved yard names.

    Each option carries the raw `members` it stands for, so the search can match every
    spelling of one city without the substring matching that made short codes unusable.
    """
    raw = distinct_locations(conn)
    geo: dict[str, sqlite3.Row] = {}
    try:
        geo = {r["location"]: r for r in conn.execute(
            "SELECT location, label, source FROM location_geo WHERE label IS NOT NULL")}
    except sqlite3.OperationalError:
        geo = {}

    groups: dict[str, list[str]] = {}
    singles: list[str] = []
    for value in raw:
        g = geo.get(value)
        label = (g["label"] if g else "") or ""
        source = (g["source"] if g else "") or ""
        city = label.split(",")[0] if label else ""
        if label and source != "ambiguous" and city and _loc_norm(value) == _loc_norm(city):
            groups.setdefault(label, []).append(value)
        else:
            singles.append(value)

    out = [{"value": label, "label": label, "kind": "city", "members": sorted(m)}
           for label, m in sorted(groups.items())]
    out += [{"value": v, "label": v, "kind": "yard", "members": [v]} for v in sorted(singles)]
    return out


def location_members(conn: sqlite3.Connection, value: str) -> list[str]:
    """The raw location values a chosen dropdown option stands for.

    Falls back to the value itself, so a hand-typed or bookmarked location keeps working
    and a saved search made before grouping existed still resolves.
    """
    if not value:
        return []
    for opt in location_options(conn):
        if opt["value"] == value:
            return opt["members"]
    return [value]


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

    Each row also carries `residue`: True when the supplier's newest data predates its own
    last attempt, i.e. it is serving rows the last crawl failed to replace.
    """
    from datetime import datetime, timezone

    from .robots import BLOCK_MARKER
    now = datetime.now(timezone.utc)
    residue = residue_supplier_ids(conn)

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
        # Residue is a stricter, unambiguous test than `status`. A host can log an error and
        # still have stored items in the same run (partial-crawl preservation), which reads
        # as "stale" here while its data is in fact current; and it can be serving weeks-old
        # rows. Only the data-vs-attempt comparison separates those two.
        d["residue"] = d["id"] in residue
        out.append(d)
    return out


# A run whose heartbeat has not advanced in this long, and which never recorded an outcome,
# is treated as interrupted.
#
# The number is set from measurement, not intuition. `run_providers` walks its list strictly
# sequentially, so while one supplier is being fetched no other tick is possible, and the gap
# between ticks is simply the slowest supplier. In the 2026-08-03 full run the gaps between
# consecutive last_crawled stamps reached 30.1 min (umistone) and 35.3 min (stonetrash), with
# marblesystems at 16.4 min — so a 30-minute cutoff would have rendered that entirely healthy
# run as INTERRUPTED twice while it was still working. 90 gives roughly 2.5x the worst
# observed gap; a run that has genuinely died is still flagged long before anyone looks.
INTERRUPTED_AFTER_MIN = 90


def start_refresh_run(conn: sqlite3.Connection, planned: int) -> int:
    """Open a ledger row for this run and return its id."""
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO refresh_runs (started_at, heartbeat_at, planned) VALUES (?,?,?)",
        (now, now, int(planned)))
    conn.commit()
    return int(cur.lastrowid)


def heartbeat_refresh_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Record that one more supplier finished. Called per supplier, so it must stay a
    single cheap UPDATE on a connection the caller already holds — never open one here,
    because init_db() re-runs the mm-thickness repair scan on every call."""
    conn.execute(
        "UPDATE refresh_runs SET heartbeat_at = ?, attempts = attempts + 1 WHERE id = ?",
        (_now_iso(), run_id))
    conn.commit()


def finish_refresh_run(conn: sqlite3.Connection, run_id: int, *, outcome: str,
                       materials: int | None = None, detail: str | None = None) -> None:
    """Stamp the terminal state. `outcome` is 'done' or 'failed'."""
    conn.execute(
        "UPDATE refresh_runs SET finished_at = ?, outcome = ?, materials = ?, detail = ? "
        "WHERE id = ?",
        (_now_iso(), outcome, materials, (detail or "")[:500], run_id))
    conn.commit()


def recent_refresh_runs(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    """Recent runs, newest first, each with a derived `state`.

    Degrades to [] rather than raising: /health has no error handling, and a snapshot DB
    shipped before this table existed would otherwise 500 the page it was added to.
    """
    from datetime import datetime, timedelta, timezone
    try:
        rows = conn.execute(
            "SELECT * FROM refresh_runs ORDER BY started_at DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    stale_before = (datetime.now(timezone.utc)
                    - timedelta(minutes=INTERRUPTED_AFTER_MIN)).isoformat(timespec="seconds")
    # A run that stopped moving before a LATER run began is over, whatever the clock says.
    # This is the stronger signal and it settles the common case immediately: yesterday's
    # dead run, looked at after tonight's has started, no longer has to age out first.
    newest_start = (rows[0]["started_at"] or "") if rows else ""
    out = []
    for r in rows:
        d = dict(r)
        beat = d.get("heartbeat_at") or ""
        if d.get("outcome"):
            d["state"] = d["outcome"]                      # 'done' | 'failed'
        elif beat < stale_before or beat < newest_start:
            d["state"] = "interrupted"                     # stopped moving, never finished
        else:
            d["state"] = "running"
        out.append(d)
    return out


def record_spill_merge(conn: sqlite3.Connection, *, source: str, watermark: str,
                       moved: int, skipped: int, detail: str = "") -> None:
    """Bank the outcome of a spill merge. Written inside the merge's own transaction, so a
    merge that rolls back leaves no claim to have happened."""
    conn.execute(
        "INSERT INTO spill_merges (merged_at, source, watermark, moved, skipped, detail)"
        " VALUES (?,?,?,?,?,?)",
        (_now_iso(), source, watermark, int(moved), int(skipped), (detail or "")[:500]))


def recent_spill_merges(conn: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    """Recent spill merges, newest first. [] on a DB predating the table — /health draws this
    and must not 500 on a snapshot from before the feature existed."""
    try:
        rows = conn.execute("SELECT * FROM spill_merges ORDER BY id DESC LIMIT ?",
                            (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def spill_watermarks_merged(conn: sqlite3.Connection) -> set[str]:
    """Watermarks already merged, for the cheap pre-check in spill.merge_spill."""
    try:
        return {r["watermark"] for r in conn.execute(
            "SELECT watermark FROM spill_merges WHERE watermark IS NOT NULL")}
    except sqlite3.Error:
        return set()


# Consecutive days without a successful refresh before the app says so unprompted. Two, not
# one: a single missed night is noise, and a warning that cries every hiccup is a warning
# nobody reads. Named once here so it can be tuned without hunting through the query.
LOST_REFRESH_WARN_DAYS = 2


def lost_refresh_days(conn: sqlite3.Connection, *, today: str | None = None) -> int:
    """How many consecutive days have passed with no successful refresh. 0 = fine.

    Counted by DATE ARITHMETIC from the last success, never by inspecting runs, and that is
    the whole point. On 2026-08-04 the scheduled task never fired at all, so there is no run
    whose outcome could be examined — any "look at the last run and see if it failed" query
    reads a healthy row from two days earlier and reports nothing wrong. Subtracting dates
    sees the hole because the hole is what it measures.

    Days, not runs (AC-6): three failed attempts in one night is one bad day.

    A ledger with no rows at all returns 0. That is a fresh install, not a failure, and
    warning there would teach people to ignore the warning. Degrades to 0 rather than raising
    for the same reason `recent_refresh_runs` does — a snapshot DB predating the table must
    not 500 every page in the app now that this renders in the header.
    """
    from datetime import date, datetime, timezone

    try:
        row = conn.execute(
            """SELECT (SELECT COALESCE(finished_at, started_at) FROM refresh_runs
                        WHERE outcome = 'done'
                        ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 1) last_ok,
                      (SELECT started_at FROM refresh_runs
                        ORDER BY started_at ASC LIMIT 1) first_run"""
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None or not row["first_run"]:
        return 0            # never refreshed: a new install, not a broken one

    def _day(stamp: str) -> date | None:
        try:
            return date.fromisoformat(stamp[:10])
        except (TypeError, ValueError):
            return None

    now = (date.fromisoformat(today) if today
           else datetime.now(timezone.utc).date())
    ok = _day(row["last_ok"] or "")
    if ok is not None:
        # Days AFTER the last good one. A success today, however many failures preceded it
        # the same day, is 0.
        return max(0, (now - ok).days)
    first = _day(row["first_run"] or "")
    if first is None:
        return 0
    # Never once succeeded, so the first run's own day counts too — inclusive, hence the +1.
    return max(0, (now - first).days + 1)


# How much older than its own last attempt a supplier's newest data must be before we call
# it residue. A crawl that stores items sets both stamps within the same run, so any real gap
# means the newest attempt did not store anything.
RESIDUE_AFTER_HOURS = 24

# A supplier still serving rows from an earlier crawl because its most recent attempt failed.
# `replace_materials` only runs on success, so the old rows survive with their original
# crawled_at while suppliers.last_crawled advances — the page then reports the ATTEMPT as if
# it were the collection date.
#
# The test is per-supplier and relative: this supplier's data vs this supplier's own last
# attempt. A global cutoff cannot work here — 91.9% of rows predate the newest crawl day
# simply because that day was a targeted retry of 41 hosts, so "older than the newest crawl"
# would condemn nearly the whole catalog.
# BOTH sides go through datetime(). The stored values are `2026-08-03T23:49:00+00:00`, while
# datetime() returns `2026-08-02 23:49:00` — space separator, offset stripped. Comparing the
# raw column against a datetime() result is a TEXT comparison that diverges at position 10 on
# 'T' (0x54) vs ' ' (0x20), so whenever the date halves match the left side always wins and
# the rule silently degenerates to a date-only test: a 26.8h gap goes unflagged while a 25.0h
# one that straddles midnight is caught. Normalizing both sides makes the constant mean what
# it says, and also protects the comparison if a writer ever emits a non-UTC offset.
_RESIDUE_SQL = f"""
    SELECT s.id FROM suppliers s JOIN materials m ON m.supplier_id = s.id
    GROUP BY s.id
    HAVING s.last_crawled IS NOT NULL AND MAX(m.crawled_at) IS NOT NULL
       AND datetime(MAX(m.crawled_at)) < datetime(s.last_crawled, '-{RESIDUE_AFTER_HOURS} hours')
"""

# The query is a GROUP BY over all ~171k materials rows, and it is now on the item and search
# render paths. It only changes on a crawl, so cache it exactly like stats() — keyed by DB
# file, cleared by the web app's _invalidate_caches alongside _stats_cache.
_residue_cache: dict[str, tuple[float, set[int]]] = {}
_RESIDUE_TTL = 30.0


def residue_supplier_ids(conn: sqlite3.Connection, *, use_cache: bool = True) -> set[int]:
    """Suppliers whose newest data predates their own last crawl attempt."""
    path = ""
    if use_cache:
        try:
            path = (conn.execute("PRAGMA database_list").fetchone() or ("", "", ""))[2] or ""
        except sqlite3.Error:
            path = ""
        hit = _residue_cache.get(path)
        if hit and time.monotonic() - hit[0] < _RESIDUE_TTL:
            return hit[1]
    try:
        out = {int(r["id"]) for r in conn.execute(_RESIDUE_SQL)}
    except sqlite3.Error:
        return set()
    if use_cache:
        _residue_cache[path] = (time.monotonic(), out)
    return out


def narrows_contributors(**filters: Any) -> bool:
    """True when a filter selects a SUBSET of the suppliers behind a result row.

    Type and colour describe the stone itself, so every contributor survives them and the
    residue chip stays truthful. Supplier, location, proximity, in-stock and the size floors
    all drop individual supplier rows out of the aggregate, which is what makes a
    whole-catalog residue lookup unable to speak for what is actually on screen.
    """
    return any(bool(filters.get(k)) for k in
               ("supplier", "location", "near", "in_stock", "min_length", "min_width",
                "min_sqft"))


def groups_with_residue(conn: sqlite3.Connection, rows: Iterable[dict]) -> set[str]:
    """Which of these result rows draw on at least one supplier serving residue.

    ANY contributor, not all. A result row's slab totals are summed across its suppliers, so
    one stale contributor already makes the number the user acts on partly unverified —
    and measured on the live catalog the rule marks 349 of 40,712 rows (0.9%) against 104
    (0.3%) for all-stale, so it is not the noisy choice either. All-stale would stay silent
    on the 245 rows in between, every one of which does contain frozen inventory figures.

    Computed per rendered page rather than materialized into product_rollup: search has three
    paths (rollup, FTS, live) and a column would have to be threaded through each, so the
    chip would appear or vanish depending on which one served the query.

    IMPORTANT: this resolves contributors by material_key across the WHOLE catalog, so it
    only tells the truth about an unnarrowed result set. Once a filter selects a subset of
    contributors — a supplier, a location, in-stock, a size floor — the row aggregates that
    subset while this still sees every supplier, and the chip would accuse a row whose
    displayed contributors are all fresh. The caller must not ask when the view is narrowed;
    `narrows_contributors()` is that check. Under-warning is the safe direction here.
    """
    rows = list(rows)
    if not rows:
        return set()
    bad = residue_supplier_ids(conn)
    if not bad:
        return set()
    keys = {r.get("material_key") for r in rows if r.get("material_key")}
    ids = {r.get("id") for r in rows if not r.get("material_key") and r.get("id")}
    out: set[str] = set()
    if keys:
        kph = ",".join("?" for _ in keys)
        sph = ",".join("?" for _ in bad)
        out |= {r["material_key"] for r in conn.execute(
            f"SELECT DISTINCT material_key FROM materials WHERE material_key IN ({kph}) "
            f"AND supplier_id IN ({sph})", (*keys, *bad))}
    if ids:
        iph = ",".join("?" for _ in ids)
        sph = ",".join("?" for _ in bad)
        out |= {f"id:{r['id']}" for r in conn.execute(
            f"SELECT id FROM materials WHERE id IN ({iph}) AND supplier_id IN ({sph})",
            (*ids, *bad))}
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
                  -- Slabs only. available_slabs holds a square-foot (or piece) quantity
                  -- for tiles, so without this guard a tile on a list printed its ft²
                  -- under a "Slabs" heading. Same rule as the search UI and history.
                  (SELECT SUM(CASE WHEN UPPER(COALESCE(m.product_form,'')) LIKE '%TILE%'
                                   THEN 0 ELSE COALESCE(m.available_slabs,0) END)
                     FROM materials m
                    WHERE m.supplier_id = li.supplier_id AND m.item_id = li.item_id) AS live_slabs,
                  (SELECT SUM(CASE WHEN UPPER(COALESCE(m.product_form,'')) LIKE '%TILE%'
                                        AND UPPER(COALESCE(m.uom,'')) = 'SF'
                                   THEN COALESCE(m.available_slabs,0) ELSE 0 END)
                     FROM materials m
                    WHERE m.supplier_id = li.supplier_id AND m.item_id = li.item_id) AS live_tile_sf,
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


def remap_aliases(conn: sqlite3.Connection, remap) -> dict[str, int]:
    """Re-derive every stored merge through `remap` after the key rules change.

    A confirmed merge is two stored material_keys. Change how keys are built and both
    sides can stop being produced — apply_aliases would then fold rows into a key nothing
    else has, quietly orphaning the merge instead of undoing it. Rewriting both sides
    keeps the curator's decision meaning what they meant.

    A merge whose two sides collapse to the SAME key is dropped: the new rules already
    unify those rows, so the alias has nothing left to do. Returns counts of what moved.
    """
    rows = conn.execute(
        "SELECT alias_key, canonical_key, canonical_type, note, created_at "
        "FROM material_aliases").fetchall()
    remapped, dropped, out = 0, 0 , []
    for r in rows:
        ak, ck = remap(r["alias_key"]), remap(r["canonical_key"])
        if ak == ck:
            dropped += 1
            continue
        if ak != r["alias_key"] or ck != r["canonical_key"]:
            remapped += 1
        out.append((ak, ck, r["canonical_type"], r["note"], r["created_at"]))
    conn.execute("DELETE FROM material_aliases")
    # Two old aliases can remap onto one alias_key; INSERT OR REPLACE keeps the last,
    # which is safe because they now name the same fold.
    conn.executemany(
        "INSERT OR REPLACE INTO material_aliases "
        "(alias_key, canonical_key, canonical_type, note, created_at) VALUES (?,?,?,?,?)",
        out)
    conn.commit()
    return {"remapped": remapped, "dropped": dropped, "kept": len(out)}


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


# ---------------------------------------------------------------------------
# Storefront filters: re-scope a mirror to the one product type it actually sells.
#
# A mirror (AIL-9) is a vanity storefront that stores its whole tenant's catalog under
# its own name, but the real storefront shows only a slice — American Quartz shows KLZ's
# 165 quartz items, not all 842. The platform won't tell us the scope (the tenant and the
# vanity host are byte-identical across getSettings), but our own classifier can: if the
# host's name/company/products text names exactly ONE material_type it stocks, that's the
# proposal. A human confirms it on /health; confirming re-scopes the supplier (drops the
# other types' rows) so it stops being a mirror and returns to the facet with its true
# count. Confirmations persist in supplier_filters and re-apply on every crawl.
# ---------------------------------------------------------------------------

def _supplier_id_by_host(conn: sqlite3.Connection, host: str) -> int | None:
    row = conn.execute("SELECT id FROM suppliers WHERE host = ?", (host,)).fetchone()
    return int(row["id"]) if row else None


def _infer_filter_type(text: str, available_types: list[str]) -> str | None:
    """The single material_type named in a mirror's own name/company/products text, if
    exactly one of the types it actually stocks appears as a whole word. Zero or several
    -> None, so a genuine multi-type yard ("GS Granite", products "Granite, Marble,
    Quartz, Quartzite") is never given a made-up scope. Whole-word so `Quartz` does not
    match inside `Quartzite`."""
    t = (text or "").lower()
    hits = [mt for mt in available_types
            if re.search(r"\b" + re.escape(mt.lower()) + r"\b", t)]
    return hits[0] if len(hits) == 1 else None


def _distinct_item_count(conn: sqlite3.Connection, sid: int, material_type: str = "") -> int:
    sql = ("SELECT COUNT(DISTINCT item_id) c FROM materials "
           "WHERE supplier_id = ? AND COALESCE(item_id, '') <> ''")
    args: tuple = (sid,)
    if material_type:
        sql += " AND material_type = ?"
        args += (material_type,)
    return int(conn.execute(sql, args).fetchone()["c"])


def supplier_filter_proposals(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per current mirror host: the inferred material_type scope and its counts, or a
    None type when nothing single is inferable or the proposal was already rejected.
    Recomputed live from the current mirror set, so it reflects the latest crawl."""
    rejected = {r["sig"] for r in conn.execute("SELECT sig FROM supplier_filter_rejections")}
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT id, host, company, products FROM suppliers WHERE mirror_of IS NOT NULL"
    ):
        sid = int(r["id"])
        types = [x["material_type"] for x in conn.execute(
            "SELECT DISTINCT material_type FROM materials "
            "WHERE supplier_id = ? AND COALESCE(material_type, '') <> ''", (sid,))]
        text = " ".join(filter(None, [r["host"], r["company"], r["products"]]))
        mt = _infer_filter_type(text, types)
        if mt and f"{r['host']}|{mt}" not in rejected:
            out[r["host"]] = {"material_type": mt,
                              "matched": _distinct_item_count(conn, sid, mt),
                              "total": _distinct_item_count(conn, sid)}
        else:
            out[r["host"]] = {"material_type": None, "matched": 0, "total": 0}
    return out


def _rescope_supplier(conn: sqlite3.Connection, sid: int, material_type: str) -> int:
    """Drop this supplier's rows (and their slabs) that aren't `material_type`, then refresh
    its counts. Returns the kept distinct-item count. Caller guarantees it is > 0."""
    conn.execute(
        "DELETE FROM slabs WHERE supplier_id = ? AND item_id NOT IN "
        "(SELECT item_id FROM materials WHERE supplier_id = ? AND material_type = ?)",
        (sid, sid, material_type))
    conn.execute("DELETE FROM materials WHERE supplier_id = ? AND material_type <> ?",
                 (sid, material_type))
    kept = _distinct_item_count(conn, sid)
    n_rows = conn.execute("SELECT COUNT(*) c FROM materials WHERE supplier_id = ?",
                          (sid,)).fetchone()["c"]
    n_slabs = conn.execute("SELECT COUNT(*) c FROM slabs WHERE supplier_id = ?",
                           (sid,)).fetchone()["c"]
    conn.execute("UPDATE suppliers SET item_count = ?, slab_count = ? WHERE id = ?",
                 (n_rows, n_slabs, sid))
    return kept


def add_supplier_filter(conn: sqlite3.Connection, host: str, material_type: str) -> int:
    """Confirm a material_type scope for a mirror host: persist it and re-scope the supplier
    now. Refuses (stores nothing, returns 0) if the type matches no rows — a confirmed
    filter must never empty a supplier."""
    sid = _supplier_id_by_host(conn, host)
    if sid is None:
        return 0
    if _distinct_item_count(conn, sid, material_type) == 0:
        return 0
    conn.execute(
        "INSERT INTO supplier_filters (host, material_type, created_at) VALUES (?,?,?) "
        "ON CONFLICT(host) DO UPDATE SET material_type = excluded.material_type",
        (host, material_type, _now_iso()))
    kept = _rescope_supplier(conn, sid, material_type)
    conn.commit()
    return kept


def apply_supplier_filters(conn: sqlite3.Connection) -> int:
    """Re-apply every confirmed storefront filter after a crawl re-fetched the full catalog,
    the same way apply_aliases re-folds merges. A filter that would match zero rows is
    skipped (never empties a supplier). Returns the number of suppliers re-scoped."""
    n = 0
    for f in conn.execute("SELECT host, material_type FROM supplier_filters").fetchall():
        sid = _supplier_id_by_host(conn, f["host"])
        if sid is None:
            continue
        if _distinct_item_count(conn, sid, f["material_type"]) == 0:
            continue
        _rescope_supplier(conn, sid, f["material_type"])
        n += 1
    conn.commit()
    return n


def reject_supplier_filter(conn: sqlite3.Connection, host: str, material_type: str) -> None:
    """Remember that this host's proposed filter was declined, so it isn't re-offered."""
    conn.execute(
        "INSERT OR IGNORE INTO supplier_filter_rejections (sig, created_at) VALUES (?, ?)",
        (f"{host}|{material_type}", _now_iso()))
    conn.commit()


# ---------------------------------------------------------------------------
# Per-item slab counts.
#
# A materials row is one inventory IDENTIFIER (`idone`), not one product — the schema
# is UNIQUE(supplier_id, item_id, idone) and the crawler asks for InventoryGroupBy=IDONE_.
# For the tenants that answer with one row per slab, `available_slabs` repeats the ITEM's
# total on every sibling row, so summing them multiplies the count by the number of slabs.
# Measured against the cached slab galleries as an oracle (16,134 items that have one):
# on multi-row items SUM scores a mean relative error of 159.4 and matches the gallery
# exactly 0.4% of the time, while counting the identifier rows scores 1.40 / 50.2%.
#
# Single-row items are the opposite shape: there the one row's value IS the item's count
# (exact against the gallery 70% of the time), so they are deliberately left alone. That
# split is the whole rule — it is why "5 rows of 1 slab at one yard = 5" still holds.
#
# The correction is written back onto ONE representative row with the siblings zeroed,
# so every existing SUM(available_slabs) in the app keeps working untouched.
# ---------------------------------------------------------------------------

_NON_TILE = "UPPER(COALESCE(product_form,'')) NOT LIKE '%TILE%'"

# Representative row for a collapsed item. Prefers a slab-identifier row that carries
# dimensions, because _SQFT is SUM(slabs x length x width) — parking the count on a
# dimensionless item-level summary row would silently drop the item's area to zero.
_REP_RANK = """ROW_NUMBER() OVER (
        PARTITION BY supplier_id, item_id
        ORDER BY CASE WHEN COALESCE(idone,'') <> '' AND COALESCE(avg_length,0) > 0 THEN 0
                      WHEN COALESCE(idone,'') <> '' THEN 1
                      WHEN COALESCE(avg_length,0) > 0 THEN 2
                      ELSE 3 END,
                 COALESCE(available_slabs,0) DESC, id)"""


def collapse_item_slab_counts(conn: sqlite3.Connection) -> int:
    """Reduce each multi-row (supplier_id, item_id) to one honest slab count.

    Returns the number of items collapsed. Idempotent: the corrected figure is derived
    from the row structure (how many rows carry an `idone`), never from the slab values
    it overwrites, so a second pass recomputes the same number.

    Tiles are excluded — `available_slabs` holds a square-foot quantity for them, which
    the app already handles separately via _IS_TILE / _TILE_SF.
    """
    conn.execute("DROP TABLE IF EXISTS _collapse")
    conn.execute(f"""
        CREATE TEMP TABLE _collapse AS
        WITH ranked AS (
            SELECT id, supplier_id, item_id,
                   CASE WHEN COALESCE(idone,'') <> '' THEN 1 ELSE 0 END AS is_ident,
                   COALESCE(available_slabs,0) AS slabs,
                   {_REP_RANK} AS rn
              FROM materials
             WHERE {_NON_TILE}
        )
        SELECT supplier_id, item_id,
               MAX(CASE WHEN rn = 1 THEN id END)                       AS rep_id,
               -- count the slab-identifier rows; if a tenant sends none, fall back to the
               -- largest stated figure rather than inventing one.
               CASE WHEN SUM(is_ident) > 0 THEN SUM(is_ident) ELSE MAX(slabs) END AS corrected
          FROM ranked
         GROUP BY supplier_id, item_id
        HAVING COUNT(*) > 1""")
    n = int(conn.execute("SELECT COUNT(*) FROM _collapse").fetchone()[0])
    if n:
        # Zero the siblings first, then stamp the representative, so an item whose
        # representative is also a sibling of nothing still ends up with exactly one value.
        conn.execute(f"""
            UPDATE materials SET available_slabs = 0
             WHERE {_NON_TILE}
               AND id NOT IN (SELECT rep_id FROM _collapse)
               AND (supplier_id, item_id) IN (SELECT supplier_id, item_id FROM _collapse)""")
        conn.execute("""
            UPDATE materials SET available_slabs =
                (SELECT corrected FROM _collapse c WHERE c.rep_id = materials.id)
             WHERE id IN (SELECT rep_id FROM _collapse)""")
    conn.execute("DROP TABLE IF EXISTS _collapse")
    conn.commit()
    return n


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
