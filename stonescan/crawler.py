"""Politely harvest a public Stone Profits catalog with a real browser.

Each catalog is an Angular SPA behind Cloudflare, backed by a shared JSON API
(`.../fetchdata*.ashx`). A plain HTTP client is blocked (403); a real browser
renders fine. We therefore:

  1. open the public homepage in headless Chromium (inherits Cloudflare clearance),
  2. capture the inventory + taxonomy JSON the page itself requests,
  3. fall back to replaying the API call in-page if capture missed.

We only touch the public, customer-facing catalog endpoints. A per-request
delay keeps the load light and identifiable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import async_playwright, Browser, TimeoutError as PWTimeout

from . import robots as rb

# Built from robots.AGENT_TOKEN so the name a supplier would write in their
# robots.txt to address us is the same name we actually send.
USER_AGENT = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {rb.AGENT_TOKEN}/0.1 "
    "(+public-catalog indexer; contact: jason@traxtone.com)"
)
INV_MATCH = ("getinventorygallery", "getitemgallery")
TAX_MATCH = "getallsearchdetails"
DEFAULT_ASHX = "fetchdataAngularProductionToyota.ashx"


# Read robots.txt from *inside* the cleared page rather than with httpx. These hosts
# sit behind Cloudflare, which answers an outside GET of /robots.txt with a challenge
# page — a 4xx that RFC 9309 reads as "no restrictions". Asking through the same door
# the catalog comes through is the only way the answer means anything. (In practice
# the platform serves its SPA shell there, i.e. publishes no robots.txt at all — but
# that is a fact we now verify per host instead of assume.)
_ROBOTS_JS = """async () => {
    try {
        const r = await fetch('/robots.txt', { cache: 'no-store' });
        return { status: r.status, body: await r.text() };
    } catch (e) { return { status: 0, body: '', error: String(e) }; }
}"""


@dataclass
class CrawlResult:
    host: str
    ok: bool = False
    blocked: bool = False  # robots.txt disallows us; not an error, a decision
    company: str = ""
    products: str = ""
    token: str = ""
    phone: str = ""
    email: str = ""
    image_base: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    taxonomy: list[dict[str, Any]] = field(default_factory=list)
    slabs: dict[str, list] = field(default_factory=dict)  # ItemID -> [slab records]
    error: str = ""


async def _robots_decision(page, host: str):
    """Fetch and evaluate this host's robots.txt from inside the loaded page."""
    try:
        res = await page.evaluate(_ROBOTS_JS)
    except Exception as e:  # noqa: BLE001
        return rb.RobotsPolicy.unreachable(type(e).__name__).allows("/")
    status, body = res.get("status") or 0, res.get("body") or ""
    if not status:
        return rb.RobotsPolicy.unreachable(res.get("error", "fetch failed")).allows("/")
    return rb._policy_from_response(status, body, rb.AGENT_TOKEN).allows("/")


async def _read_creds(page) -> dict[str, Any]:
    return await page.evaluate(
        """async () => {
            const g = k => { try { return eval(k); } catch(e) { return null; } };
            // Contact info lives in the footer, which often loads a few seconds
            // after the page — poll for it before giving up.
            const PH = /(?:\\+?1[\\s.-]?)?\\(?\\d{3}\\)?[\\s.-]?\\d{3}[\\s.-]?\\d{4}/;
            const EM = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/;
            const fromDom = () => {
                let phone = '', email = '';
                const tel = document.querySelector('a[href^="tel:"]');
                if (tel) phone = tel.getAttribute('href').replace(/^tel:/i, '').trim();
                const mail = document.querySelector('a[href^="mailto:"]');
                if (mail) email = mail.getAttribute('href').replace(/^mailto:/i, '').split('?')[0].trim();
                const text = document.body ? document.body.innerText : '';
                if (!phone) { const m = text.match(PH); if (m) phone = m[0].trim(); }
                if (!email) { const m = text.match(EM); if (m) email = m[0].trim(); }
                return { phone, email };
            };
            let c = fromDom();
            // Primary + reliable in headless: the static footer/header custom files
            // hold the contact info even when the footer hasn't rendered.
            if (!c.phone || !c.email) {
                // The custom footer/header files live under the Stone Profits WEBNAME.
                // On a normal <webname>.stoneprofitsweb.com host that equals the hostname
                // label; on a distributor vanity host (inventory.<x>.com) it does NOT, so
                // also recover the webname from a platform script src.
                const subs = [location.hostname.split('.')[0]];
                for (const s of document.scripts) {
                    const m = (s.src || '').match(/stoneprofitsweb\\.com\\/([^\\/]+)\\//i);
                    if (m && subs.indexOf(m[1]) < 0) subs.push(m[1]);
                }
                const paths = [];
                for (const sub of subs) paths.push('/custom/' + sub + '/CustomTop.html',
                                                   '/custom/' + sub + '/CustomBottom.html');
                for (const path of paths) {
                    try {
                        const r = await fetch(path);
                        if (!r.ok) continue;
                        const html = await r.text();
                        if (!c.phone) { const m = html.match(/tel:([+\\d().\\s-]{7,})/i) || html.match(PH); if (m) c.phone = (m[1] || m[0]).trim(); }
                        if (!c.email) { const m = html.match(/mailto:([^"'?<>\\s]+)/i) || html.match(EM); if (m) c.email = (m[1] || m[0]).trim(); }
                        if (c.phone && c.email) break;
                    } catch (e) {}
                }
            }
            return {
                Token: g('Token'),
                SPSWebToken: g('SPSWebToken'),
                WebconnectSettingID: g('WebconnectSettingID'),
                Company: g('Company'),
                Products: g('Products'),
                Phone: c.phone,
                Email: c.email,
            };
        }"""
    )


