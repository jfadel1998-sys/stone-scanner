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
from pathlib import Path

from . import db, denylist, discover
from .crawler import crawl_hosts, utc_now_iso
from .normalize import normalize_item


def _refresh_log(db_path: str, msg: str) -> None:
    """Append a timestamped line to refresh-history.log next to the DB — a durable record
    of every refresh's start and outcome, unlike the web app's in-memory `_refresh`
    summary which vanishes on restart. Never raises: logging must not break a refresh."""
    from datetime import datetime, timezone
    try:
        path = Path(db_path).resolve().parent / "refresh-history.log"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp}Z  {msg}\n")
    except Exception:  # noqa: BLE001 - a logging failure must not fail the crawl
        pass


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
        # "" not None: upsert_supplier drops None fields, so a successful crawl would
        # otherwise leave the previous error in place forever (and keep the host in the
        # nightly retry-errored pass).
        last_error=data.error or "",
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
                        with_slabs: bool = False, limit_items: int = 0,
                        progress=None) -> tuple[int, int, int]:
    """Crawl every non-StoneProfits supplier entry, one provider at a time."""
    from . import providers

    conn = db.init_db(db_path)
    items = slabs = ok = 0
    for entry in entries:
        name = providers.provider_of(entry)
        label = entry.get("name") or entry.get("host")
        n = 0
        try:
            crawl = providers.get(name)
            data = await crawl(entry, with_slabs=with_slabs, delay=delay,
                               limit=limit_items)
        except Exception as e:  # noqa: BLE001 - a broken provider must not kill the run
            print(f"  [err]  {label:<34} {name}: {e}")
            db.record_crawl_streak(conn, entry["host"], 0, "")
            if progress:
                progress(label, 0)
            continue
        if not data.ok:
            db.upsert_supplier(conn, host=data.host, last_crawled=utc_now_iso(),
                               last_error=data.error or "no items returned")
            print(f"  [skip] {label:<34} {data.error}")
            db.record_crawl_streak(conn, data.host, 0, data.error)
            if progress:
                progress(label, 0)
            continue
        try:
            n, ns = _store(conn, data, with_slabs=with_slabs)
        except Exception as e:  # noqa: BLE001
            db.upsert_supplier(conn, host=data.host, last_error=f"store failed: {e}")
            print(f"  [err]  {label:<34} store failed: {e}")
            db.record_crawl_streak(conn, data.host, 0, "")
            if progress:
                progress(label, 0)
            continue
        items += n
        slabs += ns
        ok += 1
        note = f" (+{ns} slabs)" if with_slabs else ""
        print(f"  [ok]   {label:<34} {n:>5} materials{note}  [{name}]")
        db.record_crawl_streak(conn, data.host, n, "")
        if progress:
            progress(label, n)
    conn.close()
    return ok, items, slabs


def _errored_hosts(db_path: str, hosts: list[str]) -> set[str]:
    """Which of `hosts` failed in a way worth retrying.

    A robots.txt block is excluded: it is the supplier's answer, not a blip, and
    an immediate retry would just ask again after being told no.
    """
    from .robots import BLOCK_MARKER

    conn = db.init_db(db_path)
    ph = ",".join("?" for _ in hosts)
    rows = conn.execute(
        f"SELECT host FROM suppliers WHERE last_error IS NOT NULL AND last_error <> '' "
        f"AND last_error NOT LIKE ? AND host IN ({ph})", [f"{BLOCK_MARKER}%", *hosts],
    ).fetchall() if hosts else []
    conn.close()
    return {r["host"] for r in rows}


async def _crawl_entries(entries, *, concurrency, delay, headless, db_path,
                         with_slabs, provider_limit, slab_item_cap=0, progress=None) -> None:
    """Crawl a mixed entry list once, routing each to Stone Profits or its provider."""
    from . import providers
    sps = [e["host"] for e in entries
           if providers.provider_of(e) == providers.STONEPROFITS]
    other = [e for e in entries
             if providers.provider_of(e) != providers.STONEPROFITS]
    if sps:
        print(f"Crawling {len(sps)} Stone Profits catalog(s)...\n")
        await run(sps, concurrency=concurrency, delay=delay, headless=headless,
                  db_path=db_path, with_slabs=with_slabs, slab_item_cap=slab_item_cap,
                  progress=progress)
    if other:
        kinds = ", ".join(sorted({providers.provider_of(e) for e in other}))
        print(f"\nCrawling {len(other)} other catalog(s) [{kinds}]...\n")
        ok, items, slabs = await run_providers(
            other, delay=max(delay * 0.2, 0.2), db_path=db_path,
            with_slabs=with_slabs, limit_items=provider_limit, progress=progress)
        print(f"\n  {ok} supplier(s), {items} materials"
              + (f", {slabs} slabs" if with_slabs else ""))


