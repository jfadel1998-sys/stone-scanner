"""Search-by-photo: CLIP image embeddings for visual similarity.

Upload a photo of a stone -> find catalog materials that *look like it* (pattern,
veining, tone), not just same-color. Uses a CLIP ViT-B/32 vision encoder exported to
ONNX and run on CPU via onnxruntime — no torch, so it stays bundle-friendly and
offline once the model is packaged.

Pipeline:
  * embed_image()  — CLIP-preprocess a PIL image + run the encoder -> a 512-d unit vector.
  * index (image_vectors table) — one vector per distinct catalog image_url, built by
    the indexer (see index_missing / `python -m stonescan.imagesearch --index`).
  * search() — embed the query photo, cosine-rank the indexed vectors, return the
    best-matching materials (grouped by material_key).

The model file lives at stonescan/models/clip/clip_vision.onnx (bundled into the
frozen app via stonescan.spec).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import numpy as np

from . import db
from .output import say

# CLIP ViT-B/32 preprocessing constants (HF CLIPImageProcessor).
_SIZE = 224
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
DIM = 512

_session = None
_out_index = 0  # which model output carries the 512-d image embedding

# How many of the closest IMAGES vote on the identification. Wide enough that a
# widely-stocked stone can show cross-supplier agreement, narrow enough that a
# distant 200th-ranked image doesn't get a say.
CONSENSUS_WINDOW = 40

# Minimum cosine lead the named stone must hold over the next different stone
# before we print a name at all. Measured, not guessed: on the cross-supplier
# holdout (tests/eval_photoid.py, n=600 x 2 seeds) everything below this gap is
# 21-23% of queries and only ~12% right, while 0.02-0.03 jumps to 89-96% and
# above 0.03 runs 97-100%. Two stones that score within a hair of each other
# are exactly the case where CLIP is matching "white with grey veins" rather
# than this stone, so the honest answer is no name.
MIN_ID_MARGIN = 0.02


def model_path() -> Path:
    """Model location: bundled under _MEIPASS when frozen, else the project tree."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "stonescan" / "models" / "clip" / "clip_vision.onnx"


def available() -> bool:
    return model_path().exists()


# Quantized CLIP ViT-B/32 vision encoder (transformers.js export). ~81 MB.
MODEL_URL = ("https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
             "onnx/vision_model_quantized.onnx")


def download_model() -> None:
    """Fetch the CLIP vision ONNX model to model_path() (git-ignored, ~81 MB)."""
    import httpx
    p = model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    say(f"Downloading CLIP model -> {p}")
    with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(p, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    say(f"Done ({p.stat().st_size / 1e6:.0f} MB).")


def _load():
    """Lazily create the ONNX session and pick the 512-d output."""
    global _session, _out_index
    if _session is not None:
        return _session
    import onnxruntime as ort  # imported lazily so the crawler doesn't pay for it
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, (__import__("os").cpu_count() or 2) - 1)
    so.log_severity_level = 3  # quiet
    _session = ort.InferenceSession(str(model_path()), sess_options=so,
                                    providers=["CPUExecutionProvider"])
    outs = _session.get_outputs()
    # Prefer the output whose last dim is the embedding size (image_embeds); some
    # exports also emit last_hidden_state, which we must not use.
    _out_index = next((i for i, o in enumerate(outs)
                       if o.shape and o.shape[-1] == DIM), 0)
    return _session


def _preprocess(img) -> np.ndarray:
    """PIL image -> CLIP tensor [1,3,224,224]: resize shortest edge to 224 (bicubic),
    center-crop, rescale, normalize."""
    from PIL import Image
    img = img.convert("RGB")
    w, h = img.size
    scale = _SIZE / min(w, h)
    nw, nh = max(_SIZE, round(w * scale)), max(_SIZE, round(h * scale))
    img = img.resize((nw, nh), Image.BICUBIC)
    left, top = (nw - _SIZE) // 2, (nh - _SIZE) // 2
    img = img.crop((left, top, left + _SIZE, top + _SIZE))
    arr = (np.asarray(img, dtype=np.float32) / 255.0 - _MEAN) / _STD  # HWC
    return np.ascontiguousarray(arr.transpose(2, 0, 1)[None], dtype=np.float32)


def embed_image(img) -> np.ndarray:
    """A unit-length 512-d CLIP embedding for a PIL image."""
    sess = _load()
    px = _preprocess(img)
    out = sess.run(None, {sess.get_inputs()[0].name: px})[_out_index]
    v = np.asarray(out, dtype=np.float32).reshape(-1)[:DIM]
    n = float(np.linalg.norm(v))
    return v / n if n else v


