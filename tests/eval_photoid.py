"""Cross-supplier holdout evaluation of the photo-ID verdict.

Not a unit test — it needs the git-ignored catalog DB and its image_vector index, so it
SKIPS cleanly when either is missing (see `available()`), and `tests/test_reference.py`
calls it only when the data is there. Run the full sweep directly:

    python -m tests.eval_photoid --n 600 --seed 7

Method: take one indexed catalog photo as the query, mask every image belonging to that
photo's OWN supplier, and ask the real search()/identify() to name the stone. Masking the
supplier is what makes it honest — otherwise the query matches itself at cosine 1.0 and
every verdict looks perfect. A verdict counts as correct when the stone it names is the
material_key the query image actually came from.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stonescan import db, imagesearch  # noqa: E402


def available(conn=None) -> bool:
    """True when the catalog and a non-trivial vector index are both present."""
    own = conn is None
    try:
        conn = conn or db.connect()
        n = conn.execute("SELECT COUNT(*) FROM image_vectors").fetchone()[0]
        return n >= 500
    except Exception:  # noqa: BLE001 - missing DB/table is simply "not available"
        return False
    finally:
        if own and conn is not None:
            conn.close()


def _index(conn):
    """url -> (supplier_id, material_key) for every indexed image that has a material."""
    return {r["image_url"]: (r["supplier_id"], r["material_key"]) for r in conn.execute(
        """SELECT DISTINCT v.image_url, m.supplier_id, m.material_key
             FROM image_vectors v JOIN materials m ON m.image_url = v.image_url
            WHERE COALESCE(m.material_key,'') <> ''
              AND m.material_type <> 'Accessory / Non-Slab'""")}


def run(n: int = 300, seed: int = 7, conn=None, answerable_only: bool = True) -> list[dict]:
    """Return one record per query: the verdict, whether it was right, and its margin."""
    own = conn is None
    conn = conn or db.connect()
    try:
        meta = _index(conn)
        urls, mat = imagesearch._matrix(conn)
        pos = {u: i for i, u in enumerate(urls)}
        pool = [u for u in meta if u in pos]
        # Only stones another supplier also carries are answerable at all once the
        # query's own supplier is masked; anything else would measure the index's
        # coverage rather than the verdict's honesty.
        by_key: dict[str, set] = {}
        for u, (sid, key) in meta.items():
            by_key.setdefault(key, set()).add(sid)
        if answerable_only:
            pool = [u for u in pool if len(by_key[meta[u][1]]) > 1]

        rng = random.Random(seed)
        rng.shuffle(pool)
        out = []
        saved_urls, saved_mat, saved_count = imagesearch._URLS, imagesearch._MATRIX, imagesearch._MATRIX_COUNT
        try:
            for url in pool[:n]:
                sid, truth = meta[url]
                qvec = mat[pos[url]]
                keep = [i for i, u in enumerate(urls)
                        if meta.get(u, (None, None))[0] != sid]
                # Feed search() a matrix with the query's supplier removed.
                imagesearch._URLS = [urls[i] for i in keep]
                imagesearch._MATRIX = mat[keep]
                imagesearch._MATRIX_COUNT = len(keep)
                res = imagesearch.search(conn, qvec, top_k=60)
                if not res:
                    continue
                v = imagesearch.identify(res)
                if not v.get("known"):
                    continue
                best = v["best"]
                cands = v.get("candidates") or []
                # Cosine margin from the CHOSEN best to the next different stone. cands is
                # one row per material_key, so cands[1] is already a different stone.
                gap = (best["best_score"] - cands[1]["best_score"]) if len(cands) > 1 else 1.0
                out.append({
                    "confidence": v["confidence"],
                    "correct": best["material_key"] == truth,
                    "margin": v.get("score_margin", gap),
                    "best_score": best["best_score"],
                })
        finally:
            imagesearch._URLS, imagesearch._MATRIX = saved_urls, saved_mat
            imagesearch._MATRIX_COUNT = saved_count
        return out
    finally:
        if own:
            conn.close()


def summarize(recs: list[dict]) -> dict:
    total = len(recs) or 1
    out = {"n": len(recs)}
    for label in ("strong", "likely", "uncertain", "none"):
        sub = [r for r in recs if r["confidence"] == label]
        out[label] = {
            "shown_pct": 100.0 * len(sub) / total,
            "precision_pct": (100.0 * sum(r["correct"] for r in sub) / len(sub)) if sub else 0.0,
            "n": len(sub),
        }
    named = [r for r in recs if r["confidence"] in ("strong", "likely")]
    out["named_pct"] = 100.0 * len(named) / total
    out["named_precision_pct"] = (100.0 * sum(r["correct"] for r in named) / len(named)) if named else 0.0
    out["top1_pct"] = 100.0 * sum(r["correct"] for r in recs) / total
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--buckets", action="store_true", help="Accuracy by score margin.")
    ap.add_argument("--all", action="store_true",
                    help="Include stones only one supplier carries (unanswerable once "
                         "that supplier is masked) — measures coverage, not calibration.")
    a = ap.parse_args()
    if not available():
        print("no catalog / vector index present — nothing to evaluate")
        return
    recs = run(a.n, a.seed, answerable_only=not a.all)
    s = summarize(recs)
    print("n=%d   top-1 correct: %.1f%%" % (s["n"], s["top1_pct"]))
    for label in ("strong", "likely", "uncertain", "none"):
        d = s[label]
        print("  %-10s shown %5.1f%%  precision %5.1f%%  (n=%d)"
              % (label, d["shown_pct"], d["precision_pct"], d["n"]))
    print("  %-10s shown %5.1f%%  precision %5.1f%%" % ("NAMED", s["named_pct"], s["named_precision_pct"]))
    if a.buckets:
        print("\naccuracy by score margin to the next different stone:")
        edges = [0.0, 0.005, 0.02, 0.05, 0.10, 1.0]
        for lo, hi in zip(edges, edges[1:]):
            sub = [r for r in recs if lo <= r["margin"] < hi]
            if sub:
                print("   %.3f-%.3f  n=%-4d accuracy %5.1f%%"
                      % (lo, hi, len(sub), 100.0 * sum(r["correct"] for r in sub) / len(sub)))


if __name__ == "__main__":
    main()