async def run_all(entries: list[dict], *, concurrency: int = 3, delay: float = 1.5,
                  headless: bool = True, db_path: str = "", with_slabs: bool = False,
                  provider_limit: int = 0, retry_errored: bool = False,
                  slab_item_cap: int = 0, honor_rejections: bool = True, progress=None) -> None:
    """Crawl a mixed supplier list, routing each entry to its provider.

    Every caller that crawls "everything in suppliers.json" must come through here:
    the list is no longer all Stone Profits, and feeding a UMI/SlabWare entry to the
    Playwright crawler just produces a confusing "no items returned".

    `retry_errored` gives anything that failed this run one more headless attempt,
    which recovers the transient blips (a Cloudflare challenge, a flaky page load)
    without re-crawling the whole list. Persistent failures just stay flagged.
    """
    db_path = db_path or str(db.DEFAULT_DB)
    _refresh_log(db_path, f"refresh started — {len(entries)} supplier "
                          f"entr{'y' if len(entries) == 1 else 'ies'}")

    # Data safety: snapshot the DB before we modify it, so a crash or disk error mid-
    # refresh leaves a restore point — the file also holds the user's watchlist/lists.
    # A backup failure must NEVER block the refresh: a stale catalog is the worse cost.
    try:
        if db.backup_database(db_path):
            _refresh_log(db_path, f"backed up to {Path(db_path).name}.bak")
        else:
            _refresh_log(db_path, "nothing to back up yet (no existing DB)")
    except Exception as e:  # noqa: BLE001 - proceed without a backup, but say so
        _refresh_log(db_path, f"WARNING: backup failed, proceeding without one: {e}")

    try:
        # Last line of defence for a removal request: even a hand-re-added entry, or
        # one restored from an old suppliers.json, does not get crawled.
        entries, denied = denylist.filter_entries(entries)
        for e in denied:
            print(f"  [deny] {e.get('name') or e['host']:<34} "
                  f"{denylist.reason_for(e['host']) or 'on the denylist'}")

        # Skip hosts with an active triage rejection (--only bypasses this; an explicit
        # request beats a stored rejection). A lapsed rejection lets the host through.
        if honor_rejections:
            entries, rej_skipped = discover.filter_rejected(entries)
            from datetime import date as _date
            _today = _date.today()
            for e, rej in rej_skipped:
                print(f"  [rejected] {e.get('name') or e['host']:<32} {rej.reason[:46]} "
                      f"(lapses in {rej.days_until_lapse(_today)}d)")

        crawled_hosts = [e["host"] for e in entries]
        await _crawl_entries(entries, concurrency=concurrency, delay=delay,
                             headless=headless, db_path=db_path, with_slabs=with_slabs,
                             provider_limit=provider_limit, slab_item_cap=slab_item_cap,
                             progress=progress)

        if retry_errored:
            failed = _errored_hosts(db_path, [e["host"] for e in entries])
            retry = [e for e in entries if e["host"] in failed]
            if retry:
                print(f"\nRetrying {len(retry)} catalog(s) that errored this run...\n")
                await _crawl_entries(retry, concurrency=concurrency, delay=delay,
                                     headless=headless, db_path=db_path,
                                     with_slabs=with_slabs, provider_limit=provider_limit,
                                     slab_item_cap=slab_item_cap, progress=progress)
                still = _errored_hosts(db_path, [e["host"] for e in retry])
                print(f"\n  retry recovered {len(retry) - len(still)} of {len(retry)}; "
                      f"{len(still)} still failing.")

        # Re-apply curator-confirmed merges LAST: every crawl (and retry) recomputes
        # material_key from scratch, so without this each /quality merge silently undoes.
        conn = db.init_db(db_path)
        folded = db.apply_aliases(conn)
        n_aliases = db.quality_stats(conn)["aliases"]
        # Flag duplicate-catalog storefronts (one tenant under two supplier names) so they
        # aren't double-counted in supplier totals/facets. Recomputed here every crawl.
        mirrors = db.detect_mirrors(conn)
        # Refresh the per-product rollup LAST, after material_key is final (aliases folded),
        # so the search fast path reflects this crawl.
        n_rollup = db.rebuild_product_rollup(conn)
        s = db.stats(conn, use_cache=False)
        conn.close()
        if folded:
            print(f"\n  re-applied {folded} row(s) from {n_aliases} confirmed merge(s).")
        if mirrors:
            print(f"  mirrors: {len(mirrors)} duplicate storefront(s) flagged, excluded "
                  f"from supplier counts ({', '.join(m['mirror_host'] for m in mirrors)}).")
        # Reconcile triage rejections: auto-reject hosts that hit the empty-crawl streak,
        # and restore any that returned items again. Writes suppliers.json.
        rec = discover.reconcile_rejections(db_path, crawled_hosts)
        if rec["rejected"]:
            print(f"  auto-rejected {len(rec['rejected'])} dead candidate(s) after "
                  f"{discover.AUTO_REJECT_STREAK} empty crawls: {', '.join(rec['rejected'])}")
        if rec["restored"]:
            print(f"  restored {len(rec['restored'])} host(s) that returned items: "
                  f"{', '.join(rec['restored'])}")
        print(f"  product rollup: {n_rollup} products indexed for fast browse.")
        _refresh_log(db_path, f"Done — {s['materials']} materials, {s['suppliers']} suppliers.")
    except Exception as e:  # noqa: BLE001 - record durably, then let the caller handle it
        import traceback
        _refresh_log(db_path, f"FAILED: {type(e).__name__}: {e}\n"
                              f"{traceback.format_exc().rstrip()}")
        raise


