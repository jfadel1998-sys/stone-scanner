"""Recompute material_type and material_key for every stored material.

Use after improving the classifier in normalize.py — it re-derives the category
and cross-supplier match key from each row's stored category/subcategory/name,
so you don't have to re-crawl.

    python -m stonescan.reclassify                  # the app's database
    python -m stonescan.reclassify path/to/other.db # a specific database
"""

from __future__ import annotations

from . import db
from .normalize import (canonical_type, clean_color, derive_color_from_name, derive_finish,
                        material_key, normalize_thickness, remap_key)


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
    # init_db (not connect) so the schema is current before we mutate — this CLI can run
    # against a DB the app hasn't opened, and it needs columns added after first release
    # (e.g. suppliers.mirror_of) to exist before detect_mirrors writes them.
    conn = db.init_db(db_path)
    # Repair any millimetre-as-centimetre thicknesses first (idempotent; init_db's migrate
    # also runs this, so on a current DB it's a no-op second pass).
    fixed = db.fix_mm_thickness(conn)
    if fixed:
        print(f"Repaired {fixed} millimetre-as-centimetre thickness value(s).")
    rows = conn.execute(
        "SELECT id, item_name, category, subcategory, color, thickness, uom, finish "
        "FROM materials"
    ).fetchall()
    print(f"Reclassifying {len(rows)} materials...")
    updates = []
    derived = 0
    for r in rows:
        mtype = canonical_type(r["category"], r["subcategory"], r["item_name"])
        key = material_key(r["item_name"], mtype)
        color = clean_color(r["color"]) or derive_color_from_name(r["item_name"])
        # Re-derive thickness too, so an improved parser reaches stored rows without a
        # re-crawl. A stored value carries its own unit, so this is a no-op on correct
        # rows and only fills gaps (e.g. a thickness readable from the name).
        thickness = normalize_thickness(r["thickness"], r["item_name"], r["uom"] or "")
        # Finish is 96.3% empty as crawled while 35% of names state one. Derived here rather
        # than in the crawler so it reaches the stored catalog without a re-crawl, and it
        # cannot affect `key` above — that is built from item_name by material_key().
        finish = derive_finish(r["item_name"], r["finish"] or "")
        if finish and not (r["finish"] or "").strip():
            derived += 1
        updates.append((mtype, key, color, thickness, finish, r["id"]))
    conn.executemany(
        "UPDATE materials SET material_type = ?, material_key = ?, color = ?, "
        "thickness = ?, finish = ? WHERE id = ?",
        updates,
    )
    conn.commit()
    if derived:
        print(f"Derived a finish for {derived} row(s) that had none.")

    voted = recover_by_majority_vote(conn)
    if voted:
        print(f"Recovered {voted} 'Other' row(s) from same-name siblings.")

    # Stored merges hold material_keys built by an OLDER version of the key rules. Re-derive
    # both sides FIRST, or apply_aliases below folds rows into a key nothing produces any
    # more — orphaning the merge rather than undoing it. Idempotent: already-current keys
    # remap to themselves.
    moved = db.remap_aliases(conn, remap_key)
    if moved["remapped"] or moved["dropped"]:
        print(f"Re-derived {moved['remapped']} merge(s) onto current keys; "
              f"dropped {moved['dropped']} the new rules already unify.")

    # Re-fold any curator-confirmed merges: reclassify recomputes material_key from
    # scratch, which would otherwise undo every merge until the next crawl.
    folded = db.apply_aliases(conn)
    if folded:
        print(f"Re-applied {folded} row(s) from {db.quality_stats(conn)['aliases']} confirmed merge(s).")

    # Re-detect duplicate-catalog storefronts (same discipline as merges: recompute so a
    # new mirror is flagged and one that no longer matches is un-flagged).
    mirrors = db.detect_mirrors(conn)
    if mirrors:
        print(f"Detected {len(mirrors)} mirror storefront(s) "
              f"(excluded from supplier counts): "
              + ", ".join(f"{m['mirror_host']} -> {m['canonical_host']}" for m in mirrors))

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
