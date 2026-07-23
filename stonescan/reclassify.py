"""Recompute material_type and material_key for every stored material.

Use after improving the classifier in normalize.py — it re-derives the category
and cross-supplier match key from each row's stored category/subcategory/name,
so you don't have to re-crawl.

    python -m stonescan.reclassify
"""

from __future__ import annotations

from . import db
from .normalize import (canonical_type, clean_color, derive_color_from_name, material_key,
                        normalize_thickness)


def reclassify(db_path: str = str(db.DEFAULT_DB)) -> None:
    conn = db.connect(db_path)
    # Repair any millimetre-as-centimetre thicknesses first (idempotent, and this CLI can
    # run against a DB the app hasn't opened, which is where init_db would have done it).
    fixed = db.fix_mm_thickness(conn)
    if fixed:
        print(f"Repaired {fixed} millimetre-as-centimetre thickness value(s).")
    rows = conn.execute(
        "SELECT id, item_name, category, subcategory, color, thickness, uom FROM materials"
    ).fetchall()
    print(f"Reclassifying {len(rows)} materials...")
    updates = []
    for r in rows:
        mtype = canonical_type(r["category"], r["subcategory"], r["item_name"])
        key = material_key(r["item_name"], mtype)
        color = clean_color(r["color"]) or derive_color_from_name(r["item_name"])
        # Re-derive thickness too, so an improved parser reaches stored rows without a
        # re-crawl. A stored value carries its own unit, so this is a no-op on correct
        # rows and only fills gaps (e.g. a thickness readable from the name).
        thickness = normalize_thickness(r["thickness"], r["item_name"], r["uom"] or "")
        updates.append((mtype, key, color, thickness, r["id"]))
    conn.executemany(
        "UPDATE materials SET material_type = ?, material_key = ?, color = ?, "
        "thickness = ? WHERE id = ?",
        updates,
    )
    conn.commit()

    # Re-fold any curator-confirmed merges: reclassify recomputes material_key from
    # scratch, which would otherwise undo every merge until the next crawl.
    folded = db.apply_aliases(conn)
    if folded:
        print(f"Re-applied {folded} row(s) from {db.quality_stats(conn)['aliases']} confirmed merge(s).")

    # Rebuild the per-product rollup so the search fast path reflects the new keys.
    db.rebuild_product_rollup(conn)

    s = db.stats(conn)
    print(f"Done. {s['materials']} materials, {s['unique_materials']} unique.")
    print("Top types:")
    for t in s["by_type"][:20]:
        print(f"  {t['material_type']:<24} {t['n']:>6}")
    conn.close()


if __name__ == "__main__":
    reclassify()