async def run(hosts: list[str], *, concurrency: int, delay: float, headless: bool,
              db_path: str, with_slabs: bool = False, slab_item_cap: int = 0,
              progress=None) -> None:
    conn = db.init_db(db_path)
    total_items = 0
    total_slabs = 0
    ok_suppliers = 0

    async for result in crawl_hosts(
        hosts, concurrency=concurrency, delay_s=delay, headless=headless,
        with_slabs=with_slabs, slab_item_cap=slab_item_cap
    ):
        n = 0
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
            # "" not None — see note in the provider path above.
            last_error=result.error or "",
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
        # Track the empty-crawl streak for auto-rejection (n is 0 on a skip/store-fail).
        db.record_crawl_streak(conn, result.host, n, result.error)
        if progress:
            progress(result.company or result.host, n)

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
    ap.add_argument("--slab-cap", type=int, default=0,
                    help="With --slabs, pre-cache only the N most-stocked items per supplier "
                         "(0 = all). Bounds the slowest part of the crawl; un-cached galleries "
                         "are fetched live when a user opens the item.")
    ap.add_argument("--provider-limit", type=int, default=0,
                    help="Cap materials per non-StoneProfits supplier (0 = all; handy for smoke tests).")
    ap.add_argument("--retry-errored", action="store_true",
                    help="Crawl ONLY the suppliers whose last crawl errored (a cheap, targeted "
                         "retry of the health page's failures; pair with --show-browser for Cloudflare).")
    ap.add_argument("--retry", action="store_true",
                    help="After the crawl, give anything that errored this run one more attempt.")
    args = ap.parse_args()

    if args.discover:
        print("Discovering public catalogs...")
        found = discover.discover_all()
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

    if args.retry_errored:
        from .robots import BLOCK_MARKER
        conn = db.init_db(args.db)
        failed = {r["host"] for r in conn.execute(
            "SELECT host FROM suppliers WHERE last_error IS NOT NULL AND last_error <> '' "
            "AND last_error NOT LIKE ?", (f"{BLOCK_MARKER}%",),
        )}
        conn.close()
        before = len(entries)
        entries = [e for e in entries if e["host"] in failed]
        print(f"Retry mode: {len(entries)} of {before} supplier(s) had a last-crawl error.\n")
        if not entries:
            print("Nothing to retry — no errored suppliers.")
            return

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
            retry_errored=args.retry,
            slab_item_cap=args.slab_cap,
            # --only is an explicit request to crawl exactly these hosts, so it overrides
            # any stored triage rejection (AC-6).
            honor_rejections=not bool(args.only),
        )
    )


if __name__ == "__main__":
    main()
