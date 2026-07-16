"""Orchestrator: (optionally discover) -> crawl -> normalize -> store.

Usage (from the project root):

    python -m stonescan.ingest                 # crawl the suppliers.json list
    python -m stonescan.ingest --discover      # also search for more public catalogs first
    python -m stonescan.ingest --limit 5       # crawl only the first 5 (quick test)
    python -m stonescan.ingest --show-browser  # run non-headless (helps if Cloudflare blocks)
"""

from __future__ import annotations

import argparse
import asyncio

from . import db, discover
from .crawler import crawl_hosts, utc_now_iso
from .normalize import normalize_item


def _build_slab_rows(result, supplier_id: int, crawled_at: str) -> list[dict]:
    """Flatten crawler slab data (ItemID -> [slabs]) into slab-table rows."""
    from urllib.parse import quote
    base = result.image_base or ""
    if base and not base.endswith("/"):
        base += "/"
    out = []
    for item_id, slabs in (result.slabs or {}).items():
        for s in slabs:
            fn = (s.get("FileName") or "").strip()
            out.append({
                "supplier_id": supplier_id,
                "item_id": str(item_id),
                "slab_no": (s.get("IDOne") or "").strip(),
                "location": (s.get("Location") or "").strip(),
                "length": s.get("AverageLength"),
                "width": s.get("AverageWidth"),
                "qty": s.get("AvailableQty"),
                "uom": (s.get("UOM") or "").strip(),
                "barcode": (s.get("Barcode") or "").strip(),
                "image_filename": fn,
                "image_url": f"{base}{quote(fn)}?width=1400" if (base and fn) else "",
                "crawled_at": crawled_at,
            })
    return out


def _store(conn, data, *, with_slabs: bool) -> tuple[int, int]:
    """Persist one provider's SupplierData; returns (materials, slabs)."""
    supplier_id = db.upsert_supplier(
        conn, host=data.host,
        company=data.company or None,
        products=data.products or None,
        image_base=data.image_base or None,
        phone=data.phone or None,
        email=data.email or None,
        last_crawled=utc_now_iso(),
        last_error=data.error or None,
    )
    for r in data.materials:
        r["supplier_id"] = supplier_id
    n = db.replace_materials(conn, supplier_id, data.materials)
    ns = 0
    if with_slabs and data.slabs:
        for s in data.slabs:
            s["supplier_id"] = supplier_id
        ns = db.replace_slabs(conn, supplier_id, data.slabs, utc_now_iso())
        db.backfill_locations(conn, supplier_id)
    db.snapshot_history(conn, supplier_id, utc_now_iso()[:10])
    return n, ns


async def run_providers(entries: list[dict], *, delay: float, db_path: str,
                        with_slabs: bool = False, limit_items: int = 0) -> tuple[int, int, int]:
    """Crawl every non-StoneProfits supplier entry, one provider at a time."""
    from . import providers

    conn = db.init_db(db_path)
    items = slabs = ok = 0
    for entry in entries:
        name = providers.provider_of(entry)
        label = entry.get("name") or entry.get("host")
        try:
            crawl = providers.get(name)
            data = await crawl(entry, with_slabs=with_slabs, delay=delay,
                               limit=limit_items)
        except Exception as e:  # noqa: BLE001 - a broken provider must not kill the run
            print(f"  [err]  {label:<34} {name}: {e}")
            continue
        if not data.ok:
            db.upsert_supplier(conn, host=data.host, last_crawled=utc_now_iso(),
                               last_error=data.error or "no items returned")
            print(f"  [skip] {label:<34} {data.error}")
            continue
        try:
            n, ns = _store(conn, data, with_slabs=with_slabs)
        except Exception as e:  # noqa: BLE001
            db.upsert_supplier(conn, host=data.host, last_error=f"store failed: {e}")
            print(f"  [err]  {label:<34} store failed: {e}")
            continue
        items += n
        slabs += ns
        ok += 1
        note = f" (+{ns} slabs)" if with_slabs else ""
        print(f"  [ok]   {label:<34} {n:>5} materials{note}  [{name}]")
    conn.close()
    return ok, items, slabs


async def run_all(entries: list[dict], *, concurrency: int = 3, delay: float = 1.5,
                  headless: bool = True, db_path: str = "", with_slabs: bool = False,
                  provider_limit: int = 0) -> None:
    """Crawl a mixed supplier list, routing each entry to its provider.

    Every caller that crawls "everything in suppliers.json" must come through here:
    the list is no longer all Stone Profits, and feeding a UMI/SlabWare entry to the
    Playwright crawler just produces a confusing "no items returned".
    """
    from . import providers

    db_path = db_path or str(db.DEFAULT_DB)
    sps = [e["host"] for e in entries
           if providers.provider_of(e) == providers.STONEPROFITS]
    other = [e for e in entries
             if providers.provider_of(e) != providers.STONEPROFITS]

    if sps:
        print(f"Crawling {len(sps)} Stone Profits catalog(s)...\n")
        await run(sps, concurrency=concurrency, delay=delay, headless=headless,
                  db_path=db_path, with_slabs=with_slabs)
    if other:
        kinds = ", ".join(sorted({providers.provider_of(e) for e in other}))
        print(f"\nCrawling {len(other)} other catalog(s) [{kinds}]...\n")
        ok, items, slabs = await run_providers(
            other, delay=max(delay * 0.2, 0.2), db_path=db_path,
            with_slabs=with_slabs, limit_items=provider_limit)
        print(f"\n  {ok} supplier(s), {items} materials"
              + (f", {slabs} slabs" if with_slabs else ""))


