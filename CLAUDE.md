# Stone Scanner — project guide

Cross-supplier stone material search. Aggregates the **public** online catalogs of
stone suppliers on the Stone Profits platform (`*.stoneprofitsweb.com`) into one
categorized, searchable database so you can compare materials across suppliers.

**Scope constraint (important):** public catalogs only — the same pages customers
browse. No logins, no private data. Rate-limited, identifiable crawler.

## Run it

```powershell
# dev: activate venv first (.\.venv\Scripts\Activate.ps1) or call the venv python directly
.\.venv\Scripts\python.exe -m uvicorn stonescan.web.app:app --port 8000   # search UI at 127.0.0.1:8000
.\.venv\Scripts\python.exe -m stonescan.ingest --slabs                    # crawl all suppliers (+ slab galleries)
.\.venv\Scripts\python.exe -m stonescan.ingest --only host1,host2 --slabs # crawl specific suppliers
.\.venv\Scripts\python.exe -m stonescan.discover                          # find more public catalogs
.\.venv\Scripts\python.exe -m stonescan.geocode                           # resolve locations for the map (--recheck to redo)
.\build_exe.ps1                                                           # build the standalone Windows app
```

The standalone app is a native desktop window (pywebview/WebView2) — no console,
no browser. Ships with a snapshot DB + bundled Chromium. See README.md for full
build/packaging details.

## Layout

| Path | Purpose |
|------|---------|
| `stonescan/crawler.py` | Playwright crawler — per supplier: creds/contacts, getItemGallery, slab pre-fetch |
| `stonescan/normalize.py` | Category classifier, cross-supplier `material_key`, color/thickness cleanup |
| `stonescan/smartsearch.py` | Natural-language query parser ("blue marble slabs" → filters) |
| `stonescan/db.py` | SQLite schema + all query/storage helpers (materials, slabs, history, watchlist) |
| `stonescan/ingest.py` | Orchestrator: crawl → normalize → store → history snapshot |
| `stonescan/slabs.py` | On-demand per-slab gallery (live browser fetch, cached) |
| `stonescan/discover.py` | Passive-DNS + search discovery of public catalogs → suppliers.json |
| `stonescan/geocode.py` | Offline location → lat/long for the pin map (+ `locations.json` overrides) |
| `stonescan/reclassify.py` | Re-derive type/color/key in place without re-crawling |
| `stonescan/desktop.py` + `main.py` | Frozen-app launcher (native window, `--refresh` mode) |
| `stonescan/web/app.py` + `templates/` | FastAPI UI: search (table + showroom grid), item detail, canonical material page, What's New, Locations, Sourcing lists, Watchlist |
| `suppliers.json` | Editable allow-list of catalogs to crawl |
| `locations.json` | Editable map pins for locations the geocoder can't resolve |
| `stonescan.spec` / `build_exe.ps1` | PyInstaller packaging |
| `refresh.ps1` | Scheduled nightly refresh (installed as Windows task `StoneScannerRefresh`) |

## Gotchas (learned the hard way — see user memory for depth)

- Catalogs are **Angular SPAs behind Cloudflare**; plain HTTP gets 403. Must use a
  real browser (Playwright). The `<token>.stoneprofits.com` **API** works headless
  with the page's `SPSWebToken` (which **rotates per page-load** — can't be stored).
- Use **`getItemGallery`** (item-level, has thumbnails + live qty), not
  `getInventoryGallery`. Needs the **full param set** or some tenants 500.
- **Image base** comes from `getSettings.FilePath` (often `production<token>-sps-files`,
  so never guess from the token). Slab galleries via `getItemInventory` +
  `/InventoryDetail/<ItemID>` deep-links to a product.
- **`item_id` is unique per-supplier, NOT globally** — group/join by name, not item_id.
- Search results **group by product** (supplier+name+thickness+finish+form) because
  `SearchbyItemIdentifiers=on` returns one row per slab.
- Query params that can arrive empty from forms (e.g. `min_length`) must tolerate `""`.
- A **POSTed HTML form sends its fields in the body**, so those routes must declare
  `Form(...)` params (needs `python-multipart`) — a plain `q: str = ""` reads the
  *query string* and silently receives nothing. This is what broke ★ Save search.
- `db.connect()` **does not create tables** (only `init_db()` runs the schema), so the
  web app calls `init_db()` at startup for user tables (watchlist, lists).
- Anything the user owns must key off `(supplier_id, item_id)` — `materials.id` is
  reassigned every crawl. Sourcing lists also snapshot name/photo so an item that
  leaves a catalog still renders (flagged "gone").

- **Locations are not addresses.** `slabs.location` is free text: ~56 of 99 are real
  cities, the rest are internal yard names (`KLZ`, `HG-NJ`) or towns below the city
  dataset's ~15k-population floor. `geocode.py` resolves what it can **offline** and
  leaves the rest unmapped — deliberately. Don't "improve" recall with the dataset's
  `alternatenames`: that pins *Arca - Warehouse* to Arsk, **Russia**. A bare city name
  (`Columbia`, `Charleston`) is flagged `ambiguous` and shown as approximate, since it
  exists in several states.

## Data notes

- Latest crawl: ~68 suppliers, ~88k materials, ~84k slabs, ~20k unique.
- `stonescan.db` is git-ignored (large/regenerable). Re-crawl to rebuild it.
- Material row IDs are reassigned every crawl (replace-insert) — don't hardcode them.
