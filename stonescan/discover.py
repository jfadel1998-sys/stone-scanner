"""Discover *public* catalogs as exhaustively as possible, across every platform
whose tenants live on enumerable wildcard subdomains.

Tenants can't be listed from certificate transparency alone (platforms serve a
single wildcard cert), so we aggregate subdomains that public internet-scanning
and passive-DNS services have *already observed*, then let the crawler decide which
ones actually expose a public catalog (private/login-gated tenants simply return no
items and are skipped — the /discovery page surfaces those for triage).

Sources (all free, no API key):
  * crt.sh                — certificate transparency
  * AlienVault OTX        — passive DNS
  * urlscan.io            — submitted-scan hostnames
  * HackerTarget          — hostsearch
  * subdomain.center      — aggregated subdomains
  * DuckDuckGo (HTML)     — search fallback (indexed public catalogs)

Platforms we CAN enumerate this way (multi-tenant on real subdomains):
  * Stone Profits  <tenant>.stoneprofitsweb.com   (provider: default)
  * SlabWare       <tenant>.slabware.com          (provider: slabware)

SlabCloud can't be DNS-swept (tenants are slugs on one shared origin), but it
publishes a clients directory — `discover_slabcloud()` reads each tenant's public
inventory page for its API slug and verifies the public API returns rows.

Stone Profits also powers distributor "vanity"/white-label catalogs on the
distributor's OWN domain (e.g. slabs.nsrstone.com, inventory.acegraniteusa.com,
outlet.ckfco.com) that the `*.stoneprofitsweb.com` sweep never sees. Two finders:
`discover_sps_embeds()` (urlscan — any domain, any prefix: it lists pages that load a
Stone Profits resource, then fingerprint-verifies each) and `probe_sps_vanity()`
(fingerprints `<prefix>.<distributor>` across the common prefixes for a curated apex
list). Both confirm the platform marker before adding, so false positives are dropped.

UMI / StoneTrash are single sites — seeded by hand.

Discovered hosts are merged into suppliers.json (tagged with their provider); your
manual entries are kept.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx

SUPPLIERS_FILE = Path(os.environ.get("STONESCAN_SUPPLIERS") or (Path(__file__).resolve().parent.parent / "suppliers.json"))

# provider=None means Stone Profits (the default; written without a "provider" key).
# `skip` are subdomain labels that are platform infrastructure, not tenants.
PLATFORMS: list[dict] = [
    {"base": "stoneprofitsweb.com", "provider": None,
     "skip": {"www", "pay", "apps", "nwww"}},
    {"base": "slabware.com", "provider": "slabware",
     # demo1/2/3, demolite, blog2 etc. are numbered siblings, which _is_infra's dashed-
     # variant rule can't reach from "demo"/"blog" — they need their own tokens. All were
     # observed live in the 2026-07-24 sweep and every one of them refused the inventory
     # endpoint, so they cost a nightly request each and can never return a catalog.
     "skip": {"www", "app", "api", "admin", "portal", "static", "cdn", "mail", "blog",
              "blog2", "demo", "demo1", "demo2", "demo3", "demolite", "webservice",
              "aatestex", "campaing"}},
]

_UA = {"User-Agent": "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"}


def _gate():
    """Denylist + robots gate for the probes.

    Fingerprinting a candidate means fetching its homepage, which is a crawl like
    any other. The aggregator queries above only read third-party indexes and need
    no gate; the moment we touch a supplier's own server, it applies.
    """
    from .denylist import denied_hosts, is_denied
    from .robots import SyncRobotsCache

    cache = SyncRobotsCache()
    denied = denied_hosts()

    def may_fetch(url: str) -> bool:
        host = url.split("//", 1)[-1].split("/")[0]
        return not is_denied(host, denied) and cache.allowed(url)

    return may_fetch


def _host_re(base: str) -> re.Pattern:
    # Trailing (?![a-z0-9.-]) so the base must END the host: without it,
    # "x.slabware.company" (next char 'p') or "x.slabware.com.br" (next char '.')
    # would each yield a spurious "x.slabware.com" for a different registrable domain.
    return re.compile(r"([a-z0-9][a-z0-9-]*\." + re.escape(base) + r")(?![a-z0-9.-])",
                      re.IGNORECASE)


def _is_infra(label: str, skip: set[str]) -> bool:
    """A subdomain label is platform infrastructure if it exactly matches a skip
    token or is one of its dashed variants (so 'api' also drops 'api-exporter')."""
    return any(label == s or label.startswith(s + "-") for s in skip)


def _hosts_in(text: str, base: str, skip: set[str]) -> set[str]:
    hosts = {m.group(1).lower() for m in _host_re(base).finditer(text or "")}
    # Drop the bare apex and any platform-infrastructure labels.
    return {h for h in hosts if h != base and not _is_infra(h.split(".", 1)[0], skip)}


# Each source returns the raw response text for a base domain; host extraction is
# done uniformly by the caller so a new platform needs no per-source changes.
def _crtsh(client: httpx.Client, base: str) -> str:
    return client.get(f"https://crt.sh/?q=%25.{base}&output=json", timeout=30).text


def _otx(client: httpx.Client, base: str) -> str:
    url = f"https://otx.alienvault.com/api/v1/indicators/domain/{base}/passive_dns"
    return client.get(url, timeout=30).text


def _urlscan(client: httpx.Client, base: str) -> str:
    return client.get(f"https://urlscan.io/api/v1/search/?q=domain:{base}&size=10000", timeout=30).text


def _hackertarget(client: httpx.Client, base: str) -> str:
    return client.get(f"https://api.hackertarget.com/hostsearch/?q={base}", timeout=30).text


def _subdomain_center(client: httpx.Client, base: str) -> str:
    return client.get(f"https://api.subdomain.center/?domain={base}", timeout=45).text


def _duckduckgo(client: httpx.Client, base: str) -> str:
    """Search fallback: concatenate a few indexed-catalog queries' HTML."""
    queries = [
        f"site:{base} granite marble quartz",
        f"site:{base} slabs inventory",
        f'"{base}" quartzite supplier stone',
    ]
    out = []
    for q in queries:
        try:
            out.append(client.post("https://html.duckduckgo.com/html/",
                                    data={"q": q}, headers=_UA, timeout=25).text)
        except Exception as e:  # noqa: BLE001
            print(f"    ddg '{q[:28]}...' failed: {e}")
    return "\n".join(out)


