"""Discover *public* Stone Profits catalogs as exhaustively as possible.

Tenants can't be enumerated from certificate transparency (the platform serves a
single wildcard cert), so we aggregate subdomains that public internet-scanning
and passive-DNS services have *already observed*, then let the crawler decide
which ones actually expose a public catalog (private/login-gated tenants simply
return no items and are skipped).

Sources (all free, no API key):
  * crt.sh                — certificate transparency
  * AlienVault OTX        — passive DNS
  * urlscan.io            — submitted-scan hostnames
  * HackerTarget          — hostsearch
  * subdomain.center      — aggregated subdomains
  * DuckDuckGo (HTML)     — search fallback (indexed public catalogs)

Discovered hosts are merged into suppliers.json; your manual entries are kept.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

SUPPLIERS_FILE = Path(os.environ.get("STONESCAN_SUPPLIERS") or (Path(__file__).resolve().parent.parent / "suppliers.json"))
BASE_DOMAIN = "stoneprofitsweb.com"
HOST_RE = re.compile(r"([a-z0-9][a-z0-9-]*\." + re.escape(BASE_DOMAIN) + r")", re.IGNORECASE)
_SKIP = {"www." + BASE_DOMAIN, "pay." + BASE_DOMAIN, "www.pay." + BASE_DOMAIN, BASE_DOMAIN}

_UA = {"User-Agent": "Mozilla/5.0 (compatible; StoneScanner/0.1; +public-catalog indexer)"}


def _hosts_in(text: str) -> set[str]:
    return {m.group(1).lower() for m in HOST_RE.finditer(text or "")} - _SKIP


def _crtsh(client: httpx.Client) -> set[str]:
    r = client.get(f"https://crt.sh/?q=%25.{BASE_DOMAIN}&output=json", timeout=30)
    return _hosts_in(r.text)


def _otx(client: httpx.Client) -> set[str]:
    url = f"https://otx.alienvault.com/api/v1/indicators/domain/{BASE_DOMAIN}/passive_dns"
    r = client.get(url, timeout=30)
    return _hosts_in(r.text)


def _urlscan(client: httpx.Client) -> set[str]:
    url = f"https://urlscan.io/api/v1/search/?q=domain:{BASE_DOMAIN}&size=10000"
    r = client.get(url, timeout=30)
    return _hosts_in(r.text)


def _hackertarget(client: httpx.Client) -> set[str]:
    r = client.get(f"https://api.hackertarget.com/hostsearch/?q={BASE_DOMAIN}", timeout=30)
    return _hosts_in(r.text)


def _subdomain_center(client: httpx.Client) -> set[str]:
    r = client.get(f"https://api.subdomain.center/?domain={BASE_DOMAIN}", timeout=45)
    return _hosts_in(r.text)


_DDG_QUERIES = [
    "site:stoneprofitsweb.com granite marble quartz",
    "site:stoneprofitsweb.com slabs inventory",
    '"stoneprofitsweb.com" quartzite supplier stone',
]


def _duckduckgo(client: httpx.Client) -> set[str]:
    hosts: set[str] = set()
    for q in _DDG_QUERIES:
        try:
            r = client.post("https://html.duckduckgo.com/html/", data={"q": q}, headers=_UA, timeout=25)
            hosts |= _hosts_in(r.text)
        except Exception as e:  # noqa: BLE001
            print(f"    ddg '{q[:28]}...' failed: {e}")
    return hosts


_SOURCES = [
    ("crt.sh", _crtsh),
    ("AlienVault OTX", _otx),
    ("urlscan.io", _urlscan),
    ("HackerTarget", _hackertarget),
    ("subdomain.center", _subdomain_center),
    ("DuckDuckGo", _duckduckgo),
]


def discover_hosts(verbose: bool = True) -> set[str]:
    found: set[str] = set()
    with httpx.Client(follow_redirects=True, headers=_UA) as client:
        for name, fn in _SOURCES:
            try:
                hosts = fn(client)
                new = hosts - found
                found |= hosts
                if verbose:
                    print(f"  {name:<18} {len(hosts):>4} hosts ({len(new)} new)")
            except Exception as e:  # noqa: BLE001
                if verbose:
                    print(f"  {name:<18} failed: {e}")
    return found


def load_suppliers() -> list[dict]:
    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    return data.get("suppliers", [])


def merge_discovered(hosts: set[str]) -> int:
    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    existing = {s["host"].lower() for s in data.get("suppliers", [])}
    added = 0
    for h in sorted(hosts):
        if h.lower() not in existing:
            data["suppliers"].append({"host": h, "name": ""})
            existing.add(h.lower())
            added += 1
    if added:
        SUPPLIERS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return added


if __name__ == "__main__":
    print(f"Discovering public catalogs on *.{BASE_DOMAIN} ...")
    hosts = discover_hosts()
    print(f"\nTotal distinct hosts discovered: {len(hosts)}")
    added = merge_discovered(hosts)
    print(f"Added {added} new supplier(s) to suppliers.json "
          f"(now {len(load_suppliers())} total).")
