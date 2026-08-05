# Stone Scanner — project guide

Cross-supplier stone material search. Aggregates the **public** online catalogs of
stone suppliers into one categorized, searchable database so you can compare
materials across suppliers. Most are on the Stone Profits platform
(`*.stoneprofitsweb.com`); other platforms are supported via providers (below).

**Scope constraint (important):** public catalogs only — the same pages customers
browse. No logins, no private data. Rate-limited, identifiable crawler.
**We honor robots.txt** — enforced in code (`robots.py`), not by hand: every fetch
is checked against its own origin's rules before it goes out. **A removal request is
honored via `denylist.json`** — deleting a host from `suppliers.json` is *not* enough,
because discovery re-adds anything it finds that isn't listed. Aria Stone Gallery is
denylisted for exactly this reason: it disallows all non-search crawlers on both its
storefront *and* (uniquely among SPS tenants) its catalog host. Getting Aria is a
commercial ask, not a code change.

## Run it

```powershell
# dev: activate venv first (.\.venv\Scripts\Activate.ps1) or call the venv python directly
.\.venv\Scripts\python.exe -m uvicorn stonescan.web.app:app --port 8000   # search UI at 127.0.0.1:8000
.\.venv\Scripts\python.exe -m stonescan.ingest --slabs                    # crawl all (+ slab galleries; add --slab-cap N to bound the deep pre-fetch — nightly uses 40)
.\.venv\Scripts\python.exe -m stonescan.ingest --only host1,host2 --slabs # crawl specific suppliers
.\.venv\Scripts\python.exe -m stonescan.discover                          # find more public catalogs
.\.venv\Scripts\python.exe -m stonescan.geocode                           # resolve locations for the map (--recheck to redo)
.\.venv\Scripts\python.exe -m stonescan.dedupe                            # report merge candidates (--auto-conflicts to bulk-merge landslides)
.\.venv\Scripts\python.exe -m stonescan.imagesearch --download-model     # fetch the CLIP model (~81MB, git-ignored), then:
.\.venv\Scripts\python.exe -m stonescan.imagesearch --index              # embed catalog images for search-by-photo (--limit N to bound)
.\.venv\Scripts\python.exe -m stonescan.denylist add <host> --reason "..."  # honor a removal request: deny future crawls AND erase collected data (--keep-data to skip the erase)
.\install-refresh-task.ps1                                                # install the nightly refresh as a Windows task (run once, elevated)
.\.venv\Scripts\python.exe -m stonescan.denylist list                     # what we've been asked not to crawl (`check <host>` to test one)
.\.venv\Scripts\python.exe -m stonescan.reference stats                   # stone-reference coverage (`list --gaps`, `lookup <name>`)
.\.venv\Scripts\python.exe -m stonescan.reference import <file.json>      # merge researched entries (unsourced facts are stripped)
.\.venv\Scripts\python.exe -m unittest tests.test_quality tests.test_robots  # merge/quality/discovery + robots/denylist tests
.\build_exe.ps1                                                           # build the standalone Windows app
```

The standalone app is a native desktop window (pywebview/WebView2) — no console,
no browser. Ships with a snapshot DB + bundled Chromium. See README.md for full
build/packaging details.

## Layout