_SOURCES = [
    ("crt.sh", _crtsh),
    ("AlienVault OTX", _otx),
    ("urlscan.io", _urlscan),
    ("HackerTarget", _hackertarget),
    ("subdomain.center", _subdomain_center),
    ("DuckDuckGo", _duckduckgo),
]


def discover_platform(platform: dict, verbose: bool = True) -> set[str]:
    """All hosts observed for one platform's base domain."""
    base, skip = platform["base"], platform["skip"]
    found: set[str] = set()
    with httpx.Client(follow_redirects=True, headers=_UA) as client:
        for name, fn in _SOURCES:
            try:
                hosts = _hosts_in(fn(client, base), base, skip)
                new = hosts - found
                found |= hosts
                if verbose:
                    print(f"  [{base}] {name:<18} {len(hosts):>4} hosts ({len(new)} new)")
            except Exception as e:  # noqa: BLE001
                if verbose:
                    print(f"  [{base}] {name:<18} failed: {e}")
    return found


def discover_all(verbose: bool = True) -> dict[str, str | None]:
    """Sweep every enumerable platform. Returns {host: provider_or_None}."""
    out: dict[str, str | None] = {}
    for platform in PLATFORMS:
        for host in discover_platform(platform, verbose=verbose):
            out[host] = platform["provider"]
    return out


# Back-compat: the original single-platform Stone Profits sweep.
def discover_hosts(verbose: bool = True) -> set[str]:
    return discover_platform(PLATFORMS[0], verbose=verbose)


def load_suppliers() -> list[dict]:
    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    return data.get("suppliers", [])


# ---------------------------------------------------------------------------
# Rejected discovery candidates.
#
# The DNS sweep is deliberately wide: it adds every observed subdomain, and most of
# them (e.g. 41 of 47 SlabWare candidates in the 2026-07-24 sweep) can only ever 403.
# A `rejected` block on a suppliers.json entry records a triage decision so the crawl
# stops probing that host — the host stays LISTED (which is what stops merge_discovered
# re-adding it), it just isn't crawled. Rejections lapse after REJECTION_LAPSE_DAYS so a
# tenant that later opens a public catalog gets a fresh chance, and a host that fails
# AUTO_REJECT_STREAK crawls in a row is rejected automatically.
#
#     "rejected": {"reason": "...", "at": "2026-07-24"}
# ---------------------------------------------------------------------------

REJECTION_LAPSE_DAYS = 90
AUTO_REJECT_STREAK = 3


