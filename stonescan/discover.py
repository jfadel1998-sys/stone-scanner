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

Stone Profits also powers distributor "vanity" catalogs at `inventory.<distributor>`
/ `liveinventory.<distributor>` (Triton, Pacific Shore, Francini, AG&M, …) that the
`*.stoneprofitsweb.com` sweep never sees — `probe_sps_vanity()` fingerprints those.

UMI / StoneTrash are single sites — seeded by hand.

Discovered hosts are merged into suppliers.json (tagged with their provider); your
manual entries are kept.
"""

from __future__ import annotations

import json
import os
import re
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
     "skip": {"www", "app", "api", "admin", "portal", "static", "cdn", "mail", "blog"}},
]

_UA = {"User-Agent": "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"}


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


def merge_discovered(hosts: dict[str, str | None] | set[str]) -> int:
    """Add newly-discovered hosts to suppliers.json, tagged with their provider.

    Accepts either a {host: provider} map (from discover_all) or a bare set of
    Stone Profits hosts (from the legacy discover_hosts), for back-compat.
    """
    if isinstance(hosts, set):
        hosts = {h: None for h in hosts}
    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    existing = {s["host"].lower() for s in data.get("suppliers", [])}
    added = 0
    for host, provider in sorted(hosts.items()):
        if host.lower() in existing:
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
    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    sups = data.get("suppliers", [])
    hosts = {s["host"].lower() for s in sups}
    slugs = {(s.get("slug") or "").lower() for s in sups if s.get("provider") == "slabcloud"}
    added = 0
    for t in tenants:
        if t["host"].lower() in hosts or t["slug"].lower() in slugs:
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
]


def probe_sps_vanity(apexes: list[str], verbose: bool = True) -> set[str]:
    """Return the inventory.*/liveinventory.* hosts of `apexes` that serve a Stone
    Profits catalog (fingerprinted in the page HTML)."""
    hits: set[str] = set()
    with httpx.Client(follow_redirects=True, headers=_UA, timeout=25) as client:
        for apex in apexes:
            apex = apex.strip().lower().removeprefix("www.")
            for host in (f"inventory.{apex}", f"liveinventory.{apex}"):
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


if __name__ == "__main__":
    print("Discovering public catalogs across "
          + ", ".join(p["base"] for p in PLATFORMS) + " ...")
    found = discover_all()
    print(f"\nTotal distinct hosts discovered: {len(found)}")
    added = merge_discovered(found)
    print("\nResolving SlabCloud tenants from the clients directory...")
    sc = discover_slabcloud()
    added += merge_slabcloud(sc)
    print("\nFingerprinting distributor vanity domains for Stone Profits catalogs...")
    vanity = probe_sps_vanity(SPS_VANITY_CANDIDATES)
    added += merge_discovered({h: None for h in vanity})
    print(f"\nAdded {added} new supplier(s) to suppliers.json "
          f"(now {len(load_suppliers())} total).")
