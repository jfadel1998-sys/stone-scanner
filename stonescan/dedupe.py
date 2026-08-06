"""Find (and apply) cross-supplier material_key merges — the data-quality curation
engine behind the /quality review pages.

Two candidate generators, both feeding db.material_aliases (see db.apply_aliases):

  * conflict_clusters — the high-value one. The SAME base name classified under
    DIFFERENT material types across suppliers ("Taj Mahal" sold as quartzite by one,
    granite/marble/quartz by others) fragments one physical stone into several
    material_keys that never group together on the canonical page. A shared name is a
    strong signal, so these are high-confidence merges to the dominant type.
  * fuzzy_clusters — spelling variants of the same name+type ("Calacata Gold" vs
    "Calacatta Gold") that survived the qualifier-strip in normalize.material_key.
    Uses the smartsearch alias lexicon + punctuation folding to normalize, so a group
    only forms when distinct raw spellings collapse to one canonical form — precise,
    not a blind edit-distance sweep.

A merge is stored as an alias (alias_key -> canonical_key) keyed on the *computed*
material_key, so it survives re-crawls. "Not the same" is remembered as a rejection
signature so the pair is never proposed again.

    python -m stonescan.dedupe                 # report candidate counts + a preview
    python -m stonescan.dedupe --apply         # re-fold existing aliases (post-crawl)
    python -m stonescan.dedupe --auto-conflicts # merge only landslide conflicts (>=4x dominance)
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from . import db, smartsearch

# Types that are genuinely "we don't know" — never treat these as a real, distinct
# classification when deciding a conflict's dominant type.
_WEAK_TYPES = {"other", "natural stone", ""}


def _key_aggregates(conn) -> list[dict[str, Any]]:
    """One aggregated row per material_key: how widely stocked, best photo, a sample."""
    rows = conn.execute(
        """SELECT material_key AS mk, MAX(material_type) AS mt,
                  COUNT(DISTINCT supplier_id) AS suppliers,
                  COALESCE(SUM(available_slabs), 0) AS slabs,
                  COUNT(*) AS listings, MIN(id) AS sample_id,
                  MAX(image_url) AS image_url, MAX(item_name) AS item_name
           FROM materials WHERE material_key <> ''
           GROUP BY material_key"""
    ).fetchall()
    out = []
    for r in rows:
        base, _, typ = r["mk"].rpartition("|")
        out.append({**dict(r), "base": base, "type": typ})
    return out


def _display(members: list[dict]) -> str:
    """The spelling to show for a cluster: the most widely-stocked member's name."""
    best = max(members, key=lambda m: (m["suppliers"], m["slabs"]))
    return best["item_name"] or best["base"].title()


def _first_image(members: list[dict]) -> str:
    for m in sorted(members, key=lambda m: (-m["suppliers"], -m["slabs"])):
        if m["image_url"]:
            return m["image_url"]
    return ""


def conflict_clusters(conn, limit: int = 200) -> list[dict[str, Any]]:
    """Base names offered under more than one material type across suppliers.

    Ranked by reach (total supplier-listings), so the stones that fragment the most
    of the catalog surface first. The canonical target is the type with the widest
    stock, ignoring the weak 'Other'/'Natural Stone' buckets when a real type exists.
    """
    rej = db.rejections(conn)
    by_base: dict[str, list[dict]] = defaultdict(list)
    for r in _key_aggregates(conn):
        by_base[r["base"]].append(r)

    clusters = []
    for base, members in by_base.items():
        types = {m["type"] for m in members}
        if len(types) < 2 or f"conflict:{base}" in rej:
            continue
        # Pick the dominant (canonical) member: widest stock, but prefer a real type
        # over Other/Natural Stone unless only weak types exist.
        strong = [m for m in members if m["type"] not in _WEAK_TYPES]
        pool = strong or members
        dominant = max(pool, key=lambda m: (m["suppliers"], m["slabs"]))
        members.sort(key=lambda m: (-m["suppliers"], -m["slabs"]))
        clusters.append({
            "sig": f"conflict:{base}",
            "base": base,
            "display": _display(members),
            "canonical_key": dominant["mk"],
            "canonical_type": dominant["mt"],
            "image_url": _first_image(members),
            "members": members,
            "n_types": len(types),
            "suppliers": sum(m["suppliers"] for m in members),
            "slabs": sum(m["slabs"] for m in members),
        })
    clusters.sort(key=lambda c: (-c["suppliers"], -c["slabs"]))
    return clusters[:limit]


