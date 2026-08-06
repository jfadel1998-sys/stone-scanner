"""iBlocky — Italian block and slab platform (iblocky.it).

Nineteen Italian yards (Carrara, Verona, Massa), one Belgian and one Singaporean publish
their stock here, which makes this the project's first real international source: before
it, 2 of 141 suppliers holding materials were outside the US, and both of those arrived
by accident through the Stone Profits subdomain sweep.

WHY THIS WAS ALMOST MISSED, twice over — both traps are structural, not bad luck:

  * **Tenancy is path-based, not subdomain-based.** Every tenant lives at
    `app.iblocky.it/public-blocks/<slug>`, so the CT/passive-DNS sweep that finds
    `<tenant>.stoneprofitsweb.com` and `<tenant>.slabware.com` returns only
    infrastructure hosts here and reads as "this platform has no tenants". The
    directory is an API endpoint (`/v1/public/tenants`), not a DNS namespace.
  * **The inventory endpoint is POST-only.** `GET` on the exact same URL answers
    `{"error":"Route not found","statusCode":404}`. Every other provider in this
    package is GET-only, so a GET-shaped probe looks like proof of absence.

Three origins, three different robots answers, and only the middle one is the trap:

    api.iblocky.it   no robots.txt (404)          -> allowed, and this is where data lives
    app.iblocky.it   "User-agent: * / Disallow:"  -> allowed (the human catalog page)
    iblocky.it       "Disallow: /api/"            -> BLOCKED, and correctly so

The marketing apex disallows `/api/`; the API host is a different origin and says nothing.
RFC 9309 is per-origin, so `robots.py` already returns False for `iblocky.it/api/*` and True
for the other two — verified, not assumed. Nothing here is self-contradictory, so this
needs no `robots_override`; it just needs every fetch to go through `client_for`.

Product-level only. The public surface publishes a slab COUNT per lot, never the
individual slabs — the real UI renders "15 Slabs / Cristallo Alba / 343x203 / 2 CM" with
no drill-down, and every per-slab endpoint shape probed returns 404 or 401. So this
provider writes materials and no slab rows. Do not synthesise slabs to fill the table:
a fabricated slab row would carry a made-up number into the search UI's size filters.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ..robots import client_for
from .base import SupplierData, centimetres_to_inches, material_row

API = "https://api.iblocky.it/api"
APP = "https://app.iblocky.it"
TENANTS_URL = f"{API}/v1/public/tenants"
UA = "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"

# The API caps a page well below this in practice; it is a ceiling, not a promise.
PAGE_SIZE = 100
# Stop runaway paging if `hasNext` ever lies. 520 pages at limit=2 is the largest tenant
# seen at 100/page, so this is ~40x headroom and still bounded.
MAX_PAGES = 400

# `commessaType` -> the product_form we store and the public page that shows it.
_KINDS = {"slab": ("SLAB", "public-blocks"), "block": ("BLOCK", "public-summary")}


# --- petroDesc -----------------------------------------------------------------------
# Half the catalog states its stone type in Italian, and `canonical_type()` speaks English:
# 'Marmo'/'Marmi'/'MARMO' -> "Other", 'Graniti'/'Granito' -> "Other", 'Quarzite' -> "Other".
# That is not cosmetic. `material_key` is (name, type), so an un-translated row can never
# group with the same stone from a US supplier — which is the entire point of the project.
#
# `petroDescTranslations.en` looks like the answer and IS NOT: measured across all 21
# tenants it echoes the raw value verbatim ('Marmo' -> 'Marmo', 'MARMO' -> 'MARMO') and is
# sometimes EMPTY where the raw value isn't ('GRANITES' -> ''). It is a translation table
# nobody filled in. Hence a real map.
_PETRO = {
    "marmo": "Marble", "marmi": "Marble", "marbles": "Marble",
    "granito": "Granite", "graniti": "Granite", "granites": "Granite",
    "quarzite": "Quartzite",
    "onice": "Onyx",
    "travertino": "Travertine", "travertines": "Travertine",
    "arenaria": "Sandstone",
    "calcare": "Limestone",
    "semiprezioso": "Semi-Precious", "materiali semipreziosi": "Semi-Precious",
    "ceramica": "Ceramic",
    "agglomerato": "Agglomerate", "agglomerato marmo": "Agglomerate",
}

# Values that are not a stone type at all. Mapped to "" so `canonical_type()` falls back to
# reading the material NAME, which is a better guess than any of these:
#   'Sicily' is a place, 'PIETRA'/'ALTRE PIETRE' mean "stone"/"other stones", 'N/A' is a null.
_PETRO_JUNK = {"n/a", "na", "sicily", "pietra", "altre pietre", "altro", "other", "-"}


# --- origin --------------------------------------------------------------------------
# This is the field CLAUDE.md records as unavailable from the US catalogs (there,
# `materials.origin` is 1.1% populated and mostly warehouse cities). Here it is 83%
# populated and genuinely geological — but only mostly.
#
# ALLOW-LIST, NOT A CLEANUP, because the same trap is present: 347 rows say 'ELITEST' and
# 290 say 'INNOVA', which are supplier brand codes, not places. A blank-the-junk rule would
# have to enumerate them; an allow-list of countries drops anything it does not recognise,
# so a new brand code appearing next month is silently correct rather than silently wrong.
# The cost is a real country arriving in a spelling not listed here — it lands empty, which
# is the honest answer and matches how the UI already reports "not established".
_ORIGIN = {
    "italy": "Italy", "italia": "Italy",
    "brazil": "Brazil", "brasile": "Brazil",
    "india": "India",
    "greece": "Greece", "grecia": "Greece",
    "angola": "Angola",
    "portugal": "Portugal", "portogallo": "Portugal",
    "turkey": "Turkey", "turchia": "Turkey",
    "norway": "Norway", "norvegia": "Norway",
    "zimbabwe": "Zimbabwe",
    "iran": "Iran",
    "tunisia": "Tunisia",
    "namibia": "Namibia",
    "spain": "Spain", "spagna": "Spain",
    "albania": "Albania",
    "china": "China", "cina": "China",
    "morocco": "Morocco", "marocco": "Morocco",
    "croatia": "Croatia", "croazia": "Croatia",
    "egypt": "Egypt", "egitto": "Egypt",
    "france": "France", "francia": "France",
    "switzerland": "Switzerland", "svizzera": "Switzerland",
    "south africa": "South Africa", "sudafrica": "South Africa",
    "vietnam": "Vietnam",
    "andorra": "Andorra",
}

# "343x203" (slab: L x W) or "300x200x135" (block: L x W x H). Commas appear as decimal
# separators. Only the first two components are stored — `materials` has no height column,
# and a block's height must never be written into avg_width.
_DIM = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)")


def petro_type(row: dict) -> str:
    """The stone type as English `canonical_type()` can read it, or "" to fall back to name."""
    raw = str(row.get("petroDesc") or "").strip()
    low = raw.lower()
    if not low or low in _PETRO_JUNK:
        return ""
    return _PETRO.get(low, raw)


def origin_of(row: dict) -> str:
    """A recognised country name, or "" — never a supplier brand code. See _ORIGIN."""
    return _ORIGIN.get(str(row.get("origin") or "").strip().lower(), "")


def dimensions(stats: dict) -> tuple[float | None, float | None]:
    """(length, width) in INCHES from the platform's centimetre "LxW[xH]" string."""
    m = _DIM.match(str(stats.get("dimensions") or ""))
    if not m:
        return None, None
    return (centimetres_to_inches(m.group(1).replace(",", ".")),
            centimetres_to_inches(m.group(2).replace(",", ".")))


