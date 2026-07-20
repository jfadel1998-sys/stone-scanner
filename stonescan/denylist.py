"""Hosts that must never be crawled — and must stay uncrawled.

Deleting a host from suppliers.json does not remove it. `discover` re-adds
anything it finds that isn't already listed, so a deletion survives exactly until
the next sweep. That made the project unable to honor the one request a public
crawler absolutely must honor: "stop indexing us."

The denylist is that missing memory. It is checked in three places, because a
removal has to hold against all three ways a host can come back:

    discover  — never re-add a denied host to suppliers.json
    ingest    — never crawl one, even if it was re-added by hand
    discover's fingerprint probes — never even fetch one to identify it

Matching covers subdomains: denying `example.com` also denies
`inventory.example.com`, since a supplier asking to be removed means the company,
not one hostname they happen to serve a catalog from.

    python -m stonescan.denylist add aria.example.com --reason "emailed request"
    python -m stonescan.denylist list
    python -m stonescan.denylist remove aria.example.com
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

DENYLIST_FILE = Path(
    os.environ.get("STONESCAN_DENYLIST")
    or (Path(__file__).resolve().parent.parent / "denylist.json")
)

_TEMPLATE = {
    "_comment": (
        "Hosts that must never be crawled or re-added by discovery. This is how a "
        "supplier's removal request is honored durably — deleting an entry from "
        "suppliers.json alone does not work, because the next discovery run re-adds "
        "it. Matching includes subdomains: 'example.com' also denies "
        "'inventory.example.com'. Use: python -m stonescan.denylist add <host> "
        "--reason \"...\""
    ),
    "denied": [],
}


def _normalize(host: str) -> str:
    host = (host or "").strip().lower()
    for prefix in ("https://", "http://"):
        host = host.removeprefix(prefix)
    return host.split("/")[0].removeprefix("www.")


def load() -> list[dict]:
    if not DENYLIST_FILE.exists():
        return []
    try:
        data = json.loads(DENYLIST_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A malformed denylist must not silently become an empty one — that would
        # quietly resume crawling everyone on it.
        raise
    return data.get("denied", [])


def denied_hosts() -> set[str]:
    return {_normalize(e.get("host", "")) for e in load() if e.get("host")}


def is_denied(host: str, denied: set[str] | None = None) -> bool:
    h = _normalize(host)
    for d in (denied if denied is not None else denied_hosts()):
        if h == d or h.endswith("." + d):
            return True
    return False


def reason_for(host: str) -> str:
    h = _normalize(host)
    for e in load():
        d = _normalize(e.get("host", ""))
        if d and (h == d or h.endswith("." + d)):
            return e.get("reason", "")
    return ""


def filter_entries(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split supplier entries into (crawlable, denied)."""
    denied = denied_hosts()
    if not denied:
        return list(entries), []
    keep, drop = [], []
    for e in entries:
        (drop if is_denied(e.get("host", ""), denied) else keep).append(e)
    return keep, drop


def _write(entries: list[dict]) -> None:
    data = dict(_TEMPLATE)
    if DENYLIST_FILE.exists():
        try:
            existing = json.loads(DENYLIST_FILE.read_text(encoding="utf-8"))
            data["_comment"] = existing.get("_comment", data["_comment"])
        except (json.JSONDecodeError, OSError):
            pass
    data["denied"] = sorted(entries, key=lambda e: e.get("host", ""))
    DENYLIST_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def add(host: str, reason: str = "", *, prune_suppliers: bool = True) -> bool:
    """Deny a host. Also drops it from suppliers.json so the next run skips it."""
    h = _normalize(host)
    if not h:
        return False
    entries = load()
    if any(_normalize(e.get("host", "")) == h for e in entries):
        return False
    entries.append({"host": h, "reason": reason or "removal requested",
                    "added": date.today().isoformat()})
    _write(entries)
    if prune_suppliers:
        _prune_suppliers(h)
    return True