def _norm_base(base: str) -> str:
    """Fold a base name to a spelling-insensitive signature: known trade misspellings
    (via the smartsearch alias lexicon) unified, then punctuation/space stripped."""
    b = smartsearch.apply_aliases(base.lower())
    return re.sub(r"[^a-z0-9]", "", b)


def fuzzy_clusters(conn, limit: int = 200) -> list[dict[str, Any]]:
    """Same material type, distinct raw spellings that collapse to one signature."""
    rej = db.rejections(conn)
    by_norm: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in _key_aggregates(conn):
        norm = _norm_base(r["base"])
        if norm:
            by_norm[(r["type"], norm)].append(r)

    clusters = []
    for (typ, norm), members in by_norm.items():
        if len({m["base"] for m in members}) < 2:
            continue
        members.sort(key=lambda m: (-m["suppliers"], -m["slabs"]))
        # Sign on the STABLE (type, normalized-name) — not the member key set, which
        # shifts as catalogs change or members get merged, resurrecting a rejection.
        sig = f"fuzzy:{typ}:{norm}"
        if sig in rej:
            continue
        dominant = members[0]
        clusters.append({
            "sig": sig,
            "display": _display(members),
            "canonical_key": dominant["mk"],
            "canonical_type": dominant["mt"],
            "image_url": _first_image(members),
            "members": members,
            "suppliers": sum(m["suppliers"] for m in members),
            "slabs": sum(m["slabs"] for m in members),
        })
    clusters.sort(key=lambda c: (-c["suppliers"], -c["slabs"]))
    return clusters[:limit]


def pending_counts(conn) -> dict[str, int]:
    """How many merge candidates are waiting, without building the clusters.

    Exists because the ⚙ menu badge renders on EVERY page in the app, and asking the
    cluster builders for a count is not affordable there: measured on the 174k-row
    catalog, `conflict_clusters` takes 14.5s and `fuzzy_clusters` 2.9s. Almost none of
    that is the data — `_key_aggregates` is 0.38s. The cost is `_first_image`, one query
    per cluster, which a count has no use for. Skipping it and sharing a single
    `_key_aggregates` call brings both counts to ~0.4s together.

    The grouping and rejection rules are duplicated from the two builders rather than
    factored out of them, because the builders' member sorting and canonical-pick logic
    is genuinely irrelevant here. `test_pending_counts_match_the_cluster_builders` asserts
    the two agree, so the duplication cannot drift silently.
    """
    rej = db.rejections(conn)
    by_base: dict[str, set[str]] = defaultdict(set)
    by_norm: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in _key_aggregates(conn):
        by_base[r["base"]].add(r["type"])
        norm = _norm_base(r["base"])
        if norm:
            by_norm[(r["type"], norm)].add(r["base"])

    conflicts = sum(1 for base, types in by_base.items()
                    if len(types) >= 2 and f"conflict:{base}" not in rej)
    fuzzy = sum(1 for (typ, norm), bases in by_norm.items()
                if len(bases) >= 2 and f"fuzzy:{typ}:{norm}" not in rej)
    return {"conflicts": conflicts, "fuzzy": fuzzy, "total": conflicts + fuzzy}


