# Stone Scanner — project guide

Cross-supplier stone material search. Aggregates the **public** online catalogs of
stone suppliers into one categorized, searchable database so you can compare
materials across suppliers. Most are on the Stone Profits platform
(`*.stoneprofitsweb.com`); other platforms are supported via providers (below).

**Scope constraint (important):** public catalogs only — the same pages customers
browse. No logins, no private data. Rate-limited, identifiable crawler.
**We honor robots.txt** — it's what makes that claim true across ~80 suppliers.
Aria Stone Gallery is deliberately excluded: it disallows all non-search crawlers
on both its storefront *and* (uniquely among SPS tenants) its catalog host. Getting
Aria is a commercial ask, not a code change.

## Run it

```powershell
# dev: activate venv first (.\.venv\Scripts\Activate.ps1) or call the venv python directly
.\.venv\Scripts\python.exe -m uvicorn stonescan.web.app:app --port 8000   # search UI at 127.0.0.1:8000
.\.venv\Scripts\python.exe -m stonescan.ingest --slabs                    # crawl all suppliers (+ slab galleries)
.\.venv\Scripts\python.exe -m stonescan.ingest --only host1,host2 --slabs # crawl specific suppliers
.\.venv\Scripts\python.exe -m stonescan.discover                          # find more public catalogs
.\.venv\Scripts\python.exe -m stonescan.geocode                           # resolve locations for the map (--recheck to redo)
.\.venv\Scripts\python.exe -m stonescan.dedupe                            # report merge candidates (--auto-conflicts to bulk-merge landslides)
.\.venv\Scripts\python.exe -m unittest tests.test_quality                 # unit tests for the merge/quality/discovery logic
.\build_exe.ps1                                                           # build the standalone Windows app
```

The standalone app is a native desktop window (pywebview/WebView2) — no console,
no browser. Ships with a snapshot DB + bundled Chromium. See README.md for full
build/packaging details.

## Layout

| Path | Purpose |
|------|---------|
| `stonescan/crawler.py` | Playwright crawler — per supplier: creds/contacts, getItemGallery, slab pre-fetch |
| `stonescan/providers/` | Non-StoneProfits adapters (`umi`, `slabware`, `stonetrash`, `slabcloud`, `unbuilt`, `genericfeed`); all plain HTTP. `base.material_row()` is mandatory — it's what keeps `material_key` identical across platforms |
| `stonescan/normalize.py` | Category classifier, cross-supplier `material_key`, color/thickness cleanup |
| `stonescan/smartsearch.py` | Natural-language query parser ("blue marble slabs" → filters) |
| `stonescan/db.py` | SQLite schema + all query/storage helpers (materials, slabs, history, watchlist) |
| `stonescan/ingest.py` | Orchestrator: crawl → normalize → store → history snapshot |
| `stonescan/slabs.py` | On-demand per-slab gallery (live browser fetch, cached) |
| `stonescan/discover.py` | Discovery of public catalogs → suppliers.json. (1) passive-DNS/CT sweep of Stone Profits **and** SlabWare wildcard subdomains; (2) `discover_slabcloud()` resolves SlabCloud tenants from `slabcloud.com/clients` (reads each tenant's verbatim API slug from `company:"…"`, incl. the `_h_` prefix); (3) distributor **vanity / white-label** Stone Profits catalogs on the distributor's own domain (slabs.nsrstone.com, inventory.acegraniteusa.com, outlet.ckfco.com — drop-in on the default provider, any subdomain prefix, invisible to the subdomain sweep). `discover_sps_embeds()` finds them via urlscan (pages that load a Stone Profits resource → fingerprint-verify); `probe_sps_vanity()` fingerprints `<prefix>.<apex>` over a curated apex list. UMI/StoneTrash single sites stay hand-seeded |
| `stonescan/geocode.py` | Offline location → lat/long for the pin map (+ `locations.json` overrides) |
| `stonescan/reclassify.py` | Re-derive type/color/key in place without re-crawling (also re-applies merges) |
| `stonescan/dedupe.py` | Data-quality curation: type-conflict + spelling merge candidates, `apply_aliases` fold |
| `stonescan/desktop.py` + `main.py` | Frozen-app launcher (native window, `--refresh` mode) |
| `stonescan/web/app.py` + `templates/` | FastAPI UI: search (table + showroom grid), item detail, canonical material page, What's New, Locations, Sourcing lists, Watchlist, Health, Discovery (candidate triage), Quality (merge review + type audit) |
| `suppliers.json` | Editable allow-list of catalogs to crawl |
| `locations.json` | Editable map pins for locations the geocoder can't resolve |
| `stonescan.spec` / `build_exe.ps1` / `build_mac.sh` | PyInstaller packaging. Spec is platform-aware (WebView2/.NET deps gated to Windows, Cocoa/PyObjC to macOS); `build_exe.ps1` (Windows) and `build_mac.sh` (macOS) each build onedir + copy that OS's Chromium. **No cross-compile** — build each OS on that OS. |
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
- **Merges (Quality page) are keyed on the computed `material_key`, not a row id** —
  a crawl/reclassify recomputes `material_key` from scratch and would silently undo
  every merge, so `db.apply_aliases()` must run **last** in `ingest.run_all` and at the
  end of `reclassify` to re-fold them. Same-name-different-type ("Taj Mahal" as
  quartzite vs granite) is the biggest source of split materials; `dedupe.py` surfaces
  those as the primary merge queue. Rejections ("not the same") are remembered by
  signature so a cluster isn't re-proposed.
- Anything the user owns must key off `(supplier_id, item_id)` — `materials.id` is
  reassigned every crawl. Sourcing lists also snapshot name/photo so an item that
  leaves a catalog still renders (flagged "gone").

- **Other platforms** (`suppliers.json` → `"provider": "..."`; omit = Stone Profits):
  each has one trap worth remembering. **UMI**: its API only offers legacy RSA ciphers
  so OpenSSL 3 refuses the handshake (curl works — schannel is laxer); its "branches"
  are regional windows onto ONE pool (connecticut/boston/brooklyn return identical
  sets), so union by item and trust the slab's own `Branch`. **SlabWare**: browser UA
  required (Cloudflare), `Bundles` is JSON-inside-JSON, `DetalheBundleNovo` needs the
  full param set, prices may be the literal `"CALL"`. **StoneTrash**: `buildId` changes
  every deploy (read it from `__NEXT_DATA__`); type comes from `materials_Tags`, since
  `taxonomy` can be just "Tile". **SlabCloud**: list returns one row PER SLAB with the
  count repeated (group by name or slabs inflate); names carry a leading TAB that must
  be sent back verbatim; the API slug is a tenant's `company:"…"` value **incl. any
  `_h_` prefix** (dropping it returns a smaller dataset). **Unbuilt** (Cosentino outlet):
  general surplus marketplace, so query `/api/listings/?q=<brand>` per Cosentino line
  (dekton/silestone/sensa/scalea) and keep only `category_path` containing "Slab";
  `/api/listings/` is the one robots-allowed `/api/` path. **genericfeed**: the long-tail
  provider — robots.txt → sitemap(s) → product pages → schema.org Product JSON-LD, one
  page fetch per product (bounded by `max_products`), every URL checked against robots
  `Disallow` before fetch. Product-level only (no live slab qty).
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