async def _fetch_catalog(page, token: str, web_token: str, setting_id: str) -> dict[str, Any]:
    """Fetch the item-level catalog from inside the page (inherits origin + CORS).

    `getItemGallery` returns one row per material with the thumbnail filename and
    live availability. We also derive the S3 image base from a rendered thumbnail,
    falling back to the standard per-tenant bucket pattern.
    """
    return await page.evaluate(
        """async ({token, webToken, settingId, ashx}) => {
            const base = `https://${token}.stoneprofits.com/api/${ashx}`;
            const H = { 'Content-Type': 'application/json', 'Authorization': webToken };
            const out = { items: [], taxonomy: [], imageBase: '', email: '', err: '' };
            try {
                const tu = `${base}?act=getAllSearchDetails&WebconnectSettingID=${settingId}&q=${Date.now()}`;
                out.taxonomy = await (await fetch(tu, { headers: H })).json();
            } catch (e) { out.err += 'tax:' + e.message + ';'; }
            try {
                // Full param set matches what the catalog itself sends; a minimal
                // set makes some tenants throw a SQL error.
                const iu = `${base}?act=getItemGallery&WebconnectSettingID=${settingId}`
                    + `&InventoryGroupBy=IDONE_&SearchbyItemIdentifiers=on&ShowFeatureProductOnTop=null`
                    + `&OnHold=null&OnSO=null&Intransit=null&showNotInStock=null&SearchbyFinish=on`
                    + `&SearchbySKU=null&Alphabet=&q=${Date.now()}`;
                const r = await fetch(iu, { method: 'POST', headers: H, body: '{}' });
                const j = await r.json();
                out.items = Array.isArray(j) ? j : [];
            } catch (e) { out.err += 'item:' + e.message + ';'; }
            // Fallback: some tenants only serve the granular inventory feed cleanly.
            if (!out.items.length) {
                try {
                    const gu = `${base}?act=getInventoryGallery&WebconnectSettingID=${settingId}`
                        + `&InventoryGroupBy=IDONE_&TrimmedUserID=&OnHold=0&OnSO=0&Intransit=0&q=${Date.now()}`;
                    const r2 = await fetch(gu, { method: 'POST', headers: H, body: '{}' });
                    const j2 = await r2.json();
                    out.items = Array.isArray(j2) ? j2 : [];
                } catch (e) { out.err += 'inv:' + e.message + ';'; }
            }
            // Image base: authoritative source is getSettings.FilePath (the S3 bucket URL).
            // The bucket is often `production<token>-sps-files`, so never guess from the token.
            try {
                const su = `${base}?act=getSettings&WebconnectSettingID=${settingId}&q=${Date.now()}`;
                const sj = await (await fetch(su, { headers: H })).json();
                const cfg = Array.isArray(sj) ? (sj[0] || {}) : (sj || {});
                let fp = cfg.SecureFilePath || cfg.FilePath || '';
                if (fp) { if (!fp.endsWith('/')) fp += '/'; out.imageBase = fp; }
                // A real supplier email is sometimes here (skip the generic platform one).
                const e = (cfg.EmailImagesfromGallery_SendersEmail || '').trim();
                if (e && !/@stoneprofits\\.com$/i.test(e)) out.email = e;
            } catch (e) {}
            // Fallbacks: a rendered thumbnail, the bucket name in markup, then token pattern.
            try {
                if (!out.imageBase) {
                    const rendered = [...document.images].map(im => im.currentSrc || im.src)
                        .find(s => /sps-files/i.test(s));
                    if (rendered) {
                        const noQ = rendered.split('?')[0];
                        out.imageBase = noQ.slice(0, noQ.lastIndexOf('/') + 1);
                    } else {
                        const m = document.documentElement.outerHTML.match(/([a-z0-9]+-sps-files)/i);
                        if (m) out.imageBase = `https://s3.us-east-1.amazonaws.com/${m[1]}/`;
                    }
                }
            } catch (e) {}
            if (!out.imageBase && token) {
                out.imageBase = `https://s3.us-east-1.amazonaws.com/${token}-sps-files/`;
            }
            return out;
        }""",
        {"token": token, "webToken": web_token, "settingId": str(setting_id or "1"), "ashx": DEFAULT_ASHX},
    )