def other_samples(conn, material_type: str = "Other", limit: int = 80) -> list[dict[str, Any]]:
    """Most-common item names sitting in the Other (or Accessory) bucket — the
    worklist for improving the classifier / spotting misfiled real stones."""
    return [dict(r) for r in conn.execute(
        """SELECT MIN(id) AS id, item_name, COUNT(*) AS n,
                  COUNT(DISTINCT supplier_id) AS suppliers, MAX(image_url) AS image_url
           FROM materials WHERE material_type = ?
           GROUP BY name_norm ORDER BY n DESC, suppliers DESC LIMIT ?""",
        (material_type, limit),
    ).fetchall()]


def apply_conflict_cluster(conn, cluster: dict) -> int:
    """Fold every non-canonical member of a conflict/fuzzy cluster into the canonical."""
    n = 0
    for m in cluster["members"]:
        if m["mk"] != cluster["canonical_key"]:
            db.add_alias(conn, m["mk"], cluster["canonical_key"],
                         cluster["canonical_type"], note=cluster.get("sig", ""))
            n += 1
    return n


def auto_conflicts(conn, dominance: float = 4.0) -> int:
    """Merge only landslide conflicts — where the CANONICAL member (the strong-type,
    most-stocked key we actually fold into) outweighs every other member combined by
    `dominance`x. Conservative bulk pass for the obvious cases; folds rows in place.

    The gate must measure the canonical, not merely the biggest member: when a weak
    'Other' bucket is the largest, the canonical is a smaller strong-type key, and
    gating on the big weak bucket would auto-retype low-signal rows to a minority
    classification — exactly what this conservative pass exists to avoid.
    """
    merged = 0
    for c in conflict_clusters(conn, limit=100000):
        canon = next(m for m in c["members"] if m["mk"] == c["canonical_key"])
        rest = sum(m["suppliers"] for m in c["members"] if m["mk"] != c["canonical_key"])
        if rest and canon["suppliers"] >= dominance * rest:
            merged += apply_conflict_cluster(conn, c)
    if merged:
        db.apply_aliases(conn)  # --auto-conflicts must actually fold, not just record
    return merged


def report(conn) -> None:
    conflicts = conflict_clusters(conn, limit=100000)
    fuzzy = fuzzy_clusters(conn, limit=100000)
    qs = db.quality_stats(conn)
    print(f"materials         : {qs['materials']}")
    print(f"unique materials  : {qs['unique_materials']}")
    print(f"aliases applied   : {qs['aliases']}   rejections: {qs['rejections']}")
    print(f"Other bucket      : {qs['other']}     Accessory: {qs['accessory']}")
    print(f"no colour         : {qs['no_color']}")
    print()
    print(f"type-conflict clusters : {len(conflicts)}")
    print(f"fuzzy-spelling clusters: {len(fuzzy)}")
    print("\nTop type conflicts (base name -> types):")
    for c in conflicts[:12]:
        types = ", ".join(f"{m['type']}({m['suppliers']})" for m in c["members"])
        print(f"  {c['display'][:28]:<28} {types}")
    print("\nTop fuzzy clusters:")
    for c in fuzzy[:12]:
        spellings = " / ".join(sorted({m['base'] for m in c['members']}))
        print(f"  {spellings[:70]}")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Cross-supplier material merge curation.")
    ap.add_argument("--db", default=str(db.DEFAULT_DB))
    ap.add_argument("--apply", action="store_true",
                    help="Re-fold existing confirmed aliases into materials (post-crawl).")
    ap.add_argument("--auto-conflicts", action="store_true",
                    help="Auto-merge landslide type conflicts (dominant >= 4x the rest).")
    args = ap.parse_args()
    conn = db.init_db(args.db)
    if args.auto_conflicts:
        n = auto_conflicts(conn)
        print(f"Auto-merged {n} conflict member key(s).")
    if args.apply:
        print(f"Folded {db.apply_aliases(conn)} row(s) via {db.quality_stats(conn)['aliases']} alias(es).")
    report(conn)
    conn.close()


if __name__ == "__main__":
    main()
