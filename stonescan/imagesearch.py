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

# CLIP ViT-B/32 preprocessing constants (HF CLIPImageProcessor).
_SIZE = 224
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
DIM = 512

_session = None
_out_index = 0  # which model output carries the 512-d image embedding


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
    print(f"Downloading CLIP model -> {p}")
    with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(p, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    print(f"Done ({p.stat().st_size / 1e6:.0f} MB).")


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
    ph = ",".join("?" for _ in url_score)
    mt_sql = " AND m.material_type = ?" if material_type else ""
    rows = conn.execute(
        f"""SELECT MIN(m.id) AS id, m.item_name, m.material_type, m.color, m.thickness,
                   m.material_key, m.image_url,
                   COALESCE(NULLIF(s.company,''), s.host) AS supplier_name, s.host AS supplier_host,
                   SUM(m.available_slabs) AS available_slabs
            FROM materials m JOIN suppliers s ON s.id = m.supplier_id
            WHERE m.image_url IN ({ph}){mt_sql}
            GROUP BY m.image_url""",
        (*url_score.keys(), *([material_type] if material_type else [])),
    ).fetchall()
    best: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        d["score"] = url_score.get(d["image_url"], 0.0)
        key = d["material_key"] or d["image_url"]
        if key not in best or d["score"] > best[key]["score"]:
            best[key] = d
    out = sorted(best.values(), key=lambda d: -d["score"])[:top_k]
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Build the search-by-photo image index.")
    ap.add_argument("--index", action="store_true", help="Embed not-yet-indexed catalog images.")
    ap.add_argument("--limit", type=int, default=0, help="Cap images this run (0 = all).")
    ap.add_argument("--download-model", action="store_true", help="Fetch the CLIP ONNX model.")
    ap.add_argument("--db", default=str(db.DEFAULT_DB))
    args = ap.parse_args()
    if args.download_model and not available():
        download_model()
    if not available():
        print(f"CLIP model missing at {model_path()} — run --download-model first.")
        return
    conn = db.init_db(args.db)
    if args.index:
        def prog(i, total, done):
            if i % 100 == 0 or i == total:
                print(f"  {i}/{total} fetched, {done} embedded")
        n = index_missing(conn, limit=args.limit, progress=prog)
        print(f"Indexed {n} new image(s).")
    print("index:", index_stats(conn))
    conn.close()


if __name__ == "__main__":
    main()