@dataclass
class Rejection:
    """A parsed `rejected` block. Invalid without both a `reason` and a parseable ISO `at`
    date — a malformed block fails loudly rather than silently suppressing a host forever."""

    reason: str
    at: date

    @classmethod
    def from_entry(cls, entry: dict) -> "Rejection | None":
        raw = (entry or {}).get("rejected")
        if not raw:
            return None
        host = (entry.get("host") or "") if entry else ""
        if not isinstance(raw, dict):
            raise ValueError(f"rejected for {host!r} must be an object with 'reason' and 'at'")
        reason = (raw.get("reason") or "").strip()
        at_raw = (raw.get("at") or "").strip()
        if not reason:
            raise ValueError(f"rejected for {host!r} needs a 'reason'")
        if not at_raw:
            raise ValueError(f"rejected for {host!r} needs an ISO 'at' date (YYYY-MM-DD)")
        try:
            at = date.fromisoformat(at_raw[:10])
        except ValueError as e:
            raise ValueError(f"rejected 'at' for {host!r} is not an ISO date: {at_raw!r}") from e
        return cls(reason, at)

    def is_active(self, today: date) -> bool:
        """True while the rejection still suppresses the host (inside the lapse window)."""
        return (today - self.at).days < REJECTION_LAPSE_DAYS

    def days_until_lapse(self, today: date) -> int:
        return REJECTION_LAPSE_DAYS - (today - self.at).days


def filter_rejected(entries: list[dict], *, today: date | None = None):
    """Split entries into (to_crawl, skipped). An ACTIVE rejection (present and inside the
    lapse window) suppresses the host; a lapsed one lets it through for a fresh probe.
    `skipped` is a list of (entry, Rejection). Raises on a malformed rejected block."""
    today = today or date.today()
    keep, skipped = [], []
    for e in entries:
        rej = Rejection.from_entry(e)
        if rej and rej.is_active(today):
            skipped.append((e, rej))
        else:
            keep.append(e)
    return keep, skipped


def reconcile_rejections(db_path=None, crawled_hosts=None, *,
                         threshold: int = AUTO_REJECT_STREAK, today: date | None = None) -> dict:
    """After a crawl, reconcile suppliers.json rejections against the fresh empty-streak.

    Only hosts actually attempted this run are touched. A host at/over `threshold`
    consecutive zero-item crawls is (re-)rejected with today's date; a rejected host whose
    latest crawl stored items (streak back to 0) is restored to normal service. Returns
    {'rejected': [...], 'restored': [...]}.
    """
    from . import db as _db
    today = today or date.today()
    crawled = {(h or "").lower() for h in (crawled_hosts or [])}
    conn = _db.connect(str(db_path or _db.DEFAULT_DB))
    streaks = {(r["host"] or "").lower(): (r["empty_streak"] or 0, r["last_error"] or "")
               for r in conn.execute("SELECT host, empty_streak, last_error FROM suppliers")}
    conn.close()

    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    rejected, restored = [], []
    for s in data.get("suppliers", []):
        host = (s.get("host") or "").lower()
        if host not in crawled:
            continue
        streak, last_error = streaks.get(host, (0, ""))
        if streak >= threshold:
            reason = f"{streak} consecutive zero-item crawls"
            if last_error:
                reason += f"; last: {last_error[:80]}"
            s["rejected"] = {"reason": reason, "at": today.isoformat()}
            rejected.append(s.get("host"))
        elif streak == 0 and s.get("rejected"):
            del s["rejected"]                       # stored items again -> back to normal service
            restored.append(s.get("host"))
    if rejected or restored:
        SUPPLIERS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"rejected": rejected, "restored": restored}


def merge_discovered(hosts: dict[str, str | None] | set[str]) -> int:
    """Add newly-discovered hosts to suppliers.json, tagged with their provider.

    Accepts either a {host: provider} map (from discover_all) or a bare set of
    Stone Profits hosts (from the legacy discover_hosts), for back-compat.
    """
    from .denylist import denied_hosts, is_denied

    if isinstance(hosts, set):
        hosts = {h: None for h in hosts}
    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    existing = {s["host"].lower() for s in data.get("suppliers", [])}
    denied = denied_hosts()
    added = 0
    for host, provider in sorted(hosts.items()):
        if host.lower() in existing:
            continue
        # A host is not "new" just because it isn't listed — it may have been
        # removed on request, and re-adding it here is exactly how that used to
        # silently undo itself.
        if is_denied(host, denied):
            continue
        entry: dict = {"host": host, "name": ""}
        if provider:
            entry["provider"] = provider
        data["suppliers"].append(entry)
        existing.add(host.lower())
        added += 1
    if added:
        SUPPLIERS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return added


