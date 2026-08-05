"""Merge a spill crawl back into the primary catalog.

AIL-28 lets a night proceed when the project drive is missing, by crawling into a second
catalog beside the packaged copy on C:. Until that work is merged it is invisible to the
database the app actually reads — the night was rescued and then quietly wasted.

This is the merge, and it is the most dangerous code in the refresh path: the only thing
besides a crawl that writes into the primary catalog, and unlike a crawl it writes data it
did not collect itself. Every decision below is shaped around not damaging it.

**Newest wins, strictly, per supplier.** A supplier is taken from the spill only when the
spill's newest `materials.crawled_at` for that supplier is greater than the primary's.
Equal leaves the primary alone. The drive comes back at unpredictable times, so a spill can
easily be older than a normal crawl that has since run — for some suppliers and not others,
in the same spill.

**That rule is also what makes a second merge a no-op.** Merging copies the spill's
`crawled_at` values verbatim, so afterwards the two are equal and "strictly greater" can
never fire again for the same data. There is no flag to get out of step with reality;
`spill_merges` records what happened, it does not decide it.

**Rows are replayed, not copied.** `materials.id` is reassigned on every crawl and half the
app keys off `(supplier_id, item_id)` instead, so moving rows by id would corrupt every
reference. The merge calls the same `replace_materials` / `replace_slabs` / `upsert_supplier`
the crawler calls, with `supplier_id` rewritten to the primary's own id for that host.

**All of it or none of it.** Those helpers commit as they go, which is right for a crawl —
a supplier that landed is banked — and wrong here, where a half-merged catalog is worse than
an unmerged one. `_OneTransaction` wraps the primary connection so their commits are
swallowed and the merge commits exactly once.

**Only crawl-owned tables move.** suppliers, materials, slabs, history. The watchlist,
sourcing lists, confirmed merges, storefront filters, rejections and compare tray live on
the primary and are never read from the spill nor written from it — the spill's own copies
of them are discarded, because they are a packaged app's empty defaults.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import db

# Tables a crawl owns, and the only ones that move. Anything not on this list is either the
# user's (watchlist, lists, aliases, filters, rejections, compare) or derived and rebuilt
# afterwards (product_rollup, product_fts).
CRAWL_TABLES = ("suppliers", "materials", "slabs", "history")

# Supplier columns carried across. Deliberately NOT the full row:
#   * `id` is the spill's, and the primary's id for the same host is usually different.
#   * `item_count` / `slab_count` / `slabs_cached_at` are set by replace_materials and
#     replace_slabs from what actually landed — carrying them would let the two disagree.
#   * `mirror_of` / `mirror_source` are recomputed by detect_mirrors at the end of every run.
_SUPPLIER_FIELDS = ("token", "company", "products", "image_base", "last_crawled",
                    "last_error", "phone", "email", "locations", "empty_streak")


def default_spill_db() -> Path:
    """Where AIL-28's fallback copy keeps its catalog.

    `desktop.setup_env()` puts the database in `<folder holding the exe>/data`, and the
    installer's spill branch runs `%ProgramData%\\StoneScanner\\StoneScanner.exe`, so the two
    agree by construction. STONESCAN_SPILL overrides for tests and for an install that put
    the local copy somewhere else.
    """
    root = os.environ.get("STONESCAN_SPILL")
    if root:
        return Path(root)
    base = os.environ.get("ProgramData") or "C:\\ProgramData"
    return Path(base) / "StoneScanner" / "data" / "stonescan.db"


@dataclass
class MergeResult:
    """What happened, in a form both the log line and /health can read."""

    status: str = "nothing"      # no-spill | nothing | refused | merged | failed
    moved: list[str] = field(default_factory=list)      # hosts taken from the spill
    skipped: list[str] = field(default_factory=list)    # hosts the primary already had fresher
    reason: str = ""
    watermark: str = ""
    source: str = ""

    def summary(self) -> str:
        if self.status == "no-spill":
            return "no spill database to merge"
        if self.status == "refused":
            return f"spill REFUSED: {self.reason}"
        if self.status == "nothing":
            return (f"spill has nothing newer ({len(self.skipped)} supplier(s) already "
                    f"current here)" if self.skipped else "spill has nothing to merge")
        return (f"spill merged: {len(self.moved)} supplier(s) moved, "
                f"{len(self.skipped)} skipped as stale")


class _OneTransaction:
    """A connection whose commit() does nothing, so its callers cannot bank a partial merge.

    `replace_materials`, `replace_slabs`, `upsert_supplier`, `snapshot_history` and
    `apply_aliases` each commit as they finish. That is correct for a crawl and fatal here:
    a merge that failed on supplier 40 of 60 would leave the catalog in a state nobody chose,
    with no record of which half is which. Wrapping the connection is what lets the merge use
    the crawl's real store path (so it cannot drift from how a crawl stores) AND commit
    exactly once. The real commit is issued by merge_spill, on the underlying connection.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.__dict__["_conn"] = conn

    def commit(self) -> None:      # deliberately nothing
        pass

    def rollback(self) -> None:    # ditto: only merge_spill decides to abandon
        pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_conn"], name)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def schema_mismatch(primary: sqlite3.Connection, spill: sqlite3.Connection) -> str:
    """Why these two databases must not be merged, or "" when they may be.

    A packaged copy can be older than the source — that is normal, not exotic, because the
    copy is only as current as the last build. An older spill is missing whatever columns
    have been added since, and replaying its rows would write NULL into every one of them.
    That is a silent partial erasure of the catalog (a spill from before AIL-22 would blank
    every derived `finish`), which is exactly the kind of damage a merge must refuse rather
    than average out.
    """
    for table in CRAWL_TABLES:
        want, have = _columns(primary, table), _columns(spill, table)
        if not have:
            return f"the spill has no {table} table"
        missing, extra = sorted(want - have), sorted(have - want)
        if missing:
            return (f"the spill's {table} is missing {', '.join(missing)} — it was built "
                    f"before those columns existed; rebuild the local copy")
        if extra:
            return (f"the spill's {table} has {', '.join(extra)}, which this database does "
                    f"not — the local copy is NEWER than the source")
    return ""


