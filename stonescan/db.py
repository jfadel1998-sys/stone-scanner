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


def location_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every stocking location with its slab/supplier totals, biggest first."""
    return [dict(r) for r in conn.execute(
        """SELECT location, COUNT(*) AS slabs,
                  COUNT(DISTINCT supplier_id) AS suppliers,
                  COUNT(DISTINCT supplier_id || '/' || item_id) AS products
           FROM slabs WHERE location <> ''
           GROUP BY location ORDER BY slabs DESC, location"""
    ).fetchall()]


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


if __name__ == "__main__":
    conn = init_db()
    print(json.dumps(stats(conn), indent=2))