def embed_bytes(data: bytes) -> np.ndarray:
    from io import BytesIO
    from PIL import Image
    return embed_image(Image.open(BytesIO(data)))


def _vec_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _bytes_vec(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_UA = {"User-Agent": "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"}


# --- indexing: embed distinct catalog images not yet vectorized ------------------

def unindexed_urls(conn: sqlite3.Connection, limit: int = 0) -> list[str]:
    """Distinct catalog image URLs with no vector yet, most-stocked material first
    (so a bounded index still covers the inventory people actually browse)."""
    sql = """SELECT m.image_url FROM materials m
             LEFT JOIN image_vectors v ON v.image_url = m.image_url
             WHERE m.image_url <> '' AND v.image_url IS NULL
             GROUP BY m.image_url
             ORDER BY SUM(m.available_slabs) DESC"""
    rows = conn.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    return [r["image_url"] for r in rows]


def index_missing(conn: sqlite3.Connection, limit: int = 0, delay: float = 0.02,
                  progress=None) -> int:
    """Download + embed every not-yet-indexed catalog image. Resumable (skips ones
    already present) and bounded by `limit`. Returns how many were added."""
    import time

    import httpx
    urls = unindexed_urls(conn, limit)
    total = len(urls)
    done = 0
    with httpx.Client(headers=_UA, follow_redirects=True, timeout=30) as client:
        for i, url in enumerate(urls, 1):
            try:
                data = client.get(url).content
                vec = embed_bytes(data)
            except Exception:  # noqa: BLE001 - a bad image must not stop the index
                continue
            conn.execute(
                "INSERT OR REPLACE INTO image_vectors (image_url, vec, updated_at) VALUES (?,?,?)",
                (url, _vec_bytes(vec), _now()),
            )
            done += 1
            if done % 50 == 0:
                conn.commit()
            if progress:
                progress(i, total, done)
            if delay:
                time.sleep(delay)
    conn.commit()
    _invalidate_cache()
    return done


def _image_ssl_ctx():
    """SSL context that also accepts legacy-RSA hosts (UMI's apps.umistone.com offers
    only old ciphers OpenSSL 3 rejects with SSLV3_ALERT_HANDSHAKE_FAILURE). Lowering the
    security level is safe here — we're only fetching public catalog thumbnails."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _index_all_async(conn, concurrency: int, batch: int, limit: int, progress) -> int:
    """Concurrent indexer: download images in parallel (network is the bottleneck),
    embed each as it arrives. ~5-8x faster than the serial path for a full pass."""
    import asyncio

    import httpx
    urls = unindexed_urls(conn, limit)
    total, done = len(urls), 0
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=_UA, follow_redirects=True, timeout=30,
                                 verify=_image_ssl_ctx()) as client:
        async def fetch(u):
            async with sem:
                try:
                    r = await client.get(u)
                    r.raise_for_status()
                    return u, r.content
                except Exception:  # noqa: BLE001
                    return u, None
        for i in range(0, total, batch):
            for u, data in await asyncio.gather(*(fetch(u) for u in urls[i:i + batch])):
                if not data:
                    continue
                try:
                    vec = embed_bytes(data)          # CPU; fast relative to the downloads
                except Exception:  # noqa: BLE001
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO image_vectors (image_url, vec, updated_at) VALUES (?,?,?)",
                    (u, _vec_bytes(vec), _now()))
                done += 1
            conn.commit()
            if progress:
                progress(min(i + batch, total), total, done)
    _invalidate_cache()
    return done


def index_all(conn: sqlite3.Connection, concurrency: int = 12, limit: int = 0,
              progress=None) -> int:
    """Embed every not-yet-indexed catalog image, downloading concurrently."""
    import asyncio
    return asyncio.run(_index_all_async(conn, concurrency, 64, limit, progress))


def index_stats(conn: sqlite3.Connection) -> dict[str, int]:
    q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
    return {
        "indexed": q("SELECT COUNT(*) FROM image_vectors"),
        "images_total": q("SELECT COUNT(DISTINCT image_url) FROM materials WHERE image_url <> ''"),
    }


# --- search: cosine-rank the indexed vectors against a query embedding ------------

_URLS: list[str] | None = None
_MATRIX: np.ndarray | None = None
_MATRIX_COUNT = -1


def _invalidate_cache() -> None:
    global _MATRIX_COUNT
    _MATRIX_COUNT = -1


def _matrix(conn: sqlite3.Connection):
    """(urls, NxD matrix) of all indexed vectors, cached until the row count changes."""
    global _URLS, _MATRIX, _MATRIX_COUNT
    n = conn.execute("SELECT COUNT(*) FROM image_vectors").fetchone()[0]
    if _MATRIX is not None and n == _MATRIX_COUNT:
        return _URLS, _MATRIX
    rows = conn.execute("SELECT image_url, vec FROM image_vectors").fetchall()
    _URLS = [r["image_url"] for r in rows]
    _MATRIX = (np.stack([_bytes_vec(r["vec"]) for r in rows])
               if rows else np.zeros((0, DIM), dtype=np.float32))
    _MATRIX_COUNT = n
    return _URLS, _MATRIX


def search(conn: sqlite3.Connection, query_vec: np.ndarray, top_k: int = 60,
           material_type: str = "") -> list[dict[str, Any]]:
    """Return the best-matching materials for a query embedding, one row per
    material_key (deduped, best score kept), most-similar first."""
    urls, mat = _matrix(conn)
    if not len(urls):
        return []
    scores = mat @ query_vec.astype(np.float32)          # cosine (both unit-norm)
    order = np.argsort(-scores)[:max(top_k * 6, 200)]    # oversample for dedupe/filter
    ranked = [(urls[i], float(scores[i])) for i in order]
    url_score = {u: s for u, s in ranked}
    url_rank = {u: i for i, (u, _) in enumerate(ranked)}
    ph = ",".join("?" for _ in url_score)
    mt_sql = " AND m.material_type = ?" if material_type else ""
    rows = conn.execute(
        f"""SELECT MIN(m.id) AS id, m.item_name, m.material_type, m.color, m.thickness,
                   m.material_key, m.image_url,
                   COALESCE(NULLIF(s.company,''), s.host) AS supplier_name, s.host AS supplier_host,
                   SUM(m.available_slabs) AS available_slabs
            FROM materials m JOIN suppliers s ON s.id = m.supplier_id
            WHERE m.image_url IN ({ph}){mt_sql}
                  AND m.material_type <> 'Accessory / Non-Slab'
            GROUP BY m.image_url""",
        (*url_score.keys(), *([material_type] if material_type else [])),
    ).fetchall()

    # Consensus is measured BEFORE the dedupe, over the closest images, because the
    # deduped list has exactly one row per material by construction — counting
    # agreement on it would always say "1 of N, from 1 supplier" no matter how many
    # suppliers photographed the same stone. `image_matches` / `distinct_suppliers`
    # carry that signal forward for identify().
    agree: dict[str, dict] = {}
    for r in rows:
        if url_rank.get(r["image_url"], 10 ** 6) >= CONSENSUS_WINDOW:
            continue
        key = r["material_key"] or r["image_url"]
        g = agree.setdefault(key, {"n": 0, "suppliers": set()})
        g["n"] += 1
        if r["supplier_name"]:
            g["suppliers"].add(r["supplier_name"])
    window_total = sum(g["n"] for g in agree.values()) or 1

    best: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        d["score"] = url_score.get(d["image_url"], 0.0)
        key = d["material_key"] or d["image_url"]
        if key not in best or d["score"] > best[key]["score"]:
            best[key] = d
    for key, d in best.items():
        g = agree.get(key)
        d["image_matches"] = g["n"] if g else 0
        d["distinct_suppliers"] = len(g["suppliers"]) if g else 0
        d["window_total"] = window_total
    out = sorted(best.values(), key=lambda d: -d["score"])[:top_k]
    return out


def identify(results: list[dict[str, Any]], top_n: int = 12) -> dict[str, Any]:
    """Turn a ranked list of visual matches into an identification.

    A flat grid of "94% match / 93% match / 91% match" makes the user do the
    inference. What actually answers "what is this stone?" is the CONSENSUS across
    the top matches: if nine of the top twelve are Taj Mahal from nine different
    suppliers, that is a far stronger claim than any single 94% score, because
    nine suppliers photographing their own slabs independently agreed.

    So candidates are scored on three things a single similarity number misses:
      * how many of the top matches share the trade name (agreement),
      * how well they scored (mean similarity of that name's matches),
      * how many DISTINCT suppliers contributed (independent corroboration —
        five photos from one supplier is one opinion, not five).

    `confidence` is deliberately conservative and never says "certain": CLIP
    matches appearance, and different stones genuinely look alike. It answers
    "how much does the catalog agree", which is a claim we can actually support.
    And when the top two stones score within MIN_ID_MARGIN of each other it
    declines to answer at all ("none") — see that constant for the measurement.
    Callers must treat "none" as no identification even though `known` is True:
    the ranked matches are still worth showing, just not a name.
    """
    top = [r for r in results[:top_n] if r.get("material_key")]
    if not top:
        return {"known": False, "candidates": []}

    window = max((r.get("window_total") or 0) for r in top) or len(top)
    cands = []
    for r in top:
        # image_matches / distinct_suppliers are computed by search() over the raw
        # image ranking; recomputing them here would be meaningless because `results`
        # holds exactly one row per material.
        n = max(r.get("image_matches") or 1, 1)
        distinct = max(r.get("distinct_suppliers") or 1, 1)
        share = n / window
        # Corroboration saturates: the 2nd and 3rd independent supplier are worth a
        # lot, the 8th very little. sqrt keeps one supplier's many photos of the same
        # slab from outranking genuine cross-supplier agreement.
        corrob = (distinct / n) ** 0.5
        cands.append({
            "material_key": r["material_key"],
            "name": (r["material_key"].split("|")[0] or r.get("item_name", "")).title(),
            "material_type": r.get("material_type", ""),
            "matches": n,
            "distinct_suppliers": distinct,
            "share": share,
            "best_score": r.get("score", 0.0),
            "rank_score": r.get("score", 0.0) * (0.55 + 0.45 * share) * (0.7 + 0.3 * corrob),
            "example": r,
        })
    cands.sort(key=lambda g: -g["rank_score"])

    best = cands[0]
    runner = cands[1] if len(cands) > 1 else None
    rank_margin = best["rank_score"] - (runner["rank_score"] if runner else 0.0)
    # How far ahead the stone we are about to name actually is, in raw similarity.
    # Anchored on `best` — cands was re-sorted by rank_score, so cands[0] is not
    # necessarily results[0], and measuring from the wrong row would grade a stone
    # we aren't showing. cands holds one row per material_key, so the runner-up is
    # already a different stone. It can also out-score the winner (rank_score
    # rewards agreement over similarity), and that negative margin is meaningful:
    # on the holdout it was wrong every single time, n=91.
    # With no runner-up there is nothing to confuse it with — no ambiguity to
    # measure, rather than zero lead.
    score_margin = (best["best_score"] - runner["best_score"]) if runner else 1.0
    # Thresholds are empirical and intentionally cautious — the cost of a confident
    # wrong identification is a user ordering the wrong stone. A near-perfect single
    # match still counts as strong: an exact photo match is real evidence even when
    # only one supplier has photographed that stone.
    if score_margin < MIN_ID_MARGIN:
        # First rung on purpose: a top match this crowded is not worth naming even
        # when it would otherwise clear the "strong" bar on score alone.
        confidence, blurb = "none", "the closest matches are too evenly scored to name one"
    elif best["best_score"] >= 0.95 and rank_margin >= 0.02:
        confidence, blurb = "strong", "a near-exact image match"
    elif best["share"] >= 0.25 and best["best_score"] >= 0.80 and rank_margin >= 0.03:
        confidence, blurb = "strong", "the closest matches agree"
    elif best["share"] >= 0.12 or best["best_score"] >= 0.75:
        confidence, blurb = "likely", "several close matches point the same way"
    else:
        confidence, blurb = "uncertain", "the closest matches disagree"
    return {
        "known": True,
        "best": best,
        "confidence": confidence,
        "blurb": blurb,
        "score_margin": score_margin,
        "candidates": cands[:5],
        "considered": best.get("matches", 0),
        "window": window,
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Build the search-by-photo image index.")
    ap.add_argument("--index", action="store_true", help="Embed not-yet-indexed catalog images (concurrent).")
    ap.add_argument("--limit", type=int, default=0, help="Cap images this run (0 = all).")
    ap.add_argument("--concurrency", type=int, default=12, help="Parallel image downloads.")
    ap.add_argument("--download-model", action="store_true", help="Fetch the CLIP ONNX model.")
    ap.add_argument("--db", default=str(db.DEFAULT_DB))
    args = ap.parse_args()
    if args.download_model and not available():
        download_model()
    if not available():
        say(f"CLIP model missing at {model_path()} — run --download-model first.")
        return
    conn = db.init_db(args.db)
    if args.index:
        def prog(i, total, done):
            say(f"  {i}/{total} fetched, {done} embedded ({100 * i // max(total, 1)}%)")
        n = index_all(conn, concurrency=args.concurrency, limit=args.limit, progress=prog)
        say(f"Indexed {n} new image(s).")
    say("index:", index_stats(conn))
    conn.close()


if __name__ == "__main__":
    main()