def _watermarks(conn: sqlite3.Connection) -> dict[str, str]:
    """host -> newest materials.crawled_at, for suppliers that actually have materials.

    The join is the load-bearing part. A supplier row in the spill with no material rows must
    never reach the merge: `replace_materials` would DELETE the primary's rows for that host
    and insert nothing, turning a merge into a deletion of a working catalog.
    """
    return {r["host"]: (r["newest"] or "") for r in conn.execute(
        """SELECT s.host host, MAX(m.crawled_at) newest
             FROM suppliers s JOIN materials m ON m.supplier_id = s.id
            GROUP BY s.host""")}


def _plan(primary: sqlite3.Connection, spill: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """(hosts to move, hosts to skip as stale)."""
    theirs, ours = _watermarks(spill), _watermarks(primary)
    move, skip = [], []
    for host, newest in sorted(theirs.items()):
        # Strictly greater. Equal means the same crawl is already here — which is what a
        # second merge of the same spill looks like, and what makes it a no-op.
        (move if newest > ours.get(host, "") else skip).append(host)
    return move, skip


def _move_supplier(primary, spill: sqlite3.Connection, host: str) -> None:
    """Replay one supplier's crawl output into the primary, as a crawl would have stored it."""
    src = spill.execute("SELECT * FROM suppliers WHERE host = ?", (host,)).fetchone()
    spill_sid = int(src["id"])
    fields = {k: src[k] for k in _SUPPLIER_FIELDS if k in src.keys()}
    sid = db.upsert_supplier(primary, host, **fields)

    def rows(table: str) -> list[dict[str, Any]]:
        # supplier_id is rewritten, never carried: the same host is a different id in each
        # database, and replace_materials inserts whatever the row dict says.
        return [{**dict(r), "supplier_id": sid} for r in
                spill.execute(f"SELECT * FROM {table} WHERE supplier_id = ?", (spill_sid,))]

    mats = rows("materials")
    db.replace_materials(primary, sid, mats)
    slabs = rows("slabs")
    crawled = max((m.get("crawled_at") or "" for m in mats), default="")
    db.replace_slabs(primary, sid, slabs, src["slabs_cached_at"] or crawled)

    # History is copied rather than recomputed, and the dates are why. snapshot_history
    # regenerates a snapshot under TODAY's date; the spill's rows belong to the night the
    # drive was missing, which is a different day. Restamping them would tell the alert
    # digest that last night's stock levels are tonight's. The table has no id column, so
    # copying rows moves nothing that gets reassigned.
    hcols = sorted(_columns(spill, "history"))
    placeholders = ", ".join("?" for _ in hcols)
    for h in spill.execute("SELECT * FROM history WHERE supplier_id = ?", (spill_sid,)):
        row = dict(h)
        row["supplier_id"] = sid
        primary.execute("DELETE FROM history WHERE supplier_id = ? AND snapshot_date = ?",
                        (sid, row["snapshot_date"]))
        primary.execute(f"INSERT INTO history ({', '.join(hcols)}) VALUES ({placeholders})",
                        tuple(row.get(c) for c in hcols))


def merge_spill(primary_db: str | Path | None = None, spill_db: str | Path | None = None, *,
                log: Callable[[str], None] | None = None,
                on_supplier: Callable[[str], None] | None = None) -> MergeResult:
    """Merge an unmerged spill crawl into the primary catalog. Never raises.

    `on_supplier` is a test seam: called with each host just before it is moved, so a test
    can raise part-way through and prove the primary is left untouched (AC-7).
    """
    primary_path = Path(primary_db or db.DEFAULT_DB)
    spill_path = Path(spill_db) if spill_db else default_spill_db()
    say = log or (lambda _m: None)
    res = MergeResult(source=str(spill_path))

    if not spill_path.exists() or spill_path.resolve() == primary_path.resolve():
        res.status = "no-spill"
        return res

    try:
        # --- decide, without writing anything ---------------------------------
        pconn, sconn = db.init_db(primary_path), db.connect(spill_path)
        try:
            res.reason = schema_mismatch(pconn, sconn)
            if res.reason:
                res.status = "refused"
                say(res.summary())
                return res
            move, skip = _plan(pconn, sconn)
            res.skipped = skip
            res.watermark = max((v for v in _watermarks(sconn).values()), default="")
            if res.watermark and res.watermark in db.spill_watermarks_merged(pconn):
                move = []          # already dealt with; the timestamps agree anyway
            if not move:
                say(res.summary())
                return res
        finally:
            pconn.close()
            sconn.close()

        # --- back up, then write ----------------------------------------------
        # AC-6. A merge writes into the primary from data it did not collect, so it earns at
        # least the safety net a crawl gets. Taken here, with no connection open, exactly as
        # run_all does — and only now that there is definitely something to merge, so the
        # ~25s checkpoint+copy is never spent on a night with no spill.
        try:
            if db.backup_database(primary_path):
                say(f"backed up to {primary_path.name}.bak before merging the spill")
        except Exception as e:  # noqa: BLE001 - warn and continue, as run_all does
            say(f"WARNING: pre-merge backup failed, merging anyway: {e}")

        pconn, sconn = db.init_db(primary_path), db.connect(spill_path)
        try:
            one = _OneTransaction(pconn)
            # Re-decide inside the transaction rather than trusting the plan above: the
            # backup sits between the two, and reading the state we are about to overwrite
            # at the moment we overwrite it costs one cheap query.
            move, skip = _plan(pconn, sconn)
            res.moved, res.skipped = [], skip
            for host in move:
                if on_supplier:
                    on_supplier(host)
                _move_supplier(one, sconn, host)
                res.moved.append(host)
            # AC-8. Every crawl re-derives material_key from scratch and would silently undo
            # each curator-confirmed merge without this; replayed rows are no different. The
            # rollup is rebuilt after, so the search fast path reflects what just landed —
            # the same order run_all uses, for the same reason.
            db.apply_aliases(one)
            db.rebuild_product_rollup(one)
            db.record_spill_merge(one, source=str(spill_path), watermark=res.watermark,
                                  moved=len(res.moved), skipped=len(res.skipped),
                                  detail=", ".join(res.moved)[:500])
            pconn.commit()          # the one and only commit
            res.status = "merged"
        except BaseException:
            pconn.rollback()
            raise
        finally:
            pconn.close()
            sconn.close()
    except Exception as e:  # noqa: BLE001 - a failed merge must not take the crawl with it
        res.status = "failed"
        res.reason = f"{type(e).__name__}: {e}"
        res.moved = []
        say(f"spill merge FAILED and was rolled back: {res.reason}")
        return res

    say(res.summary() + (f" ({', '.join(res.moved)})" if res.moved else ""))
    return res


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Merge a spill crawl into the primary catalog.")
    p.add_argument("--db", default="", help="primary database (default: the project's)")
    p.add_argument("--spill", default="", help=f"spill database (default: {default_spill_db()})")
    a = p.parse_args(argv)
    r = merge_spill(a.db or None, a.spill or None, log=print)
    return 0 if r.status in ("merged", "nothing", "no-spill") else 1


if __name__ == "__main__":
    raise SystemExit(main())
