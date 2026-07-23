"""Recompute material_type and material_key for every stored material.

Use after improving the classifier in normalize.py — it re-derives the category
and cross-supplier match key from each row's stored category/subcategory/name,
so you don't have to re-crawl.

    python -m stonescan.reclassify                  # the app's database
    python -m stonescan.reclassify path/to/other.db # a specific database
"""

from __future__ import annotations

from . import db
from .normalize import (canonical_type, clean_color, derive_color_from_name, material_key,
                        normalize_thickness)


def recover_by_majority_vote(conn) -> int:
    """Give an 'Other' row the material type its same-name siblings agree on.

    The classifier reads one row at a time, so a listing whose own name/category carries
    no type word lands in 'Other' even when the same stone is confidently typed by five
    other suppliers. This pass is the cross-row view that per-row classification can't
    have. Only a STRICT majority wins — a tie leaves the row in 'Other' rather than
    picking arbitrarily, and a row with no typed siblings is never touched. Returns the
    number of rows recovered.

    material_key is recomputed alongside, because the key embeds the type.
    """
    tallies: dict[str, list[tuple[int, str]]] = {}
    for r in conn.execute(
        """SELECT m.name_norm AS name_norm, m.material_type AS mtype, COUNT(*) AS n
             FROM materials m
            WHERE m.material_type NOT IN ('Other', '')
              AND COALESCE(m.name_norm, '') <> ''
              AND m.name_norm IN (SELECT name_norm FROM materials
                                   WHERE material_type = 'Other'
                                     AND COALESCE(name_norm, '') <> '')
            GROUP BY m.name_norm, m.material_type"""
    ):
        tallies.setdefault(r["name_norm"], []).append((r["n"], r["mtype"]))

    winners: dict[str, str] = {}
    for name, votes in tallies.items():
        votes.sort(key=lambda v: v[0], reverse=True)
        if len(votes) == 1 or votes[0][0] > votes[1][0]:   # strict majority only
            winners[name] = votes[0][1]
    if not winners:
        return 0

    updates = []
    for t in conn.execute(
        "SELECT id, item_name, name_norm FROM materials WHERE material_type = 'Other'"
    ):
        win = winners.get(t["name_norm"])
        if win:
            updates.append((win, material_key(t["item_name"], win), t["id"]))
    if updates:
        conn.executemany(
            "UPDATE materials SET material_type = ?, material_key = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


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

    voted = recover_by_majority_vote(conn)
    if voted:
        print(f"Recovered {voted} 'Other' row(s) from same-name siblings.")

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


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.

    Takes an optional database path. Without one it rewrites the app's own database,
    which is why the argument exists at all: this pass mutates every row in place, so
    silently ignoring a path the caller supplied — as this did — points a dry run at
    live data.
    """
    import argparse

    ap = argparse.ArgumentParser(
        description="Re-derive material_type, material_key, colour and thickness in place.")
    ap.add_argument("db", nargs="?", default=str(db.DEFAULT_DB),
                    help="database to reclassify (default: the app's database)")
    args = ap.parse_args(argv)
    reclassify(args.db)


if __name__ == "__main__":
    main()
