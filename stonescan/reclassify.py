"""Recompute material_type and material_key for every stored material.

Use after improving the classifier in normalize.py — it re-derives the category
and cross-supplier match key from each row's stored category/subcategory/name,
so you don't have to re-crawl.

    python -m stonescan.reclassify
"""

from __future__ import annotations

from . import db
from .normalize import canonical_type, clean_color, material_key


def reclassify(db_path: str = str(db.DEFAULT_DB)) -> None:
    conn = db.connect(db_path)
    rows = conn.execute(
        "SELECT id, item_name, category, subcategory, color FROM materials"
    ).fetchall()
    print(f"Reclassifying {len(rows)} materials...")
    updates = []
    for r in rows:
        mtype = canonical_type(r["category"], r["subcategory"], r["item_name"])
        key = material_key(r["item_name"], mtype)
        updates.append((mtype, key, clean_color(r["color"]), r["id"]))
    conn.executemany(
        "UPDATE materials SET material_type = ?, material_key = ?, color = ? WHERE id = ?",
        updates,
    )
    conn.commit()

    s = db.stats(conn)
    print(f"Done. {s['materials']} materials, {s['unique_materials']} unique.")
    print("Top types:")
    for t in s["by_type"][:20]:
        print(f"  {t['material_type']:<24} {t['n']:>6}")
    conn.close()


if __name__ == "__main__":
    reclassify()
