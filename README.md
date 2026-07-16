# Stone Scanner

Aggregate the **public** catalogs of stone suppliers that run on the
[Stone Profits](https://www.stoneprofits.com/) platform (`*.stoneprofitsweb.com`)
into one categorized, searchable material database — so you can compare
materials across suppliers when sourcing.

> **Scope:** this only indexes catalogs that suppliers have deliberately published
> for the public (the same pages Google indexes and customers browse). It never
> logs in, never touches private/authenticated data, and crawls politely
> (rate-limited, identifiable user-agent). Edit `suppliers.json` to control exactly
> which catalogs are indexed.

## How it works

Each public catalog is an Angular app backed by a shared JSON API. Stone Scanner
opens each catalog in a real (headless) browser, captures the catalog's own
inventory + taxonomy JSON, normalizes it (unifying categories, colors,
thicknesses, and matching the same material across suppliers), and stores it in a
local SQLite database. A small web UI searches across everything.

```
suppliers.json → crawler (Playwright) → normalize → SQLite → search UI (FastAPI)
```

## Setup (Windows, PowerShell)

```powershell
cd "C:\Users\Traxtone\Desktop\stone scanner"

# 1. create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. install dependencies
pip install -r requirements.txt

# 3. install the browser Playwright drives
python -m playwright install chromium
```

## Usage

**1) Crawl catalogs into the database**

```powershell
# quick test — first 2 suppliers
python -m stonescan.ingest --limit 2

# full run over suppliers.json
python -m stonescan.ingest

# also search for more public catalogs, then crawl
python -m stonescan.ingest --discover

# if Cloudflare blocks the headless browser, watch it run:
python -m stonescan.ingest --show-browser --concurrency 1
```

Re-running is safe — each supplier's materials are replaced with a fresh pull.

**1b) (Optional) Re-categorize without re-crawling**

If you improve the classifier in `stonescan/normalize.py`, re-derive material
types, colors, and match keys for everything already stored:

```powershell
.\.venv\Scripts\python.exe -m stonescan.reclassify
```

**2) Launch the search UI**

```powershell
uvicorn stonescan.web.app:app --port 8000
```

Open <http://127.0.0.1:8000>. The search box understands plain language —
type things like **"blue marble slabs"**, **"black granite tiles"**, or
**"3cm polished calacatta"** and it extracts the color, material type, thickness,
and form automatically (shown as "understood as" chips). You can still use the
dropdown filters for material type / color / thickness / supplier. Click a **material name or thumbnail** to open its detail
page (large stock photo, full specs, live availability, supplier info, and every
other supplier that carries it), or **compare →** to jump straight to the
cross-supplier comparison.

**Sourcing features:**
- **Location + size filters** — narrow to stock in a given warehouse/city, or to
  slabs at least a given length × width.
- **✨ What's New** — materials suppliers flagged as new, plus "back in stock"
  (detected by comparing nightly crawls).
- **★ Watchlist** — save any search (incl. natural-language ones) and see its live
  match count + how many are newly arrived, each time you open the page.
- **Similar materials** on the detail page — comparable stone across suppliers.
- **Supplier contact** on the detail page — email/phone where published, plus a
  link to their live catalog.