# --- SlabCloud: resolve tenants from the public clients directory ---------------
# Each tenant embeds its API slug as company:"<slug>" in the IT_SPA(...) init call on
# its own inventory page; that slug is sent VERBATIM to slabcloud.com/api/slabs/<slug>
# (the "_h_" prefix on some tenants is part of the slug — dropping it returns a
# different, smaller dataset). We read it there and confirm the public API has rows.
SLABCLOUD_CLIENTS = "https://slabcloud.com/clients"
_SC_LINK = re.compile(r'<a[^>]+href="(https?://[^"]+)"[^>]*>\s*([^<]{2,60}?)\s*</a>')
_SC_COMPANY = re.compile(r'IT_SPA\(\{[^}]*?company:"([^"]+)"', re.IGNORECASE | re.DOTALL)
_SC_INV_PATH = re.compile(r"inventory|remnant|material|slab", re.IGNORECASE)


def _apex_name(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower().removeprefix("www.")
    return host.split(".")[0].replace("-", " ").title()


def _sc_rows(client: httpx.Client, slug: str) -> int:
    try:
        r = client.get(f"https://slabcloud.com/api/slabs/{quote(slug)}", timeout=30)
        if r.headers.get("content-type", "").startswith("application/json"):
            return len(r.json())
    except Exception:  # noqa: BLE001
        pass
    return 0


def discover_slabcloud(verbose: bool = True) -> list[dict]:
    """Resolve every published SlabCloud tenant to a supplier entry (host/slug/name)."""
    out: list[dict] = []
    may_fetch = _gate()
    with httpx.Client(follow_redirects=True, headers=_UA, timeout=30) as client:
        try:
            idx = client.get(SLABCLOUD_CLIENTS).text
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  slabcloud clients fetch failed: {e}")
            return out
        seen_url: set[str] = set()
        for url, _txt in _SC_LINK.findall(idx):
            if "slabcloud.com" in url or not _SC_INV_PATH.search(url) or url in seen_url:
                continue
            seen_url.add(url)
            if not may_fetch(url):   # the tenant's own site, so it gets a say
                continue
            try:
                m = _SC_COMPANY.search(client.get(url).text)
            except Exception:  # noqa: BLE001
                continue
            if not m:
                continue
            slug = m.group(1).strip()
            rows = _sc_rows(client, slug)
            if rows <= 0:
                continue
            host = slug.removeprefix("_h_") + ".slabcloud.com"
            out.append({"host": host, "slug": slug, "name": _apex_name(url), "provider": "slabcloud"})
            if verbose:
                print(f"  + {_apex_name(url):26} slug={slug:20} rows={rows}")
    return out


def merge_slabcloud(tenants: list[dict]) -> int:
    """Add SlabCloud tenants to suppliers.json, deduped by host AND slug."""
    from .denylist import denied_hosts, is_denied

    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    sups = data.get("suppliers", [])
    hosts = {s["host"].lower() for s in sups}
    slugs = {(s.get("slug") or "").lower() for s in sups if s.get("provider") == "slabcloud"}
    denied = denied_hosts()
    added = 0
    for t in tenants:
        if t["host"].lower() in hosts or t["slug"].lower() in slugs:
            continue
        if is_denied(t["host"], denied):
            continue
        sups.append({"host": t["host"], "slug": t["slug"], "name": t["name"], "provider": "slabcloud"})
        hosts.add(t["host"].lower())
        slugs.add(t["slug"].lower())
        added += 1
    if added:
        SUPPLIERS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return added


# --- Stone Profits on distributor vanity domains --------------------------------
# The crawler reads SPSWebToken out of the loaded page's JS, so a Stone Profits
# catalog served at inventory.<distributor>.com is drop-in on the DEFAULT provider —
# but such hosts never appear in the *.stoneprofitsweb.com sweep. Fingerprint them.
_SPS_MARKERS = ("stoneprofitsweb.com", "SPSWebToken", "getItemGallery", "nodemodules/angular")

# Distributor apexes to fingerprint for an inventory.* Stone Profits catalog. Extend
# freely — a confirmed hit (e.g. Pacific Shore, AG&M, Elements Room) is a full catalog
# on the DEFAULT provider that the *.stoneprofitsweb.com sweep can't see. Seed from the
# NSI / trade-show directories and any distributor site with a "live inventory" button.
SPS_VANITY_CANDIDATES: list[str] = [
    "pacificshorestones.com", "agmimports.com", "elementsroom.com",
    "architecturalsurfaces.com", "francinicollection.com", "tritonstone.com",
    "cosmos-surfaces.com", "marbleandgranite.com", "dwyermarble.com",
    "stoneland.com", "walkerzanger.com", "antolini.com", "levantina.com",
    # found via discover_sps_embeds (urlscan) — kept here so the probe re-confirms them
    "nsrstone.com", "acegraniteusa.com", "arcadiastones.net", "ohmintl.com",
]


# Vanity catalogs use varied subdomains, not just "inventory" (seen in the wild:
# inventory, liveinventory, slabs, catalog, outlet, products, stone, browse).
_SPS_VANITY_PREFIXES = ("inventory", "liveinventory", "slabs", "catalog",
                        "outlet", "products", "stone", "browse")


def probe_sps_vanity(apexes: list[str], verbose: bool = True) -> set[str]:
    """Return the <prefix>.<apex> hosts that serve a Stone Profits catalog
    (fingerprinted in the page HTML), across the common vanity prefixes."""
    hits: set[str] = set()
    may_fetch = _gate()
    with httpx.Client(follow_redirects=True, headers=_UA, timeout=25) as client:
        for apex in apexes:
            apex = apex.strip().lower().removeprefix("www.")
            for prefix in _SPS_VANITY_PREFIXES:
                host = f"{prefix}.{apex}"
                if not may_fetch(f"https://{host}/"):
                    continue
                try:
                    r = client.get(f"https://{host}/")
                    if any(mk in r.text for mk in _SPS_MARKERS):
                        hits.add(host)
                        if verbose:
                            print(f"  + {host}  (Stone Profits)")
                        break
                except Exception:  # noqa: BLE001
                    continue
    return hits


# urlscan indexes every domain touched in a scan, so pages that load a Stone Profits
# resource (from *.stoneprofits.com / *.stoneprofitsweb.com) but live on their OWN
# domain are vanity / white-label catalogs — findable regardless of subdomain prefix.
_SPS_API_DOMAINS = ("stoneprofits.com", "stoneprofitsweb.com")


def discover_sps_embeds(verbose: bool = True) -> set[str]:
    """Find vanity/white-label Stone Profits catalogs via urlscan (any domain/prefix),
    then fingerprint-verify each is a live catalog. Complements probe_sps_vanity's
    prefix guessing — this needs no candidate apex list at all."""
    candidates: set[str] = set()
    hits: set[str] = set()
    may_fetch = _gate()
    with httpx.Client(follow_redirects=True, headers=_UA, timeout=45) as client:
        for apex in _SPS_API_DOMAINS:
            try:
                data = client.get("https://urlscan.io/api/v1/search/",
                                  params={"q": f"domain:{apex}", "size": 10000}).json()
            except Exception:  # noqa: BLE001
                continue
            for res in data.get("results", []):
                pd = ((res.get("page") or {}).get("domain") or "").lower()
                if pd and "stoneprofits" not in pd:
                    candidates.add(pd)
        for host in sorted(candidates):
            if not may_fetch(f"https://{host}/"):
                continue
            try:
                r = client.get(f"https://{host}/", timeout=20)
                if any(mk in r.text for mk in _SPS_MARKERS):
                    hits.add(host)
                    if verbose:
                        print(f"  + {host}  (Stone Profits)")
            except Exception:  # noqa: BLE001
                continue
    return hits


if __name__ == "__main__":
    print("Discovering public catalogs across "
          + ", ".join(p["base"] for p in PLATFORMS) + " ...")
    found = discover_all()
    print(f"\nTotal distinct hosts discovered: {len(found)}")
    added = merge_discovered(found)
    print("\nResolving SlabCloud tenants from the clients directory...")
    sc = discover_slabcloud()
    added += merge_slabcloud(sc)
    print("\nFinding white-label Stone Profits catalogs via urlscan (any domain)...")
    vanity = discover_sps_embeds()
    print("Fingerprinting distributor vanity domains for Stone Profits catalogs...")
    vanity |= probe_sps_vanity(SPS_VANITY_CANDIDATES)
    added += merge_discovered({h: None for h in vanity})
    print(f"\nAdded {added} new supplier(s) to suppliers.json "
          f"(now {len(load_suppliers())} total).")