def purge_data(host: str, db_path: str = "") -> dict[str, int]:
    """Erase already-collected data for a denied host (and its subdomains) from the
    database. Denying stops future crawls; this honors the removal for what is
    already stored. Aggregated across every matching supplier row.

    Returns zeroed totals (and never touches the DB) if there is no database yet,
    so calling it on a fresh install or in a test is safe.
    """
    from . import db

    totals = {"materials": 0, "slabs": 0, "history": 0, "image_vectors": 0,
              "list_items_kept": 0, "suppliers": 0}
    path = db_path or str(db.DEFAULT_DB)
    if not Path(path).exists():
        return totals
    conn = db.connect(path)
    try:
        hosts = [r["host"] for r in conn.execute("SELECT host FROM suppliers")]
        for h in hosts:
            if is_denied(h, {_normalize(host)}):
                res = db.purge_supplier(conn, h)
                totals["suppliers"] += 1
                for k, v in res.items():
                    totals[k] = totals.get(k, 0) + v
    finally:
        conn.close()
    return totals


def remove(host: str) -> bool:
    h = _normalize(host)
    entries = load()
    kept = [e for e in entries if _normalize(e.get("host", "")) != h]
    if len(kept) == len(entries):
        return False
    _write(kept)
    return True


def _prune_suppliers(host: str) -> int:
    """Remove a denied host (and its subdomains) from suppliers.json."""
    from .discover import SUPPLIERS_FILE

    if not SUPPLIERS_FILE.exists():
        return 0
    data = json.loads(SUPPLIERS_FILE.read_text(encoding="utf-8"))
    sups = data.get("suppliers", [])
    kept = [s for s in sups if not is_denied(s.get("host", ""), {host})]
    n = len(sups) - len(kept)
    if n:
        data["suppliers"] = kept
        SUPPLIERS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return n


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        description="Manage hosts that must never be crawled or re-discovered.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="Deny a host and drop it from suppliers.json.")
    a.add_argument("host")
    a.add_argument("--reason", default="", help="Why (shown in the file; for your records).")
    a.add_argument("--keep-data", action="store_true",
                   help="Deny future crawls but leave already-collected rows in the DB "
                        "(default is to erase them, which is what a removal request means).")
    a.add_argument("--db", default="", help="SQLite path (default: the app DB).")
    r = sub.add_parser("remove", help="Un-deny a host.")
    r.add_argument("host")
    sub.add_parser("list", help="Show the denylist.")
    c = sub.add_parser("check", help="Test whether a host would be crawled.")
    c.add_argument("host")
    args = ap.parse_args()

    if args.cmd == "add":
        newly = add(args.host, args.reason)
        if newly:
            print(f"Denied {_normalize(args.host)} (and its subdomains).")
            print(f"  {DENYLIST_FILE}")
        else:
            print(f"{_normalize(args.host)} was already denied.")
        # Erase what we already collected, unless told to keep it. Run this even for
        # an already-denied host: a first `add` may have denied it before this data
        # existed, or a later re-crawl slipped rows in — so re-running cleans up.
        if not args.keep_data:
            t = purge_data(args.host, args.db)
            if t["suppliers"]:
                print(f"Erased collected data from {t['suppliers']} supplier row(s): "
                      f"{t['materials']} materials, {t['slabs']} slabs, "
                      f"{t['history']} history rows, {t['image_vectors']} image vectors.")
                if t["list_items_kept"]:
                    print(f"  Kept {t['list_items_kept']} of your own sourcing-list "
                          "item(s) referencing them (they render as 'gone').")
            else:
                print("  No collected data to erase.")
    elif args.cmd == "remove":
        print("Removed." if remove(args.host) else "Not on the denylist.")
    elif args.cmd == "check":
        h = _normalize(args.host)
        if is_denied(h):
            print(f"DENIED  {h} — {reason_for(h) or 'no reason recorded'}")
        else:
            print(f"allowed {h}")
    else:
        entries = load()
        if not entries:
            print(f"Denylist is empty ({DENYLIST_FILE}).")
            return
        print(f"{len(entries)} denied host(s) — {DENYLIST_FILE}\n")
        for e in entries:
            print(f"  {e.get('host', ''):<38} {e.get('added', ''):<12} {e.get('reason', '')}")


if __name__ == "__main__":
    main()