The detail page also shows the **full per-slab gallery** — every individual slab
(photo, slab #, location, size); click any slab for a full-size lightbox view.
These are **pre-fetched and cached during the nightly refresh** (`--slabs`), so
galleries open instantly. If an item wasn't pre-cached yet, the page fetches it
live once (a few seconds) and caches it. So the nightly job does the heavy
lifting; daytime browsing is fast.

## Building the standalone Windows app (.exe)

The app can be packaged into a self-contained folder that runs on any Windows PC
**with no Python and no setup** — it bundles the server, the crawler, Chromium,
and a snapshot of the database.

```powershell
# one-time: install the build tool + Chromium (if not already present)
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m playwright install chromium

# build (produces dist\StoneScanner\ and copies Chromium in)
.\build_exe.ps1
```

This yields **`dist\StoneScanner\`** (~925 MB) containing `StoneScanner.exe`.
Zip that folder (or `dist\StoneScanner.zip`, ~365 MB) and share it. On the target
PC: unzip and run `StoneScanner.exe` — it opens as a **native desktop window**
(no console window, no external browser; the UI is hosted in an embedded
WebView2). A `data\` folder is created next to the exe for the database and
supplier list; the bundled snapshot loads immediately, and the in-app **Refresh**
button (or `StoneScanner.exe --refresh`) updates it live using the bundled
Chromium.

Requires the WebView2 runtime, which is pre-installed on Windows 10/11. (If it's
ever missing, the app falls back to opening the system browser.)

`READ ME FIRST.txt` inside the folder has end-user instructions.

## Project layout

| File | Purpose |
|------|---------|
| `suppliers.json` | Editable allow-list of public catalogs to index |
| `stonescan/crawler.py` | Playwright crawler (captures each catalog's JSON) |
| `stonescan/normalize.py` | Category unification + cross-supplier material matching |
| `stonescan/db.py` | SQLite schema and storage |
| `stonescan/discover.py` | Multi-source discovery of public catalogs (passive DNS + search) |
| `stonescan/reclassify.py` | Re-categorize stored materials after classifier tweaks |
| `stonescan/ingest.py` | Orchestrator / CLI |
| `stonescan/web/` | FastAPI UI: search, item detail, cross-supplier comparison |
| `stonescan/smartsearch.py` | Natural-language query parser (color/type/thickness/form) |
| `stonescan/slabs.py` | On-demand per-slab gallery (live browser + short cache) |
| `stonescan/desktop.py` | Desktop launcher (frozen paths, server + browser, `--refresh`) |
| `main.py` / `stonescan.spec` / `build_exe.ps1` | Packaging into the standalone .exe |
| `refresh.ps1` | Scheduled-refresh script (discover + re-crawl) |
| `stonescan.db` | The database (created on first crawl) |

## Adding suppliers manually

Add an entry to `suppliers.json`:

```json
{ "host": "somecompany.stoneprofitsweb.com", "name": "Some Company" }
```

Custom domains that run on the same platform also work (e.g.
`inventory.tritonstone.com`) — the crawler reads the tenant identity from the page.

## Keeping it fresh

Suppliers' catalogs change constantly as they sell and receive material, so the
app is built to stay current:

- **Thumbnails link to each supplier's live image** (their S3 bucket) — they are
  never downloaded or cached, so a thumbnail always reflects the supplier's
  current photo. Availability counts and "NEW" arrivals come from the live
  item-level feed at crawl time.
- **Re-crawl on a schedule** to refresh availability and pick up new arrivals /
  new suppliers:

  ```powershell
  .\refresh.ps1                       # discover + full re-crawl + slab pre-cache (logs to refresh.log)

  # incremental: only re-crawl suppliers not refreshed in the last 12 hours
  .\.venv\Scripts\python.exe -m stonescan.ingest --stale-hours 12

  # crawl without pre-caching slab galleries (faster; galleries fetch live on first view)
  .\.venv\Scripts\python.exe -m stonescan.ingest
  ```

  `--slabs` (used by `refresh.ps1`) pre-fetches every in-stock item's slab gallery
  so detail pages open instantly. It makes the crawl noticeably longer, which is
  why it's reserved for the scheduled nightly run.

  **A daily refresh is already installed** as the Windows scheduled task
  `StoneScannerRefresh` (runs `refresh.ps1` at 03:00). Manage it with:

  ```powershell
  Get-ScheduledTaskInfo StoneScannerRefresh     # next run time / last result
  Start-ScheduledTask   StoneScannerRefresh     # run a refresh right now
  Disable-ScheduledTask StoneScannerRefresh     # pause automatic refreshes
  Unregister-ScheduledTask StoneScannerRefresh  # remove it
  ```

The UI header shows when the data was last refreshed.

## Notes & limits

- **Discovery** can't enumerate every tenant (the platform uses one wildcard
  certificate), so it relies on passive-DNS + search + your curated list.
- **Thumbnails** are full-resolution (the platform ignores resize hints), so the
  UI lazy-loads them. Items without an uploaded photo show a stone placeholder.
- Prices are only shown if a supplier makes them public (most don't).
