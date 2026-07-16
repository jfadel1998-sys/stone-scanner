"""On-demand fetching of an item's full slab gallery.

The catalog's item-detail view lists every physical slab (with its own photo) via
`getItemInventory`. That call needs a fresh per-session token and Cloudflare
clearance, so it can't be replayed with plain HTTP — we drive a real (headless)
browser to the supplier's `/InventoryDetail/<ItemID>` route and capture the call
the app makes itself. Results are cached briefly so repeat views are instant
while staying live.

A single headless browser is launched lazily and reused for the app's lifetime.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

from playwright.async_api import async_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StoneScanner/0.1 "
    "(+public-catalog viewer; contact: jason@traxtone.com)"
)

_pw = None
_browser = None
_launch_lock = asyncio.Lock()
_page_sem = asyncio.Semaphore(2)          # cap concurrent detail loads
_cache: dict[tuple[str, str], tuple[float, list]] = {}
_CACHE_TTL = 600.0                         # seconds; on-demand but avoids re-loads


async def _browser_instance():
    global _pw, _browser
    if _browser is None or not _browser.is_connected():
        async with _launch_lock:
            if _browser is None or not _browser.is_connected():
                if _pw is None:
                    _pw = await async_playwright().start()
                _browser = await _pw.chromium.launch(headless=True)
    return _browser


async def fetch_slabs(
    host: str, item_id: str, image_base: str, *, timeout_ms: int = 35000
) -> list[dict]:
    """Return the individual slabs (photo + details) for one item at one supplier."""
    item_id = str(item_id)
    key = (host, item_id)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    browser = await _browser_instance()
    slabs: list[dict] = []
    async with _page_sem:
        context = await browser.new_context(
            user_agent=USER_AGENT, ignore_https_errors=True,
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()
        try:
            # Bootstrap on the homepage to obtain a fresh token, then fetch the
            # item's slabs in-page (the app doesn't auto-fire its API in headless).
            await page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_function(
                    "typeof SPSWebToken !== 'undefined' && !!SPSWebToken", timeout=timeout_ms
                )
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            result = await page.evaluate(_SLAB_FETCH_JS, str(item_id))
            data = result.get("slabs") or []
            base = image_base or result.get("filePath") or ""
            if base and not base.endswith("/"):
                base += "/"

            for s in data:
                fn = (s.get("FileName") or s.get("Filename") or "").strip()
                slabs.append({
                    "photo": f"{base}{quote(fn)}?width=1400" if (base and fn) else "",
                    "thumb": f"{base}{quote(fn)}?width=500" if (base and fn) else "",
                    "slab_no": (s.get("IDOne") or s.get("SlabNo") or s.get("CustomID") or "").strip(),
                    "location": (s.get("Location") or "").strip(),
                    "length": s.get("AverageLength"),
                    "width": s.get("AverageWidth"),
                    "qty": s.get("AvailableQty"),
                    "uom": (s.get("UOM") or "").strip(),
                    "barcode": (s.get("Barcode") or "").strip(),
                })
            # Photos first, then any photoless slabs.
            slabs.sort(key=lambda x: (x["photo"] == "",))
        except Exception:
            pass  # degrade gracefully; the UI links out to the live catalog
        finally:
            await context.close()

    _cache[key] = (now, slabs)
    return slabs


# Runs in the page: fetch the item's slab list with the parameters the catalog uses.
_SLAB_FETCH_JS = """async (iid) => {
    let token, web, sid;
    try { token = eval('Token'); web = eval('SPSWebToken'); sid = eval('WebconnectSettingID'); } catch (e) {}
    const base = `https://${token}.stoneprofits.com/api/fetchdataAngularProductionToyota.ashx`;
    const H = { 'Content-Type': 'application/json', 'Authorization': web };
    const out = { slabs: [], filePath: '' };
    try {
        const sj = await (await fetch(`${base}?act=getSettings&WebconnectSettingID=${sid}&q=` + Date.now(), { headers: H })).json();
        const cfg = Array.isArray(sj) ? (sj[0] || {}) : (sj || {});
        out.filePath = cfg.SecureFilePath || cfg.FilePath || '';
    } catch (e) {}
    try {
        const u = `${base}?act=getItemInventory&WebconnectSettingID=${sid}&id=${iid}`
            + `&InventoryGroupBy=IDOne_&TrimmedUserID=&OnHold=null&OnSO=null&Intransit=null`
            + `&SelctdLocation=null&ShowLocationinGallery=on&LotPicturesRestrictToSIPL=True`
            + `&DetailLocation=null&q=` + Date.now();
        const j = await (await fetch(u, { headers: H })).json();
        if (Array.isArray(j)) out.slabs = j;
    } catch (e) { out.err = e.message; }
    return out;
}"""


async def shutdown() -> None:
    global _pw, _browser
    try:
        if _browser is not None:
            await _browser.close()
    finally:
        _browser = None
        if _pw is not None:
            await _pw.stop()
            _pw = None