def _slug(entry: dict) -> str:
    """Tenant slug: explicit, else the label from '<slug>.iblocky.it'.

    As with SlabCloud, `<slug>.iblocky.it` is this supplier's identity key in our DB and
    not a host that resolves — the tenant's real public page is APP/public-blocks/<slug>.
    """
    if entry.get("slug"):
        return str(entry["slug"]).strip()
    return (entry.get("host") or "").split(".")[0].strip()


async def crawl(entry: dict, *, with_slabs: bool = False, delay: float = 0.3,
                limit: int = 0, **_kw) -> SupplierData:
    slug = _slug(entry)
    host = entry.get("host") or f"{slug}.iblocky.it"
    out = SupplierData(host=host, company=entry.get("name") or slug.replace("-", " ").title())
    crawled_at = _now()
    seen: set[str] = set()
    try:
        # Gated against api.iblocky.it — the origin actually fetched — not the
        # <slug>.iblocky.it host this entry is filed under, which does not exist.
        async with client_for(entry, headers={"User-Agent": UA, "Accept": "application/json"},
                              follow_redirects=True) as client:
            # Contacts, from the tenant record rather than suppliers.json, so a supplier
            # who changes their phone number is right again after one crawl.
            try:
                t = (await client.get(f"{API}/v1/tenants/{slug}", timeout=45)).json()
                info = t.get("tenant") or {}
                out.company = info.get("name") or out.company
                out.email = (info.get("email") or "").strip()
                out.phone = (info.get("phone") or "").strip()
            except Exception:  # noqa: BLE001 - contacts are a nicety; stock is the job
                pass

            for kind, (form, page_path) in _KINDS.items():
                source_url = f"{APP}/{page_path}/{slug}"
                page = 1
                while page <= MAX_PAGES:
                    if limit and len(out.materials) >= limit:
                        break
                    # POST, not GET. GET on this exact URL is a 404 — see the module docstring.
                    r = await client.post(
                        f"{API}/v2/tenants/{slug}/commesse/all",
                        params={"page": page, "limit": PAGE_SIZE},
                        json={"commessaType": kind}, timeout=60,
                    )
                    r.raise_for_status()
                    body = r.json()
                    rows = [c for c in (body.get("commesse") or []) if isinstance(c, dict)]
                    if not rows:
                        break

                    for row in rows:
                        if limit and len(out.materials) >= limit:
                            break
                        if row.get("isActive") is False:
                            continue
                        name = str(row.get("material") or "").strip()
                        if not name:
                            continue
                        # `id` and not `code`: item_id is the durable half of the
                        # (supplier_id, item_id) key everything the user owns hangs off,
                        # and `code` is a lot number that repeats across slab/block types.
                        item_id = str(row.get("id") or "")
                        if not item_id or item_id in seen:
                            continue
                        seen.add(item_id)

                        stats = row.get("stats") or {}
                        length, width = dimensions(stats)
                        finishes = [f for f in (stats.get("finishes") or []) if f]
                        # thickness is a cm list ([2] = 2cm). No thickness_uom= and no
                        # entry in THICKNESS_INCH_HOSTS: normalize_thickness already
                        # assumes cm when the unit is absent, which is the right answer here.
                        thicks = [t for t in (stats.get("thickness") or []) if t]

                        out.materials.append(material_row(
                            name=name, crawled_at=crawled_at, item_id=item_id,
                            category=petro_type(row),
                            finish=str(finishes[0]) if finishes else "",
                            thickness=str(thicks[0]) if thicks else "",
                            product_form=form,
                            origin=origin_of(row),
                            available_slabs=stats.get("availableSlabs") or 0,
                            avg_length=length, avg_width=width,
                            uom="SF", sku=str(row.get("code") or ""),
                            image_url=str(row.get("photo") or ""),
                            source_url=source_url,
                        ))

                    if not (body.get("pagination") or {}).get("hasNext"):
                        break
                    page += 1
                    await asyncio.sleep(delay)
        out.ok = bool(out.materials)
        if not out.ok:
            out.error = "no materials returned"
    except Exception as e:  # noqa: BLE001 - one provider must not kill the crawl
        # Keep whatever was collected before the failure, like every other provider:
        # ingest stores the partial rows WITH the error rather than discarding a
        # supplier's whole catalog over one bad page.
        out.error = f"{type(e).__name__}: {e}"
        out.ok = out.ok or bool(out.materials)
    return out


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