| Path | Purpose |
|------|---------|
| `stonescan/crawler.py` | Playwright crawler — per supplier: creds/contacts, getItemGallery, slab pre-fetch |
| `stonescan/robots.py` | robots.txt enforcement (RFC 9309). `PoliteClient` is an `httpx.AsyncClient` that refuses disallowed URLs; `client_for(entry)` is what providers use. Gate lives at the HTTP layer, not per-supplier, because the entry `host` often isn't the origin fetched |
| `stonescan/denylist.py` | Durable removal requests (`denylist.json` + CLI). Checked by discovery, by ingest, and by the fingerprint probes |
| `stonescan/providers/` | Non-StoneProfits adapters (`umi`, `slabware`, `stonetrash`, `slabcloud`, `unbuilt`, `genericfeed`); all plain HTTP. `base.material_row()` is mandatory — it's what keeps `material_key` identical across platforms |
| `stonescan/normalize.py` | Category classifier, cross-supplier `material_key`, color/thickness cleanup |
| `stonescan/smartsearch.py` | Natural-language query parser ("blue marble slabs" → filters) |
| `stonescan/db.py` | SQLite schema + all query/storage helpers (materials, slabs, history, watchlist) |
| `stonescan/ingest.py` | Orchestrator: crawl → normalize → store → history snapshot |
| `stonescan/slabs.py` | On-demand per-slab gallery (live browser fetch, cached 10min). Item pages paint the nightly cache instantly, then background-refresh to a **live** read via `/api/slabs?live=1` and stamp "live as of …", so the qty you see on open is current even between crawls. The nightly only pre-caches each supplier's top-`--slab-cap` items (default 40) — enough to seed the map's yard locations; everything else is fetched live on open |
| `stonescan/imagesearch.py` | Search-by-photo: CLIP ViT-B/32 vision encoder (ONNX, CPU, no torch). Embeds catalog images into `image_vectors` (keyed by image_url); `search()` cosine-ranks against an uploaded photo and carries per-material agreement counts; `identify()` turns those into a named verdict + confidence. Model at `stonescan/models/clip/clip_vision.onnx` (git-ignored, `--download-model`). |
| `stonescan/reference.py` | What a stone *is* — origin, quarries, market price — none of which exists in the catalog (see Gotchas). Cited facts in `stone_reference.json`; `lookup_live()` fills gaps from the Wikipedia API on demand. Unsourced facts are stripped on import, so the UI says "not established" rather than inventing one. |
| `stonescan/discover.py` | Discovery of public catalogs → suppliers.json. (1) passive-DNS/CT sweep of Stone Profits **and** SlabWare wildcard subdomains; (2) `discover_slabcloud()` resolves SlabCloud tenants from `slabcloud.com/clients` (reads each tenant's verbatim API slug from `company:"…"`, incl. the `_h_` prefix); (3) distributor **vanity / white-label** Stone Profits catalogs on the distributor's own domain (slabs.nsrstone.com, inventory.acegraniteusa.com, outlet.ckfco.com — drop-in on the default provider, any subdomain prefix, invisible to the subdomain sweep). `discover_sps_embeds()` finds them via urlscan (pages that load a Stone Profits resource → fingerprint-verify); `probe_sps_vanity()` fingerprints `<prefix>.<apex>` over a curated apex list. UMI/StoneTrash single sites stay hand-seeded |
| `stonescan/geocode.py` | Offline location → lat/long for the pin map (+ `locations.json` overrides) |
| `stonescan/reclassify.py` | Re-derive type/color/key in place without re-crawling (also re-applies merges) |
| `stonescan/dedupe.py` | Data-quality curation: type-conflict + spelling merge candidates, `apply_aliases` fold |
| `stonescan/desktop.py` + `main.py` | Frozen-app launcher (native window, `--refresh` mode) |
| `stonescan/web/app.py` + `templates/` | FastAPI UI: search (table + showroom grid), **By Photo** (CLIP visual similarity), item detail, canonical material page, **Compare** (side-by-side canonical materials via a persistent 4-slot tray + print board), What's New, Locations, Sourcing lists, Watchlist, Health, Discovery (candidate triage), Quality (merge review + type audit) |
| `suppliers.json` | Editable allow-list of catalogs to crawl (+ optional reviewed `robots_override`) |
| `denylist.json` | Hosts that must never be crawled or re-discovered. Bundled into the exe and **merged** (not seeded-once) on launch, so a removal shipped in a new build reaches existing installs |
| `stone_reference.json` | Researched stone facts with per-fact source URLs + confidence. Bundled and merged on launch like the denylist; curated entries beat live-looked-up ones on a collision |
| `locations.json` | Editable map pins for locations the geocoder can't resolve |
| `stonescan.spec` / `build_exe.ps1` / `build_mac.sh` | PyInstaller packaging. Spec is platform-aware (WebView2/.NET deps gated to Windows, Cocoa/PyObjC to macOS); `build_exe.ps1` (Windows) and `build_mac.sh` (macOS) each build onedir + copy that OS's Chromium. **No cross-compile** — build each OS on that OS. |
| `refresh.ps1` + `install-refresh-task.ps1` | Nightly refresh crawl, and the idempotent elevated installer that registers it as the `StoneScannerRefresh` Windows task. **Not auto-installed** — run `install-refresh-task.ps1` once; without it nothing refreshes and data goes stale. Triggers at 03:00/05:00/07:00: each waits `-WaitMinutes` for the project drive, and only the **last** one falls back to crawling into the local copy at `%ProgramData%\StoneScanner` (kept in step by `build_exe.ps1`) — earlier ones defer, because D: is usually back by mid-morning |

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
- **robots.txt must be asked through the same door the data comes through.** An
  outside `httpx` GET of `<tenant>.stoneprofitsweb.com/robots.txt` gets a Cloudflare
  **403**, which RFC 9309 reads as "no restrictions" — so a naive check would have
  rubber-stamped ~95 of 118 suppliers while looking like it worked. The crawler reads
  robots.txt *inside the cleared Playwright page* instead. (What it finds: the SPS
  platform serves its Angular shell there, i.e. publishes no robots.txt at all — but
  that is now verified per host, not assumed.) Same principle for UMI: `umistone.com`
  publishes real rules that only its **legacy-cipher** context can reach, so
  `PoliteClient` fetches robots.txt with the provider's own client.
- **A 200 that is HTML is not a robots.txt.** SPA hosts answer `/robots.txt` with
  index.html; parsing that yields "zero rules, allowed" — right answer, wrong reason,
  and one bad marketing line away from inventing a rule. `looks_like_html()` treats it
  as no-file.
- **Check the URL fetched, not the URL requested.** The gate is an httpx *request
  event hook*, because `follow_redirects=True` makes httpx follow 30x internally — a
  wrapper around `.get()` never sees the final URL, and a redirect into a disallowed
  path (or onto another origin) would sail through.
- **A robots block is a decision, not a failure.** Blocked hosts get a `last_error`
  prefixed `robots-blocked:` and are excluded from the retry pass — otherwise we'd
  re-ask, every night, a supplier who already said no (same shape as the "errors that
  never cleared" bug).
- **A publisher's robots.txt can contradict their own server.** unbuilt.co writes
  `Allow: /api/listings/` to carve that endpoint out of `Disallow: /api/`, then
  308-redirects it to the slash-less `/api/listings`, which its own Allow no longer
  matches. Strict per-hop evaluation therefore blocks an endpoint they plainly opened.
  Handled by a **reviewed `robots_override`** in suppliers.json — per-host,
  per-path-prefix, and invalid without a written `reason` (`Override.from_entry`
  raises), so exceptions are auditable rather than silent. It can rescue a `BLOCKED`
  but never an `UNREACHABLE`: "we couldn't ask" is not something a human pre-approved.
- **`materials.origin` is not geological origin, and `price_range` is not money.**
  Both look usable and neither is. `origin` has 44 distinct values of which exactly
  three are countries — the other 41 are US warehouse cities ("Anaheim, CA"), i.e. a
  mislabeled location field, and only 1.1% of rows are populated. `price_range` is
  52% populated but 76% of it is supplier tier codes ("Group 5", "Level 2",
  "Caesarstone B - Standard", bare `$`/`$$$` bands). Only two shapes are real money
  and they mean different things: `$11.9/sf` (per sq ft — StoneTrash only, ~4.7k rows)
  and `$570` (a whole-slab total — Unbuilt, Marble Systems). `_observed_prices()`
  keeps them apart and drops everything else. **This is why the photo-ID reference
  data has to come from outside the catalog** — don't "just join" origin or average
  price_range.
- **The photo-ID consensus must be measured before the dedupe.** `search()` returns
  one row per `material_key` by construction, so counting how many top results share
  a name always yields 1 — the first cut of `identify()` reported "uncertain" for a
  100% self-match. Agreement (`image_matches`, `distinct_suppliers`) is therefore
  tallied over the raw image ranking inside `search()` and carried forward. Distinct
  suppliers matter separately from image count: fourteen photos from one supplier is
  one opinion, not fourteen.
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
- **Every refresh snapshots the DB first** to `stonescan.db.bak` (single rolling backup,
  via `db.backup_database` at the top of `ingest.run_all` — a WAL checkpoint + atomic file
  copy, deliberately NOT the online backup API / `VACUUM INTO`, both of which ran for
  minutes on the ~300 MB catalog) and appends its start/outcome (or error+traceback) to
  `refresh-history.log` beside the DB (`data/` in the packaged app). Restore = swap the
  `.bak` in with the app closed. A backup failure warns to the log and proceeds — it never
  blocks the crawl (staleness is the worse cost). Separate from `refresh.ps1`'s own stdout log.
