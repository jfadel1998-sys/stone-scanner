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
import sqlite3
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
"""


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release; applied to pre-existing databases.
_MIGRATIONS = {
    "suppliers": {"image_base": "TEXT", "slabs_cached_at": "TEXT", "slab_count": "INTEGER DEFAULT 0",
                  "phone": "TEXT", "email": "TEXT", "locations": "TEXT"},
    "materials": {"new_arrival": "INTEGER DEFAULT 0", "image_url": "TEXT", "locations": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def init_db(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


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
    n = 0
    for r in rows:
        conn.execute(sql, tuple(r.get(c) for c in cols))
        n += 1
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


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    s = {}
    s["suppliers"] = conn.execute("SELECT COUNT(*) c FROM suppliers WHERE item_count > 0").fetchone()["c"]
    s["materials"] = conn.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"]
    s["unique_materials"] = conn.execute(
        "SELECT COUNT(DISTINCT material_key) c FROM materials WHERE material_key <> ''"
    ).fetchone()["c"]
    s["by_type"] = [
        dict(r) for r in conn.execute(
            "SELECT material_type, COUNT(*) n FROM materials "
            "WHERE material_type <> '' GROUP BY material_type ORDER BY n DESC"
        ).fetchall()
    ]
    row = conn.execute(
        "SELECT MAX(last_crawled) t FROM suppliers WHERE item_count > 0"
    ).fetchone()
    s["last_updated"] = row["t"] if row else None
    s["with_images"] = conn.execute(
        "SELECT COUNT(*) c FROM materials WHERE image_url <> ''"
    ).fetchone()["c"]
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
                  MAX(material_type), MAX(color), SUM(available_slabs), MAX(image_url)
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


def supplier_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-supplier crawl state for the health page.

    `status` separates the cases that matter operationally:
      ok      — has data, last crawl clean
      stale   — has data but the last refresh errored (old data still serving)
      broken  — errored and no data at all
      empty   — no error, but the catalog returned nothing (may be legitimately empty)
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out = []
    for r in conn.execute(
        """SELECT host, COALESCE(NULLIF(company,''), host) AS name, item_count,
                  slab_count, last_crawled, last_error
           FROM suppliers ORDER BY name"""
    ):
        d = dict(r)
        err = bool(d["last_error"])
        has = (d["item_count"] or 0) > 0
        d["status"] = ("ok" if not err else "stale") if has else ("broken" if err else "empty")
        age_h = None
        if d["last_crawled"]:
            try:
                t = datetime.fromisoformat(d["last_crawled"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                age_h = (now - t).total_seconds() / 3600
            except ValueError:
                pass
        d["age_hours"] = age_h
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