# In-page: fetch getItemInventory for many items with bounded concurrency.
_SLABS_FETCH_JS = """async ({ids}) => {
    let token, web, sid;
    try { token = eval('Token'); web = eval('SPSWebToken'); sid = eval('WebconnectSettingID'); } catch (e) {}
    const base = `https://${token}.stoneprofits.com/api/fetchdataAngularProductionToyota.ashx`;
    const H = { 'Content-Type': 'application/json', 'Authorization': web };
    const out = {};
    const queue = ids.slice();
    async function worker() {
        while (queue.length) {
            const id = queue.shift();
            try {
                const u = `${base}?act=getItemInventory&WebconnectSettingID=${sid}&id=${id}`
                    + `&InventoryGroupBy=IDOne_&TrimmedUserID=&OnHold=null&OnSO=null&Intransit=null`
                    + `&SelctdLocation=null&ShowLocationinGallery=on&LotPicturesRestrictToSIPL=True`
                    + `&DetailLocation=null&q=${Date.now()}${Math.round(id)}`;
                const j = await (await fetch(u, { headers: H })).json();
                if (Array.isArray(j) && j.length) {
                    out[id] = j.map(s => ({
                        FileName: s.FileName || s.Filename || '',
                        IDOne: s.IDOne || s.SlabNo || s.CustomID || '',
                        Location: s.Location || '',
                        AverageLength: s.AverageLength, AverageWidth: s.AverageWidth,
                        AvailableQty: s.AvailableQty, UOM: s.UOM || '', Barcode: s.Barcode || '',
                    }));
                }
            } catch (e) {}
        }
    }
    await Promise.all(Array.from({length: 6}, () => worker()));
    return out;
}"""


async def _fetch_all_slabs(page, item_ids: list[str]) -> dict[str, list]:
    if not item_ids:
        return {}
    try:
        return await page.evaluate(_SLABS_FETCH_JS, {"ids": item_ids})
    except Exception:
        return {}