async def run(hosts: list[str], *, concurrency: int, delay: float, headless: bool,
              db_path: str, with_slabs: bool = False) -> None:
    conn = db.init_db(db_path)
    total_items = 0
    total_slabs = 0
    ok_suppliers = 0

    async for result in crawl_hosts(
        hosts, concurrency=concurrency, delay_s=delay, headless=headless, with_slabs=with_slabs
    ):
        supplier_id = db.upsert_supplier(
            conn,
            host=result.host,
            token=result.token or None,
            company=result.company or None,
            products=result.products or None,
            image_base=result.image_base or None,
            phone=result.phone or None,
            email=result.email or None,
            last_crawled=utc_now_iso(),
            last_error=result.error or None,
        )
        if result.ok:
            try:
                rows = []
                for it in result.items:
                    try:
                        r = normalize_item(it, result.host, utc_now_iso(), result.image_base)
                        r["supplier_id"] = supplier_id
                        rows.append(r)
                    except Exception as e:  # noqa: BLE001 - skip a single malformed item
                        print(f"         (skipped malformed item on {result.host}: {e})")
                n = db.replace_materials(conn, supplier_id, rows)
                total_items += n
                ok_suppliers += 1
                label = result.company or result.host
                slab_note = ""
                if with_slabs:
                    slab_rows = _build_slab_rows(result, supplier_id, utc_now_iso())
                    ns = db.replace_slabs(conn, supplier_id, slab_rows, utc_now_iso())
                    db.backfill_locations(conn, supplier_id)
                    total_slabs += ns
                    slab_note = f" (+{ns} slabs)"
                # Daily snapshot for trend / restock / new-arrival detection.
                db.snapshot_history(conn, supplier_id, utc_now_iso()[:10])
                print(f"  [ok]   {label:<34} {n:>5} materials{slab_note}")
            except Exception as e:  # noqa: BLE001 - one supplier must not kill the crawl
                db.upsert_supplier(conn, host=result.host, last_error=f"store failed: {e}")
                print(f"  [err]  {result.host:<34} store failed: {e}")
        else:
            print(f"  [skip] {result.host:<34} {result.error}")

    print("\n" + "=" * 60)
    s = db.stats(conn)
    print(f"Suppliers with data : {ok_suppliers}")
    print(f"Total materials     : {total_items}")
    if with_slabs:
        print(f"Slabs pre-cached    : {total_slabs}")
    print(f"Unique materials    : {s['unique_materials']} (grouped across suppliers)")
    if s["by_type"]:
        top = ", ".join(f"{t['material_type']} {t['n']}" for t in s["by_type"][:8])
        print(f"By type             : {top}")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl public Stone Profits catalogs into a material database.")
    ap.add_argument("--discover", action="store_true", help="Search for more public catalogs before crawling.")
    ap.add_argument("--limit", type=int, default=0, help="Only crawl the first N suppliers.")
    ap.add_argument("--concurrency", type=int, default=3, help="Parallel catalogs (default 3).")
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds to wait after each catalog.")
    ap.add_argument("--show-browser", action="store_true", help="Run a visible browser (non-headless).")
    ap.add_argument("--db", default=str(db.DEFAULT_DB), help="SQLite path.")
    ap.add_argument("--only", default="", help="Comma-separated hosts to crawl (overrides the list).")
    ap.add_argument("--stale-hours", type=float, default=0,
                    help="Incremental refresh: skip suppliers successfully crawled within this many hours.")
    ap.add_argument("--slabs", action="store_true",
                    help="Also pre-fetch and cache each in-stock item's full slab gallery (slower).")
    ap.add_argument("--provider-limit", type=int, default=0,
                    help="Cap materials per non-StoneProfits supplier (0 = all; handy for smoke tests).")
    args = ap.parse_args()

    if args.discover:
        print("Discovering public catalogs...")
        found = discover.discover_hosts()
        added = discover.merge_discovered(found)
        print(f"  found {len(found)} candidates, added {added} new.\n")

    entries = discover.load_suppliers()
    if args.only:
        want = {h.strip().lower() for h in args.only.split(",") if h.strip()}
        entries = [e for e in entries if e["host"].lower() in want]
        # Allow crawling a host that isn't in suppliers.json yet.
        known = {e["host"].lower() for e in entries}
        entries += [{"host": h} for h in want if h not in known]
    if args.limit:
        entries = entries[: args.limit]

    if args.stale_hours:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.stale_hours)).isoformat()
        conn = db.init_db(args.db)
        fresh = {r["host"] for r in conn.execute(
            "SELECT host FROM suppliers WHERE item_count > 0 AND last_crawled >= ?", (cutoff,)
        )}
        conn.close()
        before = len(entries)
        entries = [e for e in entries if e["host"] not in fresh]
        print(f"Incremental: skipping {before - len(entries)} supplier(s) refreshed in the last {args.stale_hours:g}h.\n")

    asyncio.run(
        run_all(
            entries,
            concurrency=args.concurrency,
            delay=args.delay,
            headless=not args.show_browser,
            db_path=args.db,
            with_slabs=args.slabs,
            provider_limit=args.provider_limit,
        )
    )


if __name__ == "__main__":
    main()