async def crawl_host(
    browser: Browser, host: str, *, timeout_ms: int = 45000, with_slabs: bool = False,
    robots_cache=None, slab_item_cap: int = 0,
) -> CrawlResult:
    res = CrawlResult(host=host)

    # Two-stage check. First, cheaply, from outside: if a plain GET can already read
    # an explicit opt-out, we stop without touching the site at all.
    cache = robots_cache if robots_cache is not None else rb.RobotsCache()
    pre = await cache.check(f"https://{host}/")
    if pre.reason == rb.BLOCKED:
        res.blocked = True
        res.error = rb.block_error(f"{rb.AGENT_TOKEN} disallowed by {pre.rule}")
        return res

    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        ignore_https_errors=True,  # some tenants use custom domains with mismatched certs
    )
    page = await context.new_page()

    # Capture the page's OWN data calls, split by kind. getItemGallery carries the
    # thumbnail filename; getInventoryGallery is the granular fallback.
    captured: dict[str, list] = {"item": [], "inv": [], "tax": []}

    async def on_response(response):
        url = response.url.lower()
        try:
            if "getitemgallery" in url:
                captured["item"].append(await response.json())
            elif "getinventorygallery" in url:
                captured["inv"].append(await response.json())
            elif TAX_MATCH in url:
                captured["tax"].append(await response.json())
        except Exception:
            pass

    page.on("response", on_response)

    try:
        await page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=timeout_ms)
        # Give the SPA time to authenticate against Cloudflare and fire its data calls.
        try:
            await page.wait_for_function("typeof SPSWebToken !== 'undefined' && !!SPSWebToken", timeout=timeout_ms)
        except PWTimeout:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass

        # Second stage, authoritative: now that Cloudflare has cleared us we can read
        # the real robots.txt. Loading this one public homepage is what any visitor
        # does and is the only way to obtain that clearance; everything that follows —
        # the catalog API, the per-item slab galleries, the bulk of the traffic — is
        # gated on the answer.
        decision = await _robots_decision(page, host)
        if not decision.allowed:
            res.blocked = decision.reason == rb.BLOCKED
            res.error = (rb.block_error(f"{rb.AGENT_TOKEN} disallowed by {decision.rule}")
                         if res.blocked else f"robots.txt {decision}")
            return res

        creds = await _read_creds(page)
        res.company = (creds.get("Company") or "").strip()
        res.products = (creds.get("Products") or "").strip()
        res.token = (creds.get("Token") or "").strip()
        res.phone = (creds.get("Phone") or "").strip()
        res.email = (creds.get("Email") or "").strip()

        items: list = []
        taxonomy: list = []

        # Primary: explicitly fetch the item-level catalog (has thumbnails + live qty).
        if res.token and creds.get("SPSWebToken"):
            fetched = await _fetch_catalog(
                page, res.token, creds["SPSWebToken"], creds.get("WebconnectSettingID")
            )
            items = fetched.get("items") or []
            taxonomy = fetched.get("taxonomy") or []
            res.image_base = fetched.get("imageBase") or ""
            if not res.email and fetched.get("email"):
                res.email = fetched["email"].strip()
            if not items and fetched.get("err"):
                res.error = fetched["err"]

        # If our replay didn't work (some tenants use non-standard grouping params),
        # fall back to the page's OWN captured calls — give slow catalogs a moment.
        if not items:
            for _ in range(12):
                if captured["item"] or captured["inv"]:
                    break
                await page.wait_for_timeout(1000)
            # Prefer item-level (has images) over granular.
            items = _largest_array(captured["item"]) or _largest_array(captured["inv"])
        if not taxonomy:
            taxonomy = _largest_array(captured["tax"])
        if not res.image_base and res.token:
            res.image_base = f"https://s3.us-east-1.amazonaws.com/{res.token}-sps-files/"
        if items:
            res.error = ""  # recovered via fallback

        res.items = items or []
        res.taxonomy = taxonomy or []
        res.ok = bool(res.items)
        if not res.ok and not res.error:
            res.error = "no items returned (empty or non-public catalog)"

        # Optionally pre-fetch in-stock items' slab galleries, bounded by
        # slab_item_cap (0 = all). The deep getItemInventory call is the crawl's
        # single biggest cost, and any un-cached gallery is fetched live on open
        # anyway (see web app /api/slabs -> slabs.fetch_slabs). So we keep only the
        # most-stocked items: enough to seed each supplier's yard locations for the
        # map — a supplier has a handful of yards, well covered by its busiest
        # items — without paying to pre-cache the long tail every night.
        if res.ok and with_slabs:
            in_stock = []
            seen = set()
            for it in res.items:
                iid = str(it.get("ItemID") or "")
                try:
                    slabs_avail = float(it.get("AvailableSlabs") or 0)
                except (TypeError, ValueError):
                    slabs_avail = 0
                if iid and iid not in seen and slabs_avail > 0:
                    seen.add(iid)
                    in_stock.append((slabs_avail, iid))
            in_stock.sort(reverse=True)  # busiest items first
            if slab_item_cap > 0:
                in_stock = in_stock[:slab_item_cap]
            res.slabs = await _fetch_all_slabs(page, [iid for _, iid in in_stock])
    except PWTimeout:
        res.error = "timeout loading catalog"
    except Exception as e:  # noqa: BLE001 - report any crawl failure per-host
        res.error = f"{type(e).__name__}: {e}"
    finally:
        await context.close()
    return res


def _largest_array(candidates: list) -> list:
    best: list = []
    for c in candidates:
        if isinstance(c, list) and len(c) > len(best):
            best = c
    return best


async def crawl_hosts(
    hosts: list[str], *, concurrency: int = 3, delay_s: float = 1.5,
    headless: bool = True, with_slabs: bool = False, slab_item_cap: int = 0,
):
    """Crawl hosts with limited concurrency and a polite per-host delay.

    Yields CrawlResult objects as they complete.
    """
    results: list[CrawlResult] = []
    cache = rb.RobotsCache()  # shared, so each origin is asked once per run
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        sem = asyncio.Semaphore(concurrency)

        async def worker(h: str) -> CrawlResult:
            async with sem:
                r = await crawl_host(browser, h, with_slabs=with_slabs,
                                     robots_cache=cache, slab_item_cap=slab_item_cap)
                await asyncio.sleep(delay_s)  # be gentle
                return r

        tasks = [asyncio.create_task(worker(h)) for h in hosts]
        for fut in asyncio.as_completed(tasks):
            r = await fut
            results.append(r)
            yield r
        await browser.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
