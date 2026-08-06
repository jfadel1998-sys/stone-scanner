"""Tests for the data-quality / merge / discovery feature (#3).

Self-contained: builds a synthetic materials DB in a temp file, so it needs no
crawl and no shipped stonescan.db. Run from the project root with the project venv
(needs httpx for the discover import):

    python -m unittest tests.test_quality           # or: python -m pytest tests/

Each test crafts the exact rows that exercise one behavior, so a failure points at a
specific regression rather than "something in the pipeline".
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stonescan import db, dedupe, discover  # noqa: E402
from stonescan import normalize as nz  # noqa: E402


def _seed_suppliers(conn, upto=60):
    """materials.supplier_id has a FK to suppliers(id); create rows to satisfy it."""
    for i in range(1, upto + 1):
        conn.execute("INSERT OR IGNORE INTO suppliers (id, host) VALUES (?, ?)",
                     (i, f"s{i}.example.com"))
    conn.commit()


def _insert(conn, supplier_id, name, mtype, slabs=1, item_id=None, image=""):
    """Insert one synthetic material row (only the columns the engine reads)."""
    conn.execute(
        """INSERT INTO materials
             (supplier_id, item_id, item_name, name_norm, material_key, material_type,
              available_slabs, image_url)
           VALUES (?,?,?,?,?,?,?,?)""",
        (supplier_id, item_id or f"{supplier_id}-{name}", name, name.upper(),
         nz.material_key(name, mtype), mtype, slabs, image),
    )


def _add_key(conn, key, mtype, n_suppliers, base_supplier=1):
    """Insert n rows for a material_key under distinct supplier_ids (so COUNT(DISTINCT
    supplier_id) == n_suppliers). Name is derived from the key's base."""
    base = key.rsplit("|", 1)[0]
    for i in range(n_suppliers):
        conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key, material_type,
                  available_slabs, image_url)
               VALUES (?,?,?,?,?,?,?,?)""",
            (base_supplier + i, f"{base_supplier+i}-{key}", base.title(), base.upper(),
             key, mtype, 1, ""),
        )


class SlabCountTests(unittest.TestCase):
    """The number the app shows for a material must be honest.

    A material spans many suppliers, so a cross-supplier SUM of available_slabs
    reads as one buyable pile when it is really N separate yards. The search /
    material headline must show the DEEPEST SINGLE YARD (max per supplier), and a
    tile's square-foot quantity must never be summed as if it were a slab count.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=10)

    def tearDown(self):
        self.conn.close()

    def _mat(self, supplier_id, key, *, slabs=0, form="SLAB", uom="", length=0, width=0):
        base = key.rsplit("|", 1)[0]
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key,
                  material_type, product_form, available_slabs, uom, avg_length, avg_width)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (supplier_id, f"{supplier_id}-{slabs}-{form}", base.title(), base.upper(),
             key, key.rsplit("|", 1)[1].title(), form, slabs, uom, length, width))
        self.conn.commit()

    def _search(self, **kw):
        from stonescan.web import app
        kw.setdefault("q", ""); kw.setdefault("material_type", ""); kw.setdefault("color", "")
        kw.setdefault("thickness", ""); kw.setdefault("supplier", "")
        kw.setdefault("limit", 20); kw.setdefault("offset", 0)
        _total, rows, _ = app._search(self.conn, **kw)
        return {r["material_key"]: r for r in rows}

    def test_headline_is_deepest_yard_not_cross_supplier_sum(self):
        # Same material at three yards: 100, 40, 10 slabs.
        self._mat(1, "taj mahal|quartzite", slabs=100)
        self._mat(2, "taj mahal|quartzite", slabs=40)
        self._mat(3, "taj mahal|quartzite", slabs=10)
        r = self._search(q="taj")["taj mahal|quartzite"]
        self.assertEqual(r["suppliers"], 3)
        self.assertEqual(r["available_slabs"], 100, "should be the single deepest yard")
        self.assertEqual(r["total_slabs"], 150, "grand total kept, separately")

    def test_per_slab_row_supplier_sums_within_the_yard(self):
        # OHM-style: 5 rows of 1 slab each at ONE supplier = 5 at that yard, not max=1.
        for _ in range(5):
            self._mat(1, "fantasy brown|marble", slabs=1)
        self._mat(2, "fantasy brown|marble", slabs=2)
        r = self._search(q="fantasy")["fantasy brown|marble"]
        self.assertEqual(r["available_slabs"], 5, "supplier 1's rows sum to 5 within its yard")
        self.assertEqual(r["total_slabs"], 7)

    def test_tile_sf_is_not_counted_as_slabs(self):
        self._mat(1, "river white|granite", slabs=2, form="SLAB")
        self._mat(1, "river white|granite", slabs=2493, form="TILE", uom="SF")
        r = self._search(q="river")["river white|granite"]
        self.assertEqual(r["available_slabs"], 2, "the 2493 SF tile must not become slabs")
        self.assertEqual(round(r["tile_sf"]), 2493)
        self.assertEqual(r["has_tile"], 1)

    def test_min_sqft_requires_one_yard_to_hold_the_area(self):
        # Two yards, ~69 ft² each (100x100 in, 1 slab). Neither alone reaches 100 ft².
        self._mat(1, "blue bahia|granite", slabs=1, length=100, width=100)
        self._mat(2, "blue bahia|granite", slabs=1, length=100, width=100)
        self.assertIn("blue bahia|granite", self._search(q="blue", min_sqft=50),
                      "one yard has ~69 ft², so 50 passes")
        self.assertNotIn("blue bahia|granite", self._search(q="blue", min_sqft=100),
                         "no single yard has 100 ft², so it must be filtered out")

    def test_in_stock_filter_still_works_across_the_rollup(self):
        self._mat(1, "empty stone|granite", slabs=0)
        self._mat(2, "stocked stone|granite", slabs=3)
        keys = self._search(q="stone", in_stock=True)
        self.assertIn("stocked stone|granite", keys)
        self.assertNotIn("empty stone|granite", keys)


class ProductRollupTests(unittest.TestCase):
    """The materialized product_rollup fast path must return results IDENTICAL to the
    live two-level query for the cases it handles (default browse, material-type facet,
    in_stock / min_sqft, sorts, pagination), and must rebuild when the data changes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=10)

    def tearDown(self):
        from stonescan.web import app
        app._search_cache.clear()
        self.conn.close()

    def _mat(self, sid, key, *, slabs=0, form="SLAB", uom="", length=0, width=0,
             image="", new=0, mtype=None, name=None):
        base = key.rsplit("|", 1)[0] if key else "nameless"
        mt = mtype or (key.rsplit("|", 1)[1].title() if "|" in key else "Other")
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key, material_type,
                  product_form, available_slabs, uom, avg_length, avg_width, image_url, new_arrival)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, f"{sid}-{base}-{form}-{slabs}-{length}", name or base.title(),
             (name or base).upper(), key, mt, form, slabs, uom, length, width, image, new))
        self.conn.commit()

    def _seed_catalog(self):
        # Ties on the relevance keys, a tile, an out-of-stock product, images of both
        # kinds, and empty-key products — the shapes that stress the rollup's parity.
        self._mat(1, "alpha|marble", slabs=10, length=120, width=70, image="http://x/a.jpg")
        self._mat(2, "alpha|marble", slabs=20, length=118, width=66)
        self._mat(3, "alpha|marble", slabs=30, length=126, width=72)
        self._mat(1, "eta|marble", slabs=10)   # ties alpha on (suppliers?,slabs) partially
        self._mat(2, "eta|marble", slabs=20)
        self._mat(3, "eta|marble", slabs=30, new=1)
        self._mat(1, "gamma|granite", slabs=5)
        self._mat(2, "gamma|granite", slabs=5)
        self._mat(1, "delta|granite", slabs=5)  # same available_slabs as gamma, fewer suppliers
        self._mat(1, "epsilon|quartzite", slabs=0)  # out of stock
        # A SlabCloud thumbnail must lose to a real photo (COALESCE(real_img, any_img)).
        self._mat(1, "kappa|marble", slabs=8, image="http://slabcloud.com/slabs/x.jpg")
        self._mat(2, "kappa|marble", slabs=3, image="http://real/k.jpg")
        # Tile: SF quantity must surface as tile_sf/has_tile, never as slabs.
        self._mat(1, "zeta|porcelain", slabs=500, form="TILE", uom="SF")
        # An accessory product: excluded by default, returned only when explicitly asked.
        self._mat(1, "brushpad|accessory / non-slab", slabs=1,
                  mtype="Accessory / Non-Slab", name="Polishing Pad")
        # Empty-key products (grp = 'id:<id>'): different slab depth so ordering is
        # decided before the empty material_key tiebreaker.
        self._mat(4, "", slabs=7, name="Nameless One", mtype="Marble")
        self._mat(5, "", slabs=4, name="Nameless Two", mtype="Marble")

    def _search(self, **kw):
        from stonescan.web import app
        base = dict(q="", material_type="", color="", thickness="", supplier="",
                    limit=60, offset=0)
        base.update(kw)
        return app._search(self.conn, **base)

    def _live_and_fast(self, **kw):
        """Same query two ways: with the rollup emptied (live path) and rebuilt (fast
        path). The cache is cleared between them so the second call really recomputes."""
        from stonescan.web import app
        app._search_cache.clear()
        self.conn.execute("DELETE FROM product_rollup")
        self.conn.commit()
        live = self._search(**kw)
        app._search_cache.clear()
        n = db.rebuild_product_rollup(self.conn)
        self.assertGreater(n, 0, "rollup must be populated so the fast path is exercised")
        fast = self._search(**kw)
        return live, fast

    def test_fast_path_matches_live_across_the_matrix(self):
        self._seed_catalog()
        matrix = [
            {},                                              # default browse
            {"sort": "relevance"}, {"sort": "slabs"}, {"sort": "size"},
            {"sort": "area"}, {"sort": "new"},
            {"sort": "distance"},                            # must degrade to relevance
            {"material_type": "Marble"}, {"material_type": "Granite"},
            {"material_type": "Accessory / Non-Slab"},       # only when explicitly asked
            {"in_stock": True}, {"min_sqft": 5.0},
            {"material_type": "Marble", "in_stock": True},
            {"in_stock": True, "min_sqft": 3.0, "sort": "slabs"},
            {"limit": 3, "offset": 0}, {"limit": 3, "offset": 3},
            {"limit": 3, "offset": 6}, {"sort": "slabs", "limit": 4, "offset": 2},
        ]
        for kw in matrix:
            live, fast = self._live_and_fast(**kw)
            self.assertEqual(live[0], fast[0], f"total differs for {kw}")
            self.assertEqual(live[1], fast[1], f"rows differ for {kw}")

    def test_default_excludes_accessory_but_filter_includes_it(self):
        self._seed_catalog()
        db.rebuild_product_rollup(self.conn)
        default_keys = {r["material_key"] for r in self._search()[1]}
        self.assertNotIn("brushpad|accessory / non-slab", default_keys)
        acc_keys = {r["material_key"]
                    for r in self._search(material_type="Accessory / Non-Slab")[1]}
        self.assertIn("brushpad|accessory / non-slab", acc_keys)

    def test_empty_rollup_falls_back_to_live_not_empty(self):
        # A DB with materials but no rollup yet (e.g. a shipped seed) must still return
        # results — the fast path falls through to the live query.
        from stonescan.web import app
        self._seed_catalog()
        app._search_cache.clear()
        self.conn.execute("DELETE FROM product_rollup")
        self.conn.commit()
        total, rows, _ = self._search()
        self.assertGreater(total, 0)
        self.assertGreater(len(rows), 0)

    def test_ensure_builds_only_when_empty_with_materials(self):
        self.assertEqual(db.ensure_product_rollup(self.conn), 0, "no materials -> nothing")
        self._seed_catalog()
        built = db.ensure_product_rollup(self.conn)
        self.assertGreater(built, 0, "materials but empty rollup -> builds")
        self.assertEqual(db.ensure_product_rollup(self.conn), 0, "already built -> no-op")

    def test_empty_catalog_returns_nothing_without_error(self):
        db.rebuild_product_rollup(self.conn)          # empty materials -> empty rollup
        total, rows, _ = self._search()
        self.assertEqual((total, rows), (0, []))

    def test_rebuild_reflects_a_merge(self):
        # Two same-named products of different type; merging folds them, and the rollup
        # rebuilt afterward must show a single product with the combined supplier count.
        self._mat(1, "taj mahal|granite", slabs=4)
        self._mat(2, "taj mahal|quartzite", slabs=9)
        db.add_alias(self.conn, "taj mahal|granite", "taj mahal|quartzite", "Quartzite")
        db.apply_aliases(self.conn)
        db.rebuild_product_rollup(self.conn)
        rows = {r["material_key"]: r for r in self._search()[1]}
        self.assertIn("taj mahal|quartzite", rows)
        self.assertNotIn("taj mahal|granite", rows)
        self.assertEqual(rows["taj mahal|quartzite"]["suppliers"], 2)
        self.assertEqual(rows["taj mahal|quartzite"]["available_slabs"], 9)


class FtsSearchTests(unittest.TestCase):
    """The FTS5 name index accelerates free-text q queries, falling back to the live
    LIKE + fuzzy path for low-recall / misspelled / row-filtered queries and when FTS5
    is unavailable — so q never returns worse-than-today results."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.init_db(os.path.join(self.tmp, "t.db"))
        _seed_suppliers(self.conn, upto=10)
        self.fts = db._has_fts(self.conn)
        # >= 5 'calacatta' products so the FTS fast path (threshold 5) actually engages.
        for i, key in enumerate([
            "calacatta gold|marble", "calacatta borghini|marble", "calacatta oro|marble",
            "calacatta vagli|marble", "calacatta michelangelo|marble",
            "calacatta lincoln|quartz"], start=1):
            self._mat(i, key, slabs=3 + i)
        self._mat(1, "taj mahal|quartzite", slabs=9)
        self._mat(2, "absolute black|granite", slabs=4)
        db.rebuild_product_rollup(self.conn)
        from stonescan.web import app
        app._NAME_WORDS = None
        app._search_cache.clear()

    def tearDown(self):
        from stonescan.web import app
        app._NAME_WORDS = None
        app._search_cache.clear()
        self.conn.close()

    def _mat(self, sid, key, *, slabs=0, thickness="3cm"):
        base = key.rsplit("|", 1)[0]
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key, material_type,
                  product_form, available_slabs, thickness)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (sid, f"{sid}-{base}-{thickness}", base.title(), base.upper(), key,
             key.rsplit("|", 1)[1].title(), "SLAB", slabs, thickness))
        self.conn.commit()

    def _keys(self, **kw):
        from stonescan.web import app
        base = dict(q="", material_type="", color="", thickness="", supplier="",
                    limit=60, offset=0)
        base.update(kw)
        app._search_cache.clear()
        _t, rows, _ = app._search(self.conn, **base)
        return {r["material_key"] for r in rows}

    def test_word_query_returns_all_matches(self):
        if not self.fts:
            self.skipTest("FTS5 not available in this SQLite build")
        keys = self._keys(q="calacatta")
        self.assertEqual(len([k for k in keys if k.startswith("calacatta")]), 6)
        self.assertNotIn("taj mahal|quartzite", keys)

    def test_prefix_query_matches(self):
        if not self.fts:
            self.skipTest("FTS5 not available")
        keys = self._keys(q="calac")   # word-prefix
        self.assertEqual(len([k for k in keys if k.startswith("calacatta")]), 6)

    def test_query_with_material_type_filters(self):
        if not self.fts:
            self.skipTest("FTS5 not available")
        keys = self._keys(q="calacatta", material_type="Marble")
        self.assertNotIn("calacatta lincoln|quartz", keys)   # quartz one filtered out
        self.assertIn("calacatta gold|marble", keys)

    def test_misspelling_falls_back_to_fuzzy(self):
        # 'calacata' yields no FTS token match (< 5) -> live path -> fuzzy near-miss recall.
        keys = self._keys(q="calacata")
        self.assertTrue(any(k.startswith("calacatta") for k in keys),
                        "misspelling should still resolve via the live fuzzy fallback")

    def test_row_filter_with_q_falls_back_and_is_correct(self):
        # thickness is a row-filter -> fall back to live; only the matching thickness returns.
        self._mat(1, "calacatta gold|marble", slabs=2, thickness="2cm")  # a 2cm variant
        db.rebuild_product_rollup(self.conn)
        self.assertIn("calacatta gold|marble", self._keys(q="calacatta", thickness="3cm"))
        self.assertIn("calacatta gold|marble", self._keys(q="calacatta", thickness="2cm"))
        self.assertEqual(self._keys(q="calacatta", thickness="9cm"), set())

    def test_fts_absent_falls_back_without_error(self):
        # Simulate a SQLite without FTS5: drop the index; q must still work via live.
        if self.fts:
            self.conn.execute("DROP TABLE product_fts")
            self.conn.commit()
        keys = self._keys(q="calacatta")
        self.assertTrue(any(k.startswith("calacatta") for k in keys))

    def test_new_material_indexed_on_rebuild(self):
        if not self.fts:
            self.skipTest("FTS5 not available")
        self.assertEqual(self._keys(q="unobtainium"), set())
        for i in range(1, 6):
            self._mat(i, f"unobtainium {i}|quartzite", slabs=2)
        db.rebuild_product_rollup(self.conn)
        self.assertEqual(len(self._keys(q="unobtainium")), 5)


class DataSafetyTests(unittest.TestCase):
    """A refresh rewrites stonescan.db in place (the file also holds the user's
    watchlist/lists), so it must snapshot the DB first and record its outcome durably."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=3)
        _insert(self.conn, 1, "Taj Mahal", "Quartzite", slabs=5)
        _insert(self.conn, 2, "Absolute Black", "Granite", slabs=3)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _log_text(self):
        log = os.path.join(self.tmp, "refresh-history.log")
        return Path(log).read_text(encoding="utf-8") if os.path.exists(log) else ""

    def test_backup_creates_valid_copy(self):
        self.assertTrue(db.backup_database(self.path))
        bak = self.path + ".bak"
        self.assertTrue(os.path.exists(bak))
        c = db.connect(bak)
        n = c.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
        c.close()
        self.assertEqual(n, 2, "the backup must be a complete, openable copy")

    def test_backup_missing_source_returns_false(self):
        self.assertFalse(db.backup_database(os.path.join(self.tmp, "nope.db")))

    def test_backup_failure_leaves_old_bak_intact(self):
        from unittest.mock import patch
        self.assertTrue(db.backup_database(self.path))         # good backup: 2 materials
        _insert(self.conn, 3, "New Stone", "Marble", slabs=1)  # DB now has 3
        self.conn.commit()
        with patch("stonescan.db.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                db.backup_database(self.path)
        c = db.connect(self.path + ".bak")
        n = c.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
        c.close()
        self.assertEqual(n, 2, "a failed backup must not corrupt the previous good one")
        self.assertFalse(os.path.exists(self.path + ".bak.tmp"), "the temp file is cleaned up")

    def test_run_all_backs_up_and_logs_outcome(self):
        import asyncio
        from stonescan.ingest import run_all
        # Empty entry list = no network: exercises the backup + logging around a no-op crawl.
        asyncio.run(run_all([], db_path=self.path))
        self.assertTrue(os.path.exists(self.path + ".bak"), "run_all snapshots before writing")
        text = self._log_text()
        self.assertIn("refresh started", text)
        self.assertIn("Done", text, "a successful refresh is recorded durably")

    def test_run_all_proceeds_when_backup_fails(self):
        import asyncio
        from unittest.mock import patch
        from stonescan.ingest import run_all
        with patch("stonescan.db.backup_database", side_effect=OSError("disk full")):
            asyncio.run(run_all([], db_path=self.path))  # must NOT raise
        text = self._log_text()
        self.assertIn("backup failed", text)
        self.assertIn("Done", text, "the refresh still completes without a backup")


class StockChangesTests(unittest.TestCase):
    """'Back in stock' must compare each supplier against ITS OWN previous
    snapshot — the original compared two global dates that shared no suppliers,
    so it silently returned zero forever while real signal was discarded."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=5)

    def tearDown(self):
        self.conn.close()

    def _set_materials(self, supplier_id, products):
        """Replace a supplier's materials with (name, slabs, form) tuples."""
        self.conn.execute("DELETE FROM materials WHERE supplier_id = ?", (supplier_id,))
        for name, slabs, *rest in products:
            form = rest[0] if rest else "SLAB"
            self.conn.execute(
                """INSERT INTO materials
                     (supplier_id, item_id, item_name, name_norm, material_key,
                      material_type, thickness, finish, product_form, available_slabs, uom)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (supplier_id, f"{supplier_id}-{name}", name, name.upper(),
                 f"{name.lower()}|granite", "Granite", "3cm", "", form, slabs,
                 "SF" if form == "TILE" else ""))
        self.conn.commit()

    def _changes(self):
        from stonescan.web import app
        return app._stock_changes(self.conn)

    def test_restock_compares_each_supplier_to_its_own_baseline(self):
        # Supplier 1: crawled on d1 (X out of stock) and d3 (X restocked, Z new).
        self._set_materials(1, [("X", 0), ("Y", 5)])
        db.snapshot_history(self.conn, 1, "2026-07-01")
        self._set_materials(1, [("X", 4), ("Y", 5), ("Z", 2)])
        db.snapshot_history(self.conn, 1, "2026-07-03")
        # Supplier 2: crawled ONCE, on d2 — the global two most recent dates
        # (d3, d2) share no suppliers, which is exactly the shape that used to
        # make the comparison come up empty (or, before that, fabricate rows).
        self._set_materials(2, [("W", 9)])
        db.snapshot_history(self.conn, 2, "2026-07-02")

        restock, listed, has_baseline = self._changes()
        self.assertTrue(has_baseline)
        self.assertEqual([r["name_norm"] for r in restock], ["X"],
                         "X went 0 -> 4 at supplier 1's own baseline")
        self.assertEqual(restock[0]["since"], "2026-07-01")
        self.assertEqual([r["name_norm"] for r in listed], ["Z"],
                         "Z is newly listed, not a restock")
        names = {r["name_norm"] for r in restock + listed}
        self.assertNotIn("Y", names, "unchanged stock is not a change")
        self.assertNotIn("W", names,
                         "a supplier with one snapshot has no baseline to compare")

    def test_no_baseline_reports_honestly(self):
        self._set_materials(1, [("X", 3)])
        db.snapshot_history(self.conn, 1, "2026-07-01")
        restock, listed, has_baseline = self._changes()
        self.assertFalse(has_baseline)
        self.assertEqual((restock, listed), ([], []))

    def test_snapshot_excludes_tile_square_footage_from_slabs(self):
        """A restock alert reading '2493 slabs' for a tile listing would be flatly
        wrong — history.slabs must count slabs only."""
        self._set_materials(1, [("RIVER", 2, "SLAB"), ("RIVER TILE", 2493, "TILE")])
        db.snapshot_history(self.conn, 1, "2026-07-01")
        rows = {r["name_norm"]: r["slabs"] for r in self.conn.execute(
            "SELECT name_norm, slabs FROM history WHERE supplier_id = 1")}
        self.assertEqual(rows["RIVER"], 2)
        self.assertEqual(rows["RIVER TILE"], 0, "tile SF must not be stored as slabs")


class FreshnessTests(unittest.TestCase):
    """The 'updated' claim must describe when the DATA was collected, not when the
    freshest (or merely most recently attempted) crawl ran."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=3)
        for sid, ts in ((1, "2026-07-10T08:00:00+00:00"), (2, "2026-07-14T08:00:00+00:00")):
            self.conn.execute(
                """INSERT INTO materials (supplier_id, item_id, item_name, name_norm,
                     material_key, material_type, available_slabs, crawled_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (sid, f"i{sid}", "S", "S", "s|granite", "Granite", 1, ts))
            self.conn.execute("UPDATE suppliers SET item_count = 1 WHERE id = ?", (sid,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_stats_reports_the_range_not_the_single_freshest(self):
        s = db.stats(self.conn)
        self.assertEqual(s["oldest_updated"][:10], "2026-07-10")
        self.assertEqual(s["last_updated"][:10], "2026-07-14")

    def test_failed_crawls_do_not_advance_the_freshness_claim(self):
        # A failed crawl stamps suppliers.last_crawled but must not move the data
        # range — that was the latent half of the bug: a night of 104 Cloudflare
        # errors would have advanced "updated" over an unchanged catalog.
        db.upsert_supplier(self.conn, host="s1.example.com",
                           last_crawled="2026-07-19T03:00:00+00:00",
                           last_error="timeout loading catalog")
        s = db.stats(self.conn)
        self.assertEqual(s["last_updated"][:10], "2026-07-14")

    def test_health_separates_data_age_from_attempt_age(self):
        db.upsert_supplier(self.conn, host="s1.example.com",
                           last_crawled="2026-07-19T03:00:00+00:00",
                           last_error="timeout loading catalog")
        h = {r["host"]: r for r in db.supplier_health(self.conn)}
        r = h["s1.example.com"]
        self.assertEqual(r["status"], "stale")
        # The attempt is recent; the data is old. Both must be visible.
        self.assertLess(r["age_hours"], r["data_age_hours"])


class LocationCoverageTests(unittest.TestCase):
    """Location filters must not silently drop suppliers that lack slab-level
    location data — fall back to the supplier's known yards, and say out loud
    what still can't be placed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=5)

        def mat(sid, name, locations):
            self.conn.execute(
                """INSERT INTO materials (supplier_id, item_id, item_name, name_norm,
                     material_key, material_type, available_slabs, locations)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (sid, f"{sid}-{name}", name, name.upper(),
                 f"{name.lower()}|granite", "Granite", 3, locations))

        mat(1, "Alpha", "Dallas")            # slab-level location
        mat(2, "Beta", None)                 # none of its own; supplier has yards
        self.conn.execute("UPDATE suppliers SET locations = 'Dallas' WHERE id = 2")
        mat(3, "Gamma", None)                # no location data anywhere
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _search(self, **kw):
        from stonescan.web import app
        kw.setdefault("q", ""); kw.setdefault("material_type", ""); kw.setdefault("color", "")
        kw.setdefault("thickness", ""); kw.setdefault("supplier", "")
        kw.setdefault("limit", 20); kw.setdefault("offset", 0)
        total, rows, parsed = app._search(self.conn, **kw)
        return {r["material_key"] for r in rows}, parsed

    def test_supplier_yards_fall_back_when_material_has_no_location(self):
        keys, _ = self._search(location="Dallas")
        self.assertIn("alpha|granite", keys)
        self.assertIn("beta|granite", keys,
                      "no slab-level location, but its supplier's yard is Dallas")
        self.assertNotIn("gamma|granite", keys)

    def test_unplaceable_matches_are_counted_not_hidden(self):
        _, parsed = self._search(location="Dallas")
        self.assertEqual(parsed.get("unlocated"),
                         {"suppliers": 1, "materials": 1},
                         "supplier 3 matches everything but location and must be surfaced")

    def test_no_location_filter_no_survey(self):
        _, parsed = self._search()
        self.assertNotIn("unlocated", parsed)


class AliasApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn)

    def tearDown(self):
        self.conn.close()

    def _count(self, key):
        return self.conn.execute(
            "SELECT COUNT(*) c FROM materials WHERE material_key=?", (key,)).fetchone()["c"]

    def test_apply_is_idempotent(self):
        _add_key(self.conn, "taj mahal|granite", "Granite", 2)
        db.add_alias(self.conn, "taj mahal|granite", "taj mahal|quartzite", "Quartzite")
        _add_key(self.conn, "taj mahal|quartzite", "Quartzite", 3)
        first = db.apply_aliases(self.conn)
        self.assertEqual(first, 2)
        self.assertEqual(self._count("taj mahal|granite"), 0)
        self.assertEqual(self._count("taj mahal|quartzite"), 5)
        self.assertEqual(db.apply_aliases(self.conn), 0)  # nothing left to fold

    def test_chain_converges_forward_order(self):
        # add a->b THEN b->c  (canonical becomes an alias after the fact)
        _add_key(self.conn, "a|granite", "Granite", 2)
        _add_key(self.conn, "b|granite", "Granite", 2)
        _add_key(self.conn, "c|granite", "Granite", 2)
        db.add_alias(self.conn, "a|granite", "b|granite")
        db.add_alias(self.conn, "b|granite", "c|granite")
        db.apply_aliases(self.conn)
        self.assertEqual(self._count("a|granite"), 0)
        self.assertEqual(self._count("b|granite"), 0)
        self.assertEqual(self._count("c|granite"), 6)

    def test_chain_converges_reverse_order(self):
        # add b->c FIRST, then a->b : a must resolve straight to terminal c
        _add_key(self.conn, "a|granite", "Granite", 2)
        _add_key(self.conn, "b|granite", "Granite", 2)
        _add_key(self.conn, "c|granite", "Granite", 2)
        db.add_alias(self.conn, "b|granite", "c|granite")
        db.add_alias(self.conn, "a|granite", "b|granite")
        aliases = {a["alias_key"]: a["canonical_key"] for a in db.list_aliases(self.conn)}
        self.assertEqual(aliases["a|granite"], "c|granite")  # normalized to terminal
        db.apply_aliases(self.conn)
        self.assertEqual(self._count("c|granite"), 6)

    def test_self_alias_ignored(self):
        _add_key(self.conn, "x|granite", "Granite", 1)
        db.add_alias(self.conn, "x|granite", "x|granite")
        self.assertEqual(len(db.list_aliases(self.conn)), 0)

    def test_rejection_roundtrip(self):
        db.add_rejection(self.conn, "conflict:taj mahal")
        self.assertIn("conflict:taj mahal", db.rejections(self.conn))
        db.add_rejection(self.conn, "conflict:taj mahal")  # idempotent
        self.assertEqual(len(db.rejections(self.conn)), 1)


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.init_db(os.path.join(self.tmp, "t.db"))
        _seed_suppliers(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_conflict_cluster_prefers_strong_type(self):
        # A big weak 'Other' bucket and a smaller strong 'Granite' one.
        _add_key(self.conn, "azul|other", "Other", 9)
        _add_key(self.conn, "azul|granite", "Granite", 2)
        cl = next(c for c in dedupe.conflict_clusters(self.conn) if c["base"] == "azul")
        self.assertEqual(cl["canonical_key"], "azul|granite")   # strong type wins canonical
        self.assertEqual(cl["canonical_type"], "Granite")

    def test_rejected_conflict_not_proposed(self):
        _add_key(self.conn, "azul|other", "Other", 3)
        _add_key(self.conn, "azul|granite", "Granite", 2)
        db.add_rejection(self.conn, "conflict:azul")
        self.assertFalse(any(c["base"] == "azul" for c in dedupe.conflict_clusters(self.conn)))

    def test_fuzzy_cluster_and_stable_sig(self):
        _add_key(self.conn, "calacata gold|marble", "Marble", 2)
        _add_key(self.conn, "calacatta gold|marble", "Marble", 3)
        cl = next(iter(dedupe.fuzzy_clusters(self.conn)))
        self.assertEqual(cl["canonical_key"], "calacatta gold|marble")  # most-stocked spelling
        # sig keyed on stable (type, normalized-name), not the volatile member key set
        self.assertEqual(cl["sig"], "fuzzy:marble:calacattagold")

    def test_auto_conflicts_gate_measures_canonical_not_biggest(self):
        # Weak 'Other'(9) is biggest, strong 'Granite'(2) is canonical. The OLD gate
        # (biggest >= 4*rest -> 9>=8) fired wrongly; the fixed gate measures the
        # canonical (2 >= 4*9 -> false) and must REFUSE.
        _add_key(self.conn, "azul|other", "Other", 9)
        _add_key(self.conn, "azul|granite", "Granite", 2)
        self.assertEqual(dedupe.auto_conflicts(self.conn, dominance=4.0), 0)
        self.assertEqual(db.quality_stats(self.conn)["aliases"], 0)

    def test_auto_conflicts_merges_true_landslide(self):
        _add_key(self.conn, "taj|quartzite", "Quartzite", 8)
        _add_key(self.conn, "taj|granite", "Granite", 1)
        merged = dedupe.auto_conflicts(self.conn, dominance=4.0)
        self.assertEqual(merged, 1)
        # auto path must actually fold the rows, not just record the alias
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM materials WHERE material_key='taj|granite'").fetchone()["c"], 0)


class NormalizeTests(unittest.TestCase):
    def test_derive_color_multiword_first(self):
        self.assertEqual(nz.derive_color_from_name("Off White Marble"), "Off White")
        self.assertEqual(nz.derive_color_from_name("Blue Bahia"), "Blue")
        self.assertEqual(nz.derive_color_from_name("Absolute Black"), "Black")

    def test_derive_color_none(self):
        self.assertEqual(nz.derive_color_from_name("Taj Mahal"), "")

    def test_color_only_fallback(self):
        # structured Color present -> used; blank -> derived from name
        row = nz.normalize_item({"ItemName": "Fusion Blue", "Color": ""}, "h", "t")
        self.assertEqual(row["color"], "Blue")
        row2 = nz.normalize_item({"ItemName": "Fusion Blue", "Color": "Grey"}, "h", "t")
        self.assertEqual(row2["color"], "Grey")

    def test_accessory_wordboundary(self):
        acc = "Accessory / Non-Slab"
        self.assertEqual(nz.canonical_type("", "", "STONE 5X2 36 GRIT"), acc)
        self.assertEqual(nz.canonical_type("", "", "ACETONE 1 GALLON"), acc)
        self.assertEqual(nz.canonical_type("", "", "BACKER 4 VELCRO STIFF"), acc)
        # a real stone whose name merely contains the letters must NOT be an accessory
        self.assertNotEqual(nz.canonical_type("", "", "Gritstone 3cm"), acc)

    def test_catalog_classification(self):
        acc = "Accessory / Non-Slab"
        # junk / tools / PPE -> accessory
        for junk in ("MAKITA BRUSHES 9565", "CUTTERS #2", "FACE MASK",
                     "MESH DIAMOND BLADE", "16 OZ Dry Treat Countertop Cleaner",
                     "SLAB RACK STEEL"):
            self.assertEqual(nz.canonical_type("", "", junk), acc, junk)
        # "blanco" is Spanish white, not the sink brand -> never accessory by itself
        self.assertNotEqual(nz.canonical_type("", "", "Blanco Atlantico 3CM"), acc)
        self.assertEqual(nz.canonical_type("", "", "SILESTONE BLANCO ORION P SLAB"), "Quartz")
        # "slab" in the name protects real slabs from over-broad keywords
        self.assertNotEqual(
            nz.canonical_type("", "", "WHITE DIAMOND POLISHED CALCITE SLAB 2CM"), acc)
        # quartz brands rescued out of "Other"
        self.assertEqual(nz.canonical_type("", "", "VALIANT TECH CALCATTA VERONA 2CM"), "Quartz")
        self.assertEqual(nz.canonical_type("", "", "QZ FERRARA WHITE 3CM"), "Quartz")


class DiscoverTests(unittest.TestCase):
    def test_host_boundary_rejects_lookalike_domains(self):
        skip = discover.PLATFORMS[1]["skip"]
        got = discover._hosts_in(
            "real.slabware.com x.slabware.company y.slabware.com.br api-eua.slabware.com",
            "slabware.com", skip)
        self.assertEqual(got, {"real.slabware.com"})

    def test_infra_prefix_filter(self):
        skip = discover.PLATFORMS[1]["skip"]
        got = discover._hosts_in("api.slabware.com api-x.slabware.com artstone.slabware.com",
                                 "slabware.com", skip)
        self.assertEqual(got, {"artstone.slabware.com"})

    def test_merge_tags_provider(self):
        tmp = tempfile.mkdtemp()
        supfile = os.path.join(tmp, "suppliers.json")
        Path(supfile).write_text(json.dumps({"suppliers": [{"host": "existing.slabware.com"}]}))
        os.environ["STONESCAN_SUPPLIERS"] = supfile
        import importlib
        importlib.reload(discover)
        try:
            added = discover.merge_discovered({
                "new.slabware.com": "slabware",
                "newsps.stoneprofitsweb.com": None,
                "existing.slabware.com": "slabware",  # dup -> skipped
            })
            self.assertEqual(added, 2)
            entries = {e["host"]: e for e in discover.load_suppliers()}
            self.assertEqual(entries["new.slabware.com"].get("provider"), "slabware")
            self.assertNotIn("provider", entries["newsps.stoneprofitsweb.com"])  # SPS default
            json.loads(Path(supfile).read_text())  # still valid JSON
        finally:
            os.environ.pop("STONESCAN_SUPPLIERS", None)
            importlib.reload(discover)


class NonProductionTenantTests(unittest.TestCase):
    """AIL-26: a platform's own test/staging tenants are not suppliers. Nine `test*` hosts
    reached suppliers.json from the SlabWare sweep; seven returned nothing and cost a request
    every night until they auto-rejected."""

    def test_every_token_matches_its_bare_and_dashed_form(self):
        for token in discover.NON_PRODUCTION:
            self.assertTrue(discover.is_non_production(f"{token}.slabware.com"), token)
            self.assertTrue(discover.is_non_production(f"{token}-acme.slabware.com"), token)

    def test_leaves_alone_the_real_hosts_a_prefix_match_would_swallow(self):
        # The point of the change, not incidental coverage: each of these is a live host that
        # a plain startswith() would delete.
        for host in ("devinecountertops.stoneprofitsweb.com",  # real supplier, 63 materials
                     "qatarmarble.slabware.com",               # starts with "qa"
                     "teste.slabware.com"):                    # "test" + "e" — a distinct label
            self.assertFalse(discover.is_non_production(host), host)

    def test_every_platform_gets_the_shared_set_on_top_of_its_own_tokens(self):
        for p in discover.PLATFORMS:
            self.assertLessEqual(set(discover.NON_PRODUCTION), p["skip"], p["base"])
        # Composes with the platform's own tokens rather than replacing them, and both
        # platforms get it — not just the one where the problem was observed.
        self.assertEqual(
            discover._hosts_in("demolite.slabware.com test-x.slabware.com art.slabware.com",
                               "slabware.com", discover.PLATFORMS[1]["skip"]),
            {"art.slabware.com"})
        self.assertEqual(
            discover._hosts_in("staging.stoneprofitsweb.com devinecountertops.stoneprofitsweb.com",
                               "stoneprofitsweb.com", discover.PLATFORMS[0]["skip"]),
            {"devinecountertops.stoneprofitsweb.com"})

    def test_merge_blocks_a_new_one_but_never_touches_a_listed_tenant(self):
        tmp = tempfile.mkdtemp()
        supfile = os.path.join(tmp, "suppliers.json")
        # test-uniquartz is already listed and productive (241 materials). The skip set is
        # about what we ADD; it must not evict what is already earning its keep.
        Path(supfile).write_text(json.dumps({"suppliers": [
            {"host": "test-uniquartz.slabware.com", "provider": "slabware"}]}))
        os.environ["STONESCAN_SUPPLIERS"] = supfile
        import importlib
        importlib.reload(discover)
        try:
            added = discover.merge_discovered({
                # The vanity/embed probes fingerprint arbitrary domains and never consult a
                # platform skip set, so this one can only be caught in the merge.
                "staging.nsrstone.com": None,
                "test-api-exporter.slabware.com": "slabware",
                "art.slabware.com": "slabware",          # a real tenant still gets through
            })
            self.assertEqual(added, 1)
            self.assertEqual({e["host"] for e in discover.load_suppliers()},
                             {"test-uniquartz.slabware.com", "art.slabware.com"})
        finally:
            os.environ.pop("STONESCAN_SUPPLIERS", None)
            importlib.reload(discover)


class DiscoverExpansionTests(unittest.TestCase):
    def test_apex_name(self):
        self.assertEqual(discover._apex_name("https://www.marioandson.com/inventory/"), "Marioandson")
        self.assertEqual(discover._apex_name("http://5280stone.com/inventory/"), "5280Stone")

    def test_slabcloud_company_slug_extraction(self):
        # the verbatim company value is the API slug (incl. any _h_ prefix)
        html = 'x IT_SPA({il:false,columns_override:false,company:"_h_marioandson",filter:{}}) y'
        m = discover._SC_COMPANY.search(html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "_h_marioandson")

    def test_sps_vanity_markers(self):
        # a page carrying any Stone Profits marker fingerprints as SPS
        page = '<script src="https://acme.stoneprofitsweb.com/acme/app.js"></script>'
        self.assertTrue(any(mk in page for mk in discover._SPS_MARKERS))
        self.assertFalse(any(mk in "<html>plain site</html>" for mk in discover._SPS_MARKERS))

    def test_merge_slabcloud_dedupes_host_and_slug(self):
        tmp = tempfile.mkdtemp()
        supfile = os.path.join(tmp, "suppliers.json")
        Path(supfile).write_text(json.dumps({"suppliers": [
            {"host": "owstone.slabcloud.com", "slug": "owstone", "provider": "slabcloud"}]}))
        os.environ["STONESCAN_SUPPLIERS"] = supfile
        import importlib
        importlib.reload(discover)
        try:
            added = discover.merge_slabcloud([
                {"host": "mountaingranite.slabcloud.com", "slug": "mountaingranite", "name": "Mountaingranite", "provider": "slabcloud"},
                {"host": "owstone.slabcloud.com", "slug": "owstone", "name": "x", "provider": "slabcloud"},  # dup host
                {"host": "other.slabcloud.com", "slug": "owstone", "name": "y", "provider": "slabcloud"},    # dup slug
            ])
            self.assertEqual(added, 1)
            entries = {e["host"]: e for e in discover.load_suppliers()}
            self.assertEqual(entries["mountaingranite.slabcloud.com"]["provider"], "slabcloud")
            self.assertNotIn("other.slabcloud.com", entries)  # slug already present
        finally:
            os.environ.pop("STONESCAN_SUPPLIERS", None)
            importlib.reload(discover)


class ProviderParseTests(unittest.TestCase):
    def test_genericfeed_jsonld_and_price(self):
        from stonescan.providers import genericfeed as gf
        html = ('<script type="application/ld+json">'
                '{"@graph":[{"@type":"Product","name":"Basalto Honed 1x1 Basalt Mosaics",'
                '"sku":"MS90264","color":"Gray","material":"Basalt",'
                '"offers":[{"@type":"Offer","priceSpecification":{"0":{"price":"9.19","priceCurrency":"USD"}}}]}]}'
                '</script>')
        prods = gf._products(html)
        self.assertEqual(len(prods), 1)
        self.assertEqual(prods[0]["name"], "Basalto Honed 1x1 Basalt Mosaics")
        self.assertEqual(gf._price(prods[0]["offers"]), "$9.19")
        self.assertEqual(gf._form("Calacatta Gold Slab"), "SLAB")
        self.assertEqual(gf._form("Basalt Mosaics 12x12"), "MOSAIC")

    def test_genericfeed_robots_and_disallow(self):
        """genericfeed's hand-rolled parser was replaced by stonescan.robots; the
        behavior it guaranteed must still hold. (Deeper rule coverage, including
        the Allow precedence this one never handled, lives in tests/test_robots.py.)"""
        from stonescan import robots
        txt = ("User-agent: *\nDisallow: /cart\nDisallow: /*filter_*\n"
               "User-agent: BadBot\nDisallow: /\n"
               "Sitemap: https://x.com/sitemap_index.xml")
        pol = robots.RobotsPolicy.parse(txt)
        self.assertEqual(pol.sitemaps, ["https://x.com/sitemap_index.xml"])
        self.assertFalse(pol.allows("/cart").allowed)
        self.assertFalse(pol.allows("/shop/filter_color").allowed)
        self.assertTrue(pol.allows("/product/calacatta").allowed)
        # BadBot's blanket Disallow is not ours to obey.
        self.assertTrue(pol.allows("/").allowed)

    def test_unbuilt_price(self):
        from stonescan.providers import unbuilt
        self.assertEqual(unbuilt._price(569.72), "$570")
        self.assertEqual(unbuilt._price(None), "")
        self.assertEqual(unbuilt._price(0), "")


class ImageSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = db.init_db(os.path.join(self.tmp, "t.db"))
        _seed_suppliers(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_vec_roundtrip(self):
        import numpy as np
        from stonescan import imagesearch as ims
        v = np.arange(ims.DIM, dtype=np.float32)
        self.assertTrue(np.array_equal(ims._bytes_vec(ims._vec_bytes(v)), v))

    def test_preprocess_shape(self):
        from PIL import Image
        from stonescan import imagesearch as ims
        arr = ims._preprocess(Image.new("RGB", (100, 40), (120, 90, 60)))
        self.assertEqual(arr.shape, (1, 3, 224, 224))
        self.assertEqual(str(arr.dtype), "float32")

    def test_search_ranks_by_cosine(self):
        import numpy as np
        from stonescan import imagesearch as ims
        # three materials, three unit vectors; query is nearest to material B
        vecs = {"http://a": [1, 0, 0], "http://b": [0, 1, 0], "http://c": [0, 0, 1]}
        for i, (url, base) in enumerate(vecs.items(), 1):
            v = np.zeros(ims.DIM, dtype=np.float32); v[:3] = base
            self.conn.execute(
                """INSERT INTO materials (supplier_id, item_id, item_name, name_norm,
                     material_key, material_type, image_url, available_slabs)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (i, f"it{i}", f"Stone {url[-1].upper()}", "X", f"stone {url[-1]}|granite",
                 "Granite", url, 1))
            self.conn.execute("INSERT INTO image_vectors (image_url, vec) VALUES (?,?)",
                              (url, ims._vec_bytes(v)))
        self.conn.commit()
        ims._invalidate_cache()
        q = np.zeros(ims.DIM, dtype=np.float32); q[:3] = [0.1, 0.9, 0.0]
        res = ims.search(self.conn, q, top_k=3)
        self.assertEqual(res[0]["image_url"], "http://b")   # nearest wins
        self.assertGreater(res[0]["score"], res[-1]["score"])

    @unittest.skipUnless(__import__("stonescan.imagesearch", fromlist=["available"]).available(),
                         "CLIP model not present")
    def test_embed_is_unit_512(self):
        import numpy as np
        from PIL import Image
        from stonescan import imagesearch as ims
        v = ims.embed_image(Image.new("RGB", (64, 64), (200, 180, 150)))
        self.assertEqual(v.shape, (ims.DIM,))
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, places=3)


class CompareTests(unittest.TestCase):
    """The compare tray (persistence, the 4-item cap, idempotence) and /compare column
    assembly: availability aggregates, observed-price bucketing, alias-follow, gone
    columns, and winner-mark computation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=6)

    def tearDown(self):
        from stonescan.web import app
        app._search_cache.clear()
        self.conn.close()

    def _mat(self, sid, key, *, slabs=0, price="", form="SLAB", uom="",
             length=0, width=0, color="", name=None):
        base = key.rsplit("|", 1)[0]
        mt = key.rsplit("|", 1)[1].title() if "|" in key else "Other"
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key, material_type,
                  color, product_form, uom, available_slabs, avg_length, avg_width, price_range)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, f"{sid}-{base}-{form}-{slabs}", name or base.title(), (name or base).upper(),
             key, mt, color, form, uom, slabs, length, width, price))
        self.conn.commit()

    @staticmethod
    def _now():
        return "2026-07-23T00:00:00+00:00"

    # --- tray persistence -----------------------------------------------------

    def test_cap_refuses_fifth_and_keeps_four(self):
        for k in ["a|marble", "b|marble", "c|marble", "d|marble"]:
            self.assertEqual(db.add_to_compare(self.conn, k, k, "", self._now()), "added")
        self.assertEqual(db.add_to_compare(self.conn, "e|marble", "e", "", self._now()), "full")
        self.assertEqual(db.compare_count(self.conn), 4)
        self.assertEqual([t["material_key"] for t in db.get_compare(self.conn)],
                         ["a|marble", "b|marble", "c|marble", "d|marble"])

    def test_add_idempotent_and_remove_is_noop(self):
        self.assertEqual(db.add_to_compare(self.conn, "a|marble", "A", "", self._now()), "added")
        self.assertEqual(db.add_to_compare(self.conn, "a|marble", "A", "", self._now()), "exists")
        self.assertEqual(db.compare_count(self.conn), 1)
        db.remove_from_compare(self.conn, "not|there")   # absent -> no error, no change
        self.assertEqual(db.compare_count(self.conn), 1)
        db.remove_from_compare(self.conn, "a|marble")
        self.assertEqual(db.compare_count(self.conn), 0)

    def test_missing_table_reads_as_empty(self):
        # connect() does NOT create tables, so compare_tray is absent here.
        raw = db.connect(os.path.join(self.tmp, "raw.db"))
        try:
            self.assertEqual(db.compare_count(raw), 0)
            self.assertEqual(db.get_compare(raw), [])
        finally:
            raw.close()

    # --- column assembly ------------------------------------------------------

    def test_column_availability_and_price_buckets(self):
        from stonescan.web import app
        self._mat(1, "alpha|marble", slabs=10, price="$11.90/sf", length=120, width=70, color="White")
        self._mat(2, "alpha|marble", slabs=25, price="$570")       # whole-slab total, not /sf
        self._mat(3, "alpha|marble", slabs=5, price="Group 5")     # tier code -> dropped
        db.add_to_compare(self.conn, "alpha|marble", "Alpha", "", self._now())
        cols, _ = app._compare_columns(self.conn)
        self.assertEqual(len(cols), 1)
        c = cols[0]
        self.assertFalse(c["gone"])
        self.assertEqual(c["suppliers"], 3)
        self.assertEqual(c["deepest_yard"]["slabs"], 25)
        self.assertTrue(c["deepest_yard"]["supplier"].startswith("s2"))
        self.assertEqual(c["total_slabs"], 40)
        # the two real money shapes land in their own buckets; the tier code in neither
        self.assertAlmostEqual(c["prices"]["per_sqft"]["low"], 11.90)
        self.assertEqual(c["prices"]["per_sqft"]["n"], 1)
        self.assertAlmostEqual(c["prices"]["slab_total"]["low"], 570.0)
        self.assertEqual(c["prices"]["slab_total"]["n"], 1)

    def test_alias_follows_merge_and_gone_column_keeps_snapshot(self):
        from stonescan.web import app
        self._mat(1, "taj mahal|quartzite", slabs=9)
        self._mat(2, "taj mahal|quartzite", slabs=4)
        db.add_alias(self.conn, "taj mahal|granite", "taj mahal|quartzite", "Quartzite")
        # Tray holds the OLD merged-away key plus a genuinely-gone key.
        db.add_to_compare(self.conn, "taj mahal|granite", "Taj Mahal", "", self._now())
        db.add_to_compare(self.conn, "ghost|marble", "Ghost Stone", "http://x/g.jpg", self._now())
        cols, _ = app._compare_columns(self.conn)
        merged, gone = cols[0], cols[1]
        self.assertFalse(merged["gone"])
        self.assertEqual(merged["key"], "taj mahal|quartzite")
        self.assertEqual(merged["suppliers"], 2)
        self.assertTrue(gone["gone"])
        self.assertEqual(gone["name"], "Ghost Stone")
        self.assertEqual(gone["image"], "http://x/g.jpg")

    # --- winner marks ---------------------------------------------------------

    def test_winner_marks_leader_only_never_tie_or_single(self):
        from stonescan.web import app
        clear = [{"suppliers": 3}, {"suppliers": 1}, {"gone": True, "suppliers": 9}]
        self.assertEqual(app._compare_winner(clear, lambda c: c.get("suppliers"), "max"), 0)
        tie = [{"suppliers": 2}, {"suppliers": 2}]
        self.assertIsNone(app._compare_winner(tie, lambda c: c.get("suppliers"), "max"))
        single = [{"v": 5.0}, {"v": None}]
        self.assertIsNone(app._compare_winner(single, lambda c: c.get("v"), "max"))
        # 'min' mode: lowest $/sf wins
        price = [{"v": 20.0}, {"v": 12.0}, {"v": 30.0}]
        self.assertEqual(app._compare_winner(price, lambda c: c.get("v"), "min"), 1)

    def test_winner_map_over_real_columns(self):
        from stonescan.web import app
        self._mat(1, "a|marble", slabs=40)
        self._mat(2, "a|marble", slabs=5)     # a: 2 suppliers, deepest 40, total 45
        self._mat(3, "b|marble", slabs=20)    # b: 1 supplier,  deepest 20, total 20
        db.add_to_compare(self.conn, "a|marble", "A", "", self._now())
        db.add_to_compare(self.conn, "b|marble", "B", "", self._now())
        cols, winners = app._compare_columns(self.conn)
        self.assertEqual(winners["suppliers"], 0)   # a has more suppliers
        self.assertEqual(winners["deepest"], 0)     # a's deepest yard is bigger
        self.assertEqual(winners["total"], 0)       # a has more total slabs
        self.assertIsNone(winners["nearest"])       # no near origin -> no distances


class AlertTests(unittest.TestCase):
    """The catalog-change digest: the new DOWN direction (sharp drops + sold-outs) with
    its thresholds, the up direction unchanged, per-change unread signatures + seen
    tracking, and the no-baseline / missing-table safety nets."""

    PREV = "2026-07-01"
    LATEST = "2026-07-02"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=4)

    def tearDown(self):
        from stonescan.web import app
        app._alert_cache.clear()
        self.conn.close()

    def _hist(self, sid, date, name, thickness, slabs, mtype="Granite"):
        self.conn.execute(
            """INSERT INTO history (snapshot_date, supplier_id, material_key, name_norm,
                 thickness, material_type, color, slabs, image_url)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (date, sid, f"{name}|{mtype.lower()}", name.upper(), thickness, mtype, "", slabs, ""))
        self.conn.commit()

    def _digest(self):
        from stonescan.web import app
        app._alert_cache.clear()
        return app._alert_digest(self.conn)

    def _names(self, bucket):
        return {it["name_norm"] for it in bucket}

    def _seed_two_snapshots(self):
        # Supplier 1 across a previous and a latest snapshot, one listing per behavior.
        self._hist(1, self.PREV, "drop big", "3cm", 40);   self._hist(1, self.LATEST, "drop big", "3cm", 6)    # sharp drop
        self._hist(1, self.PREV, "sold out", "3cm", 8);    self._hist(1, self.LATEST, "sold out", "3cm", 0)    # sold out
        self._hist(1, self.PREV, "tiny drop", "3cm", 6);   self._hist(1, self.LATEST, "tiny drop", "3cm", 3)   # below the 5-slab floor
        self._hist(1, self.PREV, "small dip", "3cm", 40);  self._hist(1, self.LATEST, "small dip", "3cm", 39)  # below 50%
        self._hist(1, self.PREV, "came back", "3cm", 0);   self._hist(1, self.LATEST, "came back", "3cm", 5)   # restock
        self._hist(1, self.LATEST, "brand new", "3cm", 7)                                                       # new listing (absent prev)

    def test_sharp_drop_and_soldout_thresholds(self):
        self._seed_two_snapshots()
        d = self._digest()
        self.assertIn("DROP BIG", self._names(d["dropped"]))
        self.assertIn("SOLD OUT", self._names(d["soldout"]))
        # Below the absolute floor and below 50% are NOT flagged in either down bucket.
        down = self._names(d["dropped"]) | self._names(d["soldout"])
        self.assertNotIn("TINY DROP", down)
        self.assertNotIn("SMALL DIP", down)
        # The drop carries the real before/after numbers.
        big = next(it for it in d["dropped"] if it["name_norm"] == "DROP BIG")
        self.assertEqual((big["prev_slabs"], big["latest_slabs"]), (40, 6))

    def test_up_direction_still_detected(self):
        self._seed_two_snapshots()
        d = self._digest()
        self.assertIn("CAME BACK", self._names(d["restock"]))
        self.assertIn("BRAND NEW", self._names(d["listed"]))

    def test_signature_is_per_change_event(self):
        self._seed_two_snapshots()
        d = self._digest()
        big = next(it for it in d["dropped"] if it["name_norm"] == "DROP BIG")
        self.assertEqual(big["sig"], f"dropped|1|DROP BIG|3cm|{self.LATEST}")
        # A re-drop at a THIRD snapshot is a new event: same product, new date -> new sig.
        third = "2026-07-03"
        self._hist(1, third, "drop big", "3cm", 1)  # 6 -> 1: ≤half and ≥5 fewer = sharp drop
        d2 = self._digest()
        big2 = next(it for it in d2["dropped"] if it["name_norm"] == "DROP BIG")
        self.assertEqual(big2["sig"], f"dropped|1|DROP BIG|3cm|{third}")
        self.assertNotEqual(big["sig"], big2["sig"])

    def test_seen_tracking_and_unread_count(self):
        from stonescan.web import app
        self._seed_two_snapshots()
        app._alert_cache.clear()
        n0 = app._alert_unread_count(self.conn)
        self.assertGreater(n0, 0, "a fresh digest is all unread")
        # Mark exactly the current digest's signatures seen -> unread drops to zero.
        d = app._alert_digest(self.conn)
        sigs = [it["sig"] for k in ("restock", "listed", "dropped", "soldout") for it in d[k]]
        db.mark_alerts_seen(self.conn, sigs)
        self.assertEqual(app._alert_unread_count(self.conn), 0)

    def test_missing_alert_seen_table_is_safe(self):
        raw = db.connect(os.path.join(self.tmp, "raw.db"))  # connect() creates no tables
        try:
            self.assertEqual(db.seen_alert_sigs(raw), set())
        finally:
            raw.close()

    def test_no_baseline_is_empty_and_quiet(self):
        from stonescan.web import app
        self._hist(1, self.LATEST, "only once", "3cm", 5)  # a single snapshot, no prior
        app._alert_cache.clear()
        d = app._alert_digest(self.conn)
        self.assertFalse(d["has_baseline"])
        self.assertEqual([len(d[k]) for k in app._ALERT_KINDS], [0] * len(app._ALERT_KINDS))
        self.assertEqual(app._alert_unread_count(self.conn), 0)

    # --- AIL-24: delisted -----------------------------------------------

    def _seed_bulk(self, sid, date, n, *, prefix="filler"):
        """Enough listings that one disappearance is not a catalog collapse."""
        for i in range(n):
            self._hist(sid, date, f"{prefix} {i}", "3cm", 5)

    def test_a_listing_absent_from_the_latest_snapshot_is_delisted(self):
        # The event the digest was structurally blind to: _ALERT_ROWS_SQL starts
        # FROM agg a JOIN latest, so a row with no latest snapshot never reached
        # _classify_alert. Grepping the project for "delist" returned nothing.
        self._seed_bulk(1, self.PREV, 10)
        self._seed_bulk(1, self.LATEST, 10)
        self._hist(1, self.PREV, "gone for good", "3cm", 12)     # and nothing at LATEST
        d = self._digest()
        self.assertIn("GONE FOR GOOD", self._names(d["delisted"]))
        item = next(it for it in d["delisted"] if it["name_norm"] == "GONE FOR GOOD")
        self.assertEqual(item["prev_slabs"], 12)
        self.assertEqual(item["kind"], "delisted")

    def test_a_sold_out_listing_is_not_delisted(self):
        # The distinction the kind rests on. History stores zero-stock rows (22,881 of them
        # in the live table), so a row at 0 is still listed — absence means removed.
        self._seed_bulk(1, self.PREV, 10)
        self._seed_bulk(1, self.LATEST, 10)
        self._hist(1, self.PREV, "sold out", "3cm", 8)
        self._hist(1, self.LATEST, "sold out", "3cm", 0)
        d = self._digest()
        self.assertIn("SOLD OUT", self._names(d["soldout"]))
        self.assertNotIn("SOLD OUT", self._names(d["delisted"]))

    def test_a_listing_that_returns_under_another_thickness_is_not_delisted(self):
        # AC-6. It was re-listed, not removed, and calling that a delisting is a lie about
        # the catalog. 38 of 459 measured rows are exactly this.
        self._seed_bulk(1, self.PREV, 10)
        self._seed_bulk(1, self.LATEST, 10)
        self._hist(1, self.PREV, "rethick", "2cm", 6)
        self._hist(1, self.LATEST, "rethick", "3cm", 6)
        d = self._digest()
        self.assertNotIn("RETHICK", self._names(d["delisted"]))

    def test_a_listing_with_zero_stock_at_the_previous_snapshot_is_not_delisted(self):
        # It was already unavailable; its removal is bookkeeping, not news.
        self._seed_bulk(1, self.PREV, 10)
        self._seed_bulk(1, self.LATEST, 10)
        self._hist(1, self.PREV, "was empty", "3cm", 0)
        d = self._digest()
        self.assertNotIn("WAS EMPTY", self._names(d["delisted"]))

    def test_delisted_rows_carry_a_material_key_and_no_item_id(self):
        # AC-5. The (SELECT MIN(mm.id) FROM materials …) the other kinds use is NULL by
        # construction once the material is gone, so the row links to /material instead.
        self._seed_bulk(1, self.PREV, 10)
        self._seed_bulk(1, self.LATEST, 10)
        self._hist(1, self.PREV, "gone for good", "3cm", 12)
        item = next(it for it in self._digest()["delisted"]
                    if it["name_norm"] == "GONE FOR GOOD")
        self.assertIsNone(item["id"])
        self.assertEqual(item["material_key"], "gone for good|granite")

    def test_delistings_are_per_supplier_not_across_global_dates(self):
        # AC-4. The 9 global dates in the live table cover 65/83/6/18/3/85/17/115/19
        # suppliers, so a listing that is simply on a different crawl cycle must not read as
        # removed. Supplier 2 here has one older snapshot and has not been crawled since.
        self._seed_bulk(1, self.PREV, 10)
        self._seed_bulk(1, self.LATEST, 10)
        self._hist(2, self.PREV, "other supplier stock", "3cm", 9)
        d = self._digest()
        self.assertNotIn("OTHER SUPPLIER STOCK", self._names(d["delisted"]))

    def test_a_collapsed_catalog_is_suppressed_and_named(self):
        # Measured: 133 of 134 suppliers lose under 2.5% of their listings between their two
        # most recent snapshots. The one exception is americanquartz at 81.2% (848 -> 159),
        # which is a curator-confirmed storefront filter re-scoping a mirror to Quartz only —
        # 566 of 957 apparent delistings from one deliberate act. Unguarded it would fill the
        # 300-row cap and crowd out every real one.
        self._seed_bulk(3, self.PREV, 20)
        self._hist(3, self.LATEST, "filler 0", "3cm", 5)      # 20 -> 1
        d = self._digest()
        self.assertEqual(d["delisted"], [])
        self.assertEqual([s["supplier_name"] for s in d["delist_skipped"]],
                         ["s3.example.com"])
        self.assertEqual((d["delist_skipped"][0]["prev_names"],
                          d["delist_skipped"][0]["now_names"]), (20, 1))

    def test_an_ordinary_amount_of_churn_is_still_reported(self):
        # The other side of the threshold: 0.5 has to be loose enough that a real supplier
        # clearing out a few lines still reports. 2 of 20 gone = 10%.
        self._seed_bulk(1, self.PREV, 20)
        for i in range(2, 20):
            self._hist(1, self.LATEST, f"filler {i}", "3cm", 5)
        d = self._digest()
        self.assertEqual(self._names(d["delisted"]), {"FILLER 0", "FILLER 1"})
        self.assertEqual(d["delist_skipped"], [])

    def test_delisted_changes_take_part_in_unread_tracking(self):
        from stonescan.web import app
        self._seed_bulk(1, self.PREV, 10)
        self._seed_bulk(1, self.LATEST, 10)
        self._hist(1, self.PREV, "gone for good", "3cm", 12)
        d = self._digest()
        item = next(it for it in d["delisted"] if it["name_norm"] == "GONE FOR GOOD")
        self.assertEqual(item["sig"], f"delisted|1|GONE FOR GOOD|3cm|{self.LATEST}")
        db.mark_alerts_seen(self.conn, [it["sig"] for k in app._ALERT_KINDS for it in d[k]])
        app._alert_cache.clear()
        self.assertEqual(app._alert_unread_count(self.conn), 0)


class HistoryRetentionTests(unittest.TestCase):
    """AIL-24 AC-3/AC-7: history grows unbounded with rows no reader can reach — 83,171 of
    211,238 (39.4%) sit past every reader's horizon."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=3)

    def tearDown(self):
        from stonescan.web import app
        app._alert_cache.clear()
        app._stock_cache.clear()
        self.conn.close()

    def _hist(self, sid, date, name, slabs=5):
        self.conn.execute(
            """INSERT INTO history (snapshot_date, supplier_id, material_key, name_norm,
                 thickness, material_type, color, slabs, image_url)
               VALUES (?,?,?,?,'3cm','Granite','',?,'')""",
            (date, sid, f"{name}|granite", name.upper(), slabs))
        self.conn.commit()

    def _dates(self, sid):
        return [r["snapshot_date"] for r in self.conn.execute(
            "SELECT DISTINCT snapshot_date FROM history WHERE supplier_id = ?"
            " ORDER BY snapshot_date", (sid,))]

    def test_it_keeps_each_suppliers_own_newest_snapshots(self):
        for i in range(1, 7):
            self._hist(1, f"2026-07-0{i}", "a")
        self.assertEqual(db.prune_history(self.conn, keep=3), 3)
        self.assertEqual(self._dates(1), ["2026-07-04", "2026-07-05", "2026-07-06"])

    def test_pruning_is_per_supplier_never_per_global_date(self):
        # THE failure mode. The 9 global dates in the live table cover 65/83/6/18/3/85/17/
        # 115/19 suppliers, so "keep the 3 newest dates" would wipe the entire history of a
        # supplier last crawled on an older one — leaving its next digest with no baseline.
        for i in range(1, 7):
            self._hist(1, f"2026-07-0{i}", "a")       # crawled often, recently
        self._hist(2, "2026-07-01", "b")              # crawled twice, long ago
        self._hist(2, "2026-07-02", "b")
        db.prune_history(self.conn, keep=3)
        self.assertEqual(self._dates(2), ["2026-07-01", "2026-07-02"],
                         "an infrequently-crawled supplier lost its only baseline")

    def test_it_never_prunes_below_the_working_set(self):
        # Both readers need latest AND its own previous. keep=1 would leave a supplier with
        # nothing to compare against and silently empty the digest.
        for i in range(1, 6):
            self._hist(1, f"2026-07-0{i}", "a")
        db.prune_history(self.conn, keep=1)
        self.assertEqual(len(self._dates(1)), 2)

    def test_pruning_leaves_both_readers_outputs_unchanged(self):
        # AC-7, asserted on the actual readers rather than on row counts.
        from stonescan.web import app
        for i in range(1, 6):
            for n in ("alpha", "beta"):
                self._hist(1, f"2026-07-0{i}", n, slabs=10 if i < 5 else 2)
        self._hist(1, "2026-07-05", "gamma", slabs=9)      # a new listing at the latest
        app._alert_cache.clear(); app._stock_cache.clear()
        before_digest = {k: len(app._alert_digest(self.conn)[k]) for k in app._ALERT_KINDS}
        app._stock_cache.clear()
        before_stock = [len(x) for x in app._stock_changes(self.conn)[:2]]
        pruned = db.prune_history(self.conn, keep=3)
        self.assertGreater(pruned, 0, "nothing was pruned, so this proves nothing")
        app._alert_cache.clear(); app._stock_cache.clear()
        after_digest = {k: len(app._alert_digest(self.conn)[k]) for k in app._ALERT_KINDS}
        app._stock_cache.clear()
        after_stock = [len(x) for x in app._stock_changes(self.conn)[:2]]
        self.assertEqual(before_digest, after_digest)
        self.assertEqual(before_stock, after_stock)

    def test_the_per_supplier_index_exists_and_is_used(self):
        # AC-2. None of the three pre-existing indexes leads with supplier_id alone, yet
        # every reader works per supplier, so both queries scanned all 211,238 rows.
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='history'")}
        self.assertIn("idx_hist_sup_date", names)
        plan = " ".join(str(r[3]) for r in self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT supplier_id, MAX(snapshot_date) FROM history"
            " GROUP BY supplier_id"))
        self.assertIn("idx_hist_sup_date", plan)


class ThicknessRepairTests(unittest.TestCase):
    """Thicknesses written before normalize_thickness understood units got 'cm' appended
    to a bare millimetre number ('12mm' -> '12cm'). The repair must be deterministic
    (driven by the row's own uom), self-limiting (a re-run can't make '3cm' into '0.3cm'),
    and must leave anything it can't prove alone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=3)

    def tearDown(self):
        self.conn.close()

    def _mat(self, name, thickness, uom, sid=1):
        cur = self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key, material_type,
                  thickness, uom, available_slabs)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (sid, f"{sid}-{name}-{thickness}-{uom}", name, name.upper(),
             f"{name.lower()}|porcelain", "Porcelain", thickness, uom, 1))
        self.conn.commit()
        return cur.lastrowid

    def _thickness(self, rid):
        return self.conn.execute("SELECT thickness FROM materials WHERE id = ?",
                                 (rid,)).fetchone()[0]

    def test_converts_mm_values_case_insensitively(self):
        a = self._mat("Slab A", "30cm", "mm")
        b = self._mat("Slab B", "12cm", "MM")
        self.assertEqual(db.fix_mm_thickness(self.conn), 2)
        self.assertEqual(self._thickness(a), "3cm")
        self.assertEqual(self._thickness(b), "1.2cm")

    def test_rerun_is_a_no_op_never_compounds(self):
        a = self._mat("Slab A", "30cm", "mm")
        self.assertEqual(db.fix_mm_thickness(self.conn), 1)
        self.assertEqual(self._thickness(a), "3cm")
        # The >=10cm guard no longer matches, so a second pass must change nothing.
        self.assertEqual(db.fix_mm_thickness(self.conn), 0)
        self.assertEqual(self._thickness(a), "3cm", "must never become 0.3cm")

    def test_guard_boundaries(self):
        plausible = self._mat("Thin", "9cm", "mm")     # below the guard: already sane
        boundary = self._mat("Ten", "10cm", "mm")      # at the guard: converted
        db.fix_mm_thickness(self.conn)
        self.assertEqual(self._thickness(plausible), "9cm")
        self.assertEqual(self._thickness(boundary), "1cm")

    def test_unprovable_units_are_left_alone(self):
        blank = self._mat("No Uom", "30cm", "")
        other = self._mat("Sf Uom", "30cm", "SF")
        self.assertEqual(db.fix_mm_thickness(self.conn), 0)
        self.assertEqual(self._thickness(blank), "30cm")
        self.assertEqual(self._thickness(other), "30cm")

    def test_implausible_result_is_skipped(self):
        # 300 "mm" -> 30cm, still not a slab: leave it rather than write a wrong number.
        wild = self._mat("Wild", "300cm", "mm")
        self.assertEqual(db.fix_mm_thickness(self.conn), 0)
        self.assertEqual(self._thickness(wild), "300cm")

    def test_init_db_applies_the_repair_automatically(self):
        rid = self._mat("Auto", "20cm", "mm")
        # Re-opening through init_db is what a packaged install does on every launch.
        c2 = db.init_db(self.path)
        c2.close()
        self.assertEqual(self._thickness(rid), "2cm")

    def test_reclassify_repairs_and_recomputes_thickness(self):
        from stonescan.reclassify import reclassify
        broken = self._mat("Broken", "30cm", "mm")
        from_name = self._mat("Named 2cm Slab", "", "")   # thickness readable from the name
        reclassify(self.path)
        self.assertEqual(self._thickness(broken), "3cm")
        self.assertEqual(self._thickness(from_name), "2cm")

    def test_cli_reclassifies_the_database_it_was_given(self):
        """The CLI used to drop its path argument and always rewrite the app's own
        database — so a run aimed at a copy silently mutated live data instead."""
        from unittest.mock import patch
        from stonescan import reclassify as rc
        target = self._mat("Cli Target", "30cm", "mm")
        # Point the default at a DIFFERENT database: if the argument were ignored, the
        # pass would land there (or fail) instead of on the file we asked for.
        other = os.path.join(self.tmp, "untouched.db")
        db.init_db(other).close()
        with patch.object(db, "DEFAULT_DB", other):
            rc.main([self.path])
        self.assertEqual(self._thickness(target), "3cm", "the given database is the one rewritten")
        c = db.connect(other)
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 0,
                             "the default database must be left alone")
        finally:
            c.close()


class ClassificationHygieneTests(unittest.TestCase):
    """Classifier keyword gaps, same-name majority-vote recovery, accessory separation,
    and the colour/key hygiene rules — with the guarantee that nothing already typed
    correctly is lost and nothing unclassifiable is forced into a type."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=6)

    def tearDown(self):
        self.conn.close()

    def _mat(self, name, mtype="Other", cat="", sub="", sid=1):
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key, material_type,
                  category, subcategory, available_slabs)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (sid, f"{sid}-{name}-{mtype}", name, name.upper(),
             nz.material_key(name, mtype), mtype, cat, sub, 1))
        self.conn.commit()

    def _type_of(self, name):
        return self.conn.execute(
            "SELECT material_type FROM materials WHERE item_name = ?", (name,)).fetchone()[0]

    # --- classifier keyword gaps (AC-1) --------------------------------------

    def test_qtz_abbreviation_is_quartz(self):
        self.assertEqual(nz.canonical_type("ENGINEERED SLAB", "CALACATTA",
                                           "SP QTZ FERRARA ORO 128x64x3CM-F"), "Quartz")

    def test_viatera_is_quartz_like_every_other_quartz_brand(self):
        self.assertEqual(nz.canonical_type("LX Viatera", "", "Minuet"), "Quartz")

    def test_calcite_and_serpentine_get_their_own_types(self):
        self.assertEqual(nz.canonical_type("Calcite", "", "AQUAMARINE POLISHED CALCITE SLAB"),
                         "Calcite")
        self.assertEqual(nz.canonical_type("serpentine", "serpentine", "Verde Alpi Serpentine"),
                         "Serpentine")

    def test_ceramic_does_not_steal_from_porcelain(self):
        self.assertEqual(nz.canonical_type("Ceramic", "Ceramic", "Ice White Glossy"), "Ceramic")
        self.assertEqual(nz.canonical_type("Porcelain", "", "Some Porcelain Slab"), "Porcelain")

    def test_catchall_ceramic_category_never_demotes_a_named_brand(self):
        # Suppliers file branded sintered slabs under a generic "Ceramic" category; the
        # name is the better signal, so these must stay Sintered Stone / Mosaic.
        self.assertEqual(nz.canonical_type("Ceramic", "", "DEKTON KERANIUM"), "Sintered Stone")
        self.assertEqual(nz.canonical_type("Ceramic", "", "NEOLITH Calacatta"), "Sintered Stone")
        self.assertEqual(nz.canonical_type("Ceramic", "", "Hexagon Mosaic Blend"), "Mosaic")

    # --- accessories (AC-3) ---------------------------------------------------

    def test_tools_and_kitchen_accessories_are_separated(self):
        for name, cat, sub in [
            ("DIAMAR MADE IN ITALY 3mm DREMEL KIT ELECTROPLATED", "", ""),
            ("IMS MADE IN ITALY TOLL HOLDER CONE 1/2 GAS PARK", "", ""),
            ("MADE IN ITALY LUPATO ROCKER 115 x 105 mm SCRATCHING TOOL", "", ""),
            ("Colander For Q6701", "Soci Stainless", "Kitchen Accessories"),
            ("511 IMPREGNATOR PINT", "OTHER", ""),
        ]:
            self.assertEqual(nz.canonical_type(cat, sub, name), "Accessory / Non-Slab", name)

    def test_real_stones_are_not_swept_into_accessories(self):
        # Guards the over-broad-substring hazard the accessory list already worries about.
        self.assertEqual(nz.canonical_type("", "", "Steel Grey Granite 3cm"), "Granite")
        self.assertEqual(nz.canonical_type("", "", "Silestone Blanco Maple SLAB"), "Quartz")

    # --- majority vote (AC-2, AC-4) -------------------------------------------

    def test_majority_vote_recovers_other_rows(self):
        from stonescan.reclassify import recover_by_majority_vote
        self._mat("Absolute Black", "Granite", sid=1)
        self._mat("Absolute Black", "Granite", sid=2)
        self._mat("Absolute Black", "Other", sid=3)          # the stray
        self.assertEqual(recover_by_majority_vote(self.conn), 1)
        types = {r[0] for r in self.conn.execute(
            "SELECT material_type FROM materials WHERE item_name = 'Absolute Black'")}
        self.assertEqual(types, {"Granite"})

    def test_tie_leaves_the_row_in_other(self):
        from stonescan.reclassify import recover_by_majority_vote
        self._mat("Taj Mahal", "Granite", sid=1)
        self._mat("Taj Mahal", "Quartzite", sid=2)           # 1-1: no majority
        self._mat("Taj Mahal", "Other", sid=3)
        self.assertEqual(recover_by_majority_vote(self.conn), 0)
        self.assertIn("Other", {r[0] for r in self.conn.execute(
            "SELECT material_type FROM materials WHERE item_name = 'Taj Mahal'")})

    def test_row_with_no_typed_siblings_stays_other(self):
        from stonescan.reclassify import recover_by_majority_vote
        self._mat("Mystery Pattern", "Other", sid=1)
        self.assertEqual(recover_by_majority_vote(self.conn), 0)
        self.assertEqual(self._type_of("Mystery Pattern"), "Other")

    def test_majority_vote_recomputes_the_key(self):
        from stonescan.reclassify import recover_by_majority_vote
        self._mat("Blue Bahia", "Granite", sid=1)
        self._mat("Blue Bahia", "Other", sid=2)
        recover_by_majority_vote(self.conn)
        keys = {r[0] for r in self.conn.execute(
            "SELECT material_key FROM materials WHERE item_name = 'Blue Bahia'")}
        self.assertEqual(keys, {"blue bahia|granite"}, "key must follow the recovered type")

    def test_existing_types_are_never_downgraded(self):
        from stonescan.reclassify import recover_by_majority_vote
        self._mat("River White", "Granite", sid=1)
        recover_by_majority_vote(self.conn)
        self.assertEqual(self._type_of("River White"), "Granite")

    # --- colour + key hygiene (AC-5, AC-6) ------------------------------------

    def test_color_keeps_real_lists_and_strips_descriptions(self):
        self.assertEqual(nz.clean_color("Gray, Tan, White, Beige"), "Gray, Tan, White, Beige")
        self.assertEqual(
            nz.clean_color("Beige,Shell Reef Brushed 24 X 48 2 Cm Limestone Tile"), "Beige")
        self.assertEqual(nz.clean_color("Amarillo Santa Cecilia"), "")   # a stone name
        self.assertEqual(nz.clean_color("1054"), "")                     # numeric id
        self.assertEqual(nz.clean_color(""), "")

    def test_key_ignores_finish_and_size_noise(self):
        clean = nz.material_key("Cassablanca", "Quartzite")
        self.assertEqual(nz.material_key("Cassablanca Polished126.5 x 78.5", "Quartzite"), clean)
        self.assertEqual(nz.material_key("Cassablanca Unpolished 3cm", "Quartzite"), clean)
        # The actual defect: the same listing at two sizes split into two materials.
        # (Descriptive words like "slab" stay — only finish/size noise is stripped.)
        self.assertEqual(
            nz.material_key("Baltic Unpolished Porcelain Slab 127.5x63.7x1.2", "Porcelain"),
            nz.material_key("Baltic Polished Porcelain Slab 120x60x1.2", "Porcelain"))


class MirrorDetectionTests(unittest.TestCase):
    """Two supplier hosts serving one tenant's catalog (same token, near-identical item
    set) must be detected and dropped from supplier counts/facets â€” without touching
    either host's rows, and without ever collapsing two genuinely different catalogs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)

    def tearDown(self):
        self.conn.close()

    def _supplier(self, sid, host, token, item_ids):
        """A supplier plus one material per item_id (so item_count == distinct items)."""
        ids = list(item_ids)
        self.conn.execute(
            "INSERT INTO suppliers (id, host, token, item_count) VALUES (?,?,?,?)",
            (sid, host, token, len(ids)))
        for iid in ids:
            self.conn.execute(
                """INSERT INTO materials
                     (supplier_id, item_id, item_name, name_norm, material_key, material_type)
                   VALUES (?,?,?,?,?,?)""",
                (sid, str(iid), f"Stone {iid}", f"STONE {iid}",
                 f"stone {iid}|granite", "Granite"))
        self.conn.commit()

    def _detect(self, entries=None):
        return db.detect_mirrors(self.conn, entries=entries or [])

    # --- detection ---------------------------------------------------------
    def test_identical_catalog_is_flagged_but_rows_are_kept(self):
        self._supplier(1, "klz.example.com", "klz", range(1, 101))
        self._supplier(2, "americanquartz.example.com", "klz", range(1, 101))
        report = self._detect()
        self.assertEqual([m["mirror_host"] for m in report], ["americanquartz.example.com"])
        self.assertEqual(report[0]["canonical_host"], "klz.example.com")   # label == token
        self.assertEqual(report[0]["source"], "computed")
        self.assertAlmostEqual(report[0]["overlap"], 1.0)
        # Excluded from the "N suppliers" stat, but no row is deleted (NG-1).
        self.assertEqual(db.stats(self.conn, use_cache=False)["suppliers"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 200)

    def test_overlap_just_below_threshold_is_not_a_mirror(self):
        # A=1..98, B=1..96 + {200,201}: shared 96, union 100 -> 0.96 < 0.98.
        self._supplier(1, "a.example.com", "tok", range(1, 99))
        self._supplier(2, "b.example.com", "tok", list(range(1, 97)) + [200, 201])
        self.assertEqual(self._detect(), [])

    def test_overlap_at_threshold_is_a_mirror(self):
        # A=1..99, B=1..98 + {100}: shared 98, union 100 -> exactly 0.98 (inclusive).
        self._supplier(1, "a.example.com", "tok", range(1, 100))
        self._supplier(2, "b.example.com", "tok", list(range(1, 99)) + [100])
        report = self._detect()
        self.assertEqual(len(report), 1)
        self.assertAlmostEqual(report[0]["overlap"], 0.98, places=3)

    def test_identical_item_ids_across_different_tokens_are_not_mirrors(self):
        # item_id is unique only per-tenant; two unrelated catalogs both numbering from 1
        # must never be fused just because their integer ids coincide.
        self._supplier(1, "a.example.com", "tokA", range(1, 51))
        self._supplier(2, "b.example.com", "tokB", range(1, 51))
        self.assertEqual(self._detect(), [])

    def test_real_subset_pair_stays_separate(self):
        # The ckfco shape: outlet is a strict 241/554 subset -> 0.435, well below threshold.
        self._supplier(1, "inventory.ckfco.com", "ckfco", range(1, 555))
        self._supplier(2, "outlet.ckfco.com", "ckfco", range(1, 242))
        self.assertEqual(self._detect(), [])

    # --- canonical pick ----------------------------------------------------
    def test_canonical_is_the_host_with_more_items(self):
        self._supplier(1, "small.example.com", "tok", range(1, 100))   # 99, subset
        self._supplier(2, "big.example.com", "tok", range(1, 101))     # 100, superset -> 0.99
        report = self._detect()
        self.assertEqual(report[0]["mirror_host"], "small.example.com")
        self.assertEqual(report[0]["canonical_host"], "big.example.com")

    def test_canonical_tiebreak_prefers_the_token_label(self):
        # Equal item counts: the host whose leftmost DNS label equals the tenant token wins,
        # regardless of insert order (the pick is a total sort, not first-seen).
        self._supplier(2, "americanquartz.example.com", "klz", range(1, 101))
        self._supplier(1, "klz.example.com", "klz", range(1, 101))
        report = self._detect()
        self.assertEqual(report[0]["canonical_host"], "klz.example.com")
        self.assertEqual(report[0]["mirror_host"], "americanquartz.example.com")

    def test_a_pair_that_stops_matching_is_unflagged(self):
        self._supplier(1, "klz.example.com", "klz", range(1, 101))
        self._supplier(2, "aq.example.com", "klz", range(1, 101))
        self.assertEqual(len(self._detect()), 1)
        # aq's catalog diverges; its overlap collapses and it must be un-flagged.
        self.conn.execute("DELETE FROM materials WHERE supplier_id = 2 AND CAST(item_id AS INT) > 20")
        self.conn.execute("UPDATE suppliers SET item_count = 20 WHERE id = 2")
        self.conn.commit()
        self.assertEqual(self._detect(), [])
        self.assertIsNone(self.conn.execute(
            "SELECT mirror_of FROM suppliers WHERE id = 2").fetchone()["mirror_of"])

    # --- curated overrides -------------------------------------------------
    def test_curated_distinct_declaration_unflags_a_computed_mirror(self):
        self._supplier(1, "klz.example.com", "klz", range(1, 101))
        self._supplier(2, "americanquartz.example.com", "klz", range(1, 101))
        report = self._detect([{"host": "americanquartz.example.com",
                                "mirror_of": False, "reason": "confirmed separate yards"}])
        self.assertEqual(report, [])
        self.assertEqual(db.stats(self.conn, use_cache=False)["suppliers"], 2)

    def test_curated_forced_pairing_flags_a_host_that_did_not_compute(self):
        self._supplier(1, "main.example.com", "tok", range(1, 101))
        self._supplier(2, "vanity.example.com", "tok", range(1, 30))   # 0.29 overlap
        report = self._detect([{"host": "vanity.example.com", "mirror_of": "main.example.com"}])
        self.assertEqual(report[0]["mirror_host"], "vanity.example.com")
        self.assertEqual(report[0]["canonical_host"], "main.example.com")
        self.assertEqual(report[0]["source"], "curated")

    def test_mirror_of_false_without_reason_raises(self):
        with self.assertRaises(ValueError):
            db.MirrorOverride.from_entry({"host": "x", "mirror_of": False})
        self._supplier(1, "a.example.com", "tok", range(1, 101))
        self._supplier(2, "b.example.com", "tok", range(1, 101))
        with self.assertRaises(ValueError):
            db.detect_mirrors(self.conn, entries=[{"host": "b.example.com", "mirror_of": False}])

    def test_mirror_override_parsing(self):
        self.assertIsNone(db.MirrorOverride.from_entry({"host": "x"}))          # no key
        self.assertIsNone(db.MirrorOverride.from_entry({}))
        ov = db.MirrorOverride.from_entry({"host": "X.com", "mirror_of": "Y.com"})
        self.assertEqual((ov.host, ov.canonical), ("x.com", "y.com"))
        ov2 = db.MirrorOverride.from_entry({"host": "x", "mirror_of": False, "reason": "r"})
        self.assertIsNone(ov2.canonical)
        with self.assertRaises(ValueError):
            db.MirrorOverride.from_entry({"host": "x", "mirror_of": 5})          # malformed


class RejectionTests(unittest.TestCase):
    """Discovery triage rejections: a `rejected` block in suppliers.json suppresses a host
    from crawling, lapses after 90 days, and is written automatically after 3 empty crawls."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        self.suppliers = os.path.join(self.tmp, "suppliers.json")
        self._orig_file = discover.SUPPLIERS_FILE
        discover.SUPPLIERS_FILE = Path(self.suppliers)

    def tearDown(self):
        discover.SUPPLIERS_FILE = self._orig_file
        self.conn.close()

    def _write_suppliers(self, entries):
        Path(self.suppliers).write_text(json.dumps({"suppliers": entries}, indent=2),
                                        encoding="utf-8")

    def _read_suppliers(self):
        return json.loads(Path(self.suppliers).read_text(encoding="utf-8"))["suppliers"]

    def _supplier_row(self, host, *, empty_streak=0, item_count=0, last_error=""):
        self.conn.execute(
            "INSERT INTO suppliers (host, empty_streak, item_count, last_error) VALUES (?,?,?,?)",
            (host, empty_streak, item_count, last_error))
        self.conn.commit()

    def _streak(self, host):
        return self.conn.execute(
            "SELECT empty_streak FROM suppliers WHERE host=?", (host,)).fetchone()[0]

    # --- parsing / lapse ---------------------------------------------------
    def test_rejection_parsing_and_validation(self):
        self.assertIsNone(discover.Rejection.from_entry({"host": "x"}))
        r = discover.Rejection.from_entry(
            {"host": "x", "rejected": {"reason": "dead", "at": "2026-01-01"}})
        self.assertEqual((r.reason, r.at), ("dead", date(2026, 1, 1)))
        for bad in ({"reason": "d"},                       # no at
                    {"at": "2026-01-01"},                  # no reason
                    {"reason": "d", "at": "not-a-date"}):  # unparseable
            with self.assertRaises(ValueError):
                discover.Rejection.from_entry({"host": "x", "rejected": bad})
        with self.assertRaises(ValueError):
            discover.Rejection.from_entry({"host": "x", "rejected": "dead"})   # not an object

    def test_lapse_window(self):
        today = date(2026, 7, 24)
        self.assertTrue(discover.Rejection("r", today - timedelta(days=89)).is_active(today))
        self.assertFalse(discover.Rejection("r", today - timedelta(days=91)).is_active(today))
        self.assertEqual(
            discover.Rejection("r", today - timedelta(days=89)).days_until_lapse(today), 1)

    def test_filter_rejected_skips_active_keeps_lapsed(self):
        today = date(2026, 7, 24)
        active = {"host": "a.com", "rejected": {"reason": "r", "at": (today - timedelta(days=10)).isoformat()}}
        lapsed = {"host": "b.com", "rejected": {"reason": "r", "at": (today - timedelta(days=200)).isoformat()}}
        plain = {"host": "c.com"}
        keep, skipped = discover.filter_rejected([active, lapsed, plain], today=today)
        self.assertEqual({e["host"] for e in keep}, {"b.com", "c.com"})   # lapsed gets a fresh probe
        self.assertEqual([e["host"] for e, _ in skipped], ["a.com"])

    def test_filter_rejected_raises_on_malformed(self):
        with self.assertRaises(ValueError):
            discover.filter_rejected([{"host": "x", "rejected": {"reason": "d"}}])

    # --- streak ------------------------------------------------------------
    def test_streak_increments_on_empty_resets_on_items(self):
        self._supplier_row("a.com")
        db.record_crawl_streak(self.conn, "a.com", 0, "")
        db.record_crawl_streak(self.conn, "a.com", 0, "403 Forbidden")
        self.assertEqual(self._streak("a.com"), 2)
        db.record_crawl_streak(self.conn, "a.com", 5, "")     # stored items -> reset
        self.assertEqual(self._streak("a.com"), 0)

    def test_robots_block_never_moves_the_streak(self):
        self._supplier_row("a.com", empty_streak=2)
        db.record_crawl_streak(self.conn, "a.com", 0, "robots-blocked: disallowed")
        self.assertEqual(self._streak("a.com"), 2)            # a decision, not a failure (NG-2)

    # --- reconcile ---------------------------------------------------------
    def test_auto_reject_fires_on_third_empty_not_second(self):
        today = date(2026, 7, 24)
        self._write_suppliers([{"host": "a.com", "name": "A"}])
        self._supplier_row("a.com", empty_streak=2, last_error="403 Forbidden")
        rec = discover.reconcile_rejections(self.path, ["a.com"], today=today)
        self.assertEqual(rec["rejected"], [])
        self.assertNotIn("rejected", self._read_suppliers()[0])
        self.conn.execute("UPDATE suppliers SET empty_streak=3 WHERE host='a.com'")
        self.conn.commit()
        rec = discover.reconcile_rejections(self.path, ["a.com"], today=today)
        self.assertEqual(rec["rejected"], ["a.com"])
        block = self._read_suppliers()[0]["rejected"]
        self.assertEqual(block["at"], today.isoformat())
        self.assertIn("403 Forbidden", block["reason"])       # names the observed failure

    def test_reconcile_only_touches_crawled_hosts(self):
        today = date(2026, 7, 24)
        self._write_suppliers([{"host": "a.com"}, {"host": "b.com"}])
        self._supplier_row("a.com", empty_streak=5)
        self._supplier_row("b.com", empty_streak=5)
        discover.reconcile_rejections(self.path, ["a.com"], today=today)   # only a crawled
        hosts = {s["host"]: s for s in self._read_suppliers()}
        self.assertIn("rejected", hosts["a.com"])
        self.assertNotIn("rejected", hosts["b.com"])          # not attempted -> untouched

    def test_reconcile_restores_a_host_that_returned_items(self):
        today = date(2026, 7, 24)
        self._write_suppliers([{"host": "a.com",
                                "rejected": {"reason": "old", "at": "2026-01-01"}}])
        self._supplier_row("a.com", empty_streak=0, item_count=50)   # a good crawl reset the streak
        rec = discover.reconcile_rejections(self.path, ["a.com"], today=today)
        self.assertEqual(rec["restored"], ["a.com"])
        self.assertNotIn("rejected", self._read_suppliers()[0])

    def test_merge_discovered_does_not_readd_a_rejected_host(self):
        self._write_suppliers([{"host": "a.slabware.com", "provider": "slabware",
                                "rejected": {"reason": "dead", "at": "2026-07-24"}}])
        added = discover.merge_discovered({"a.slabware.com": "slabware"})
        self.assertEqual(added, 0)   # stays listed (rejected) -> the "already listed" check blocks it


def _register_fake_provider(testcase, error="403 Forbidden"):
    """Point a 'fake' provider at an in-memory crawl and return the list of hosts it is
    actually asked for. What a test asserts on is usually that list: the point of both
    features below is that some hosts never get asked at all."""
    from stonescan import providers
    from stonescan.providers.base import SupplierData

    attempted: list[str] = []

    async def crawl(entry, **kw):
        attempted.append(entry["host"])
        return SupplierData(host=entry["host"], ok=False, error=error)

    orig = providers.get
    providers.get = lambda name: crawl if name == "fake" else orig(name)
    testcase.addCleanup(setattr, providers, "get", orig)
    return attempted


class _SuppliersFileCase(unittest.TestCase):
    """Shared temp DB + redirected suppliers.json for the two AIL-25 suites."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        self.suppliers = os.path.join(self.tmp, "suppliers.json")
        self._orig_file = discover.SUPPLIERS_FILE
        discover.SUPPLIERS_FILE = Path(self.suppliers)
        self._write_suppliers([])

    def tearDown(self):
        discover.SUPPLIERS_FILE = self._orig_file
        self.conn.close()

    def _write_suppliers(self, entries):
        Path(self.suppliers).write_text(json.dumps({"suppliers": entries}, indent=2),
                                        encoding="utf-8")

    def _read_suppliers(self):
        return json.loads(Path(self.suppliers).read_text(encoding="utf-8"))["suppliers"]

    def _supplier_row(self, host, *, empty_streak=0, last_error=""):
        self.conn.execute(
            "INSERT INTO suppliers (host, empty_streak, last_error) VALUES (?,?,?)",
            (host, empty_streak, last_error))
        self.conn.commit()

    def _row(self, host):
        """Re-read through a fresh connection: run_all writes on its own."""
        conn = db.connect(self.path)
        try:
            return conn.execute(
                "SELECT empty_streak, last_error FROM suppliers WHERE host=?", (host,)).fetchone()
        finally:
            conn.close()


class _DeadStdout:
    """A stdout whose every write raises, like a pipe whose reader has gone."""

    def __init__(self, exc):
        self.exc = exc
        self.attempts = 0

    def write(self, *a, **k):
        self.attempts += 1
        raise self.exc

    def flush(self, *a, **k):
        pass


class TaskCheckTests(unittest.TestCase):
    """AIL-21: the nightly task can exist, be misconfigured, and have failed, with nothing in
    the app any the wiser — the live task drifted to Interactive/Limited/PT72H and sat that
    way for weeks, because seeing it meant running PowerShell by hand."""

    def test_both_sign_conventions_of_the_same_failure_decode_alike(self):
        # schtasks /FO LIST /V prints -2147024629 where Get-ScheduledTaskInfo returns
        # 2147942667. Same 0x8007010B; a table keyed on the signed form would miss one.
        from stonescan import taskcheck as tc
        self.assertEqual(tc.describe_result(2147942667), tc.describe_result(-2147024629))
        self.assertIn("drive", tc.describe_result(2147942667))

    def test_the_guards_own_exit_code_is_decoded(self):
        # 200 is the sentinel install-refresh-task.ps1 uses for "the project drive never
        # showed up" — the live task returned exactly this at 03:00 on 2026-08-05.
        from stonescan import taskcheck as tc
        self.assertIn("drive", tc.describe_result(200))
        self.assertIn("success", tc.describe_result(0))
        self.assertIn("0x", tc.describe_result(0x1234ABCD))   # unknown codes still readable

    def test_non_windows_no_ops_cleanly(self):
        from stonescan import taskcheck as tc
        import sys as _sys
        real = _sys.platform
        try:
            _sys.platform = "darwin"          # build_mac.sh exists
            s = tc.check()
        finally:
            _sys.platform = real
        self.assertFalse(s.supported)
        self.assertFalse(s.installed)
        self.assertTrue(s.reason)

    def test_a_missing_task_is_a_normal_state_not_an_error(self):
        from stonescan import taskcheck as tc
        orig = tc._query_xml
        tc._query_xml = lambda: ""
        try:
            s = tc.check()
        finally:
            tc._query_xml = orig
        self.assertTrue(s.supported)
        self.assertFalse(s.installed)
        self.assertIn("no scheduled task", s.reason)
        self.assertEqual(s.drift, [])

    def test_drift_is_detected_per_setting_in_the_xmls_own_vocabulary(self):
        # The cmdlet's `-RunLevel Highest` is written to XML as "HighestAvailable". Comparing
        # the cmdlet spelling against the XML would report drift on a correct task.
        from stonescan import taskcheck as tc
        self.assertEqual(tc.EXPECTED["RunLevel"], "HighestAvailable")
        xml = ("<Task><Principal><LogonType>InteractiveToken</LogonType>"
               "<RunLevel>LeastPrivilege</RunLevel></Principal><Settings>"
               "</Settings></Task>")
        got = tc._parse_xml(xml)
        self.assertEqual(got["LogonType"], "InteractiveToken")
        self.assertEqual(got["RunLevel"], "LeastPrivilege")
        # An absent ExecutionTimeLimit is itself the signal the task predates the installer.
        self.assertEqual(got["ExecutionTimeLimit"], "(not set)")
        drift = [f"{k} is {got[k]}, expected {v}" for k, v in tc.EXPECTED.items()
                 if k in got and got[k] != v]
        self.assertEqual(len(drift), 3, "each of the three settings drifts independently")

    def test_a_correctly_registered_task_reports_no_drift(self):
        from stonescan import taskcheck as tc
        xml = ("<Task><Principal><LogonType>S4U</LogonType>"
               "<RunLevel>HighestAvailable</RunLevel></Principal>"
               "<Settings><ExecutionTimeLimit>PT6H</ExecutionTimeLimit></Settings></Task>")
        got = tc._parse_xml(xml)
        self.assertEqual([f"{k}" for k, v in tc.EXPECTED.items()
                          if k in got and got[k] != v], [])

    def test_utf16_xml_is_decoded(self):
        # schtasks /XML writes UTF-16; decoding it as UTF-8 yields mojibake no parser accepts.
        from stonescan import taskcheck as tc
        payload = "<Task><Principal><LogonType>S4U</LogonType></Principal></Task>"
        orig = tc._run
        tc._run = lambda args: (0, payload.encode("utf-16"))
        try:
            self.assertIn("<LogonType>S4U</LogonType>", tc._query_xml())
        finally:
            tc._run = orig

    def test_an_omitted_element_is_drift_not_silence(self):
        # Windows OMITS <RunLevel> when it is LeastPrivilege, and omits <ExecutionTimeLimit>
        # on a task predating the current installer — which is exactly the state this issue
        # documents. A drift check that skips absent keys cannot see the drift it exists for.
        from stonescan import taskcheck as tc
        xml = ("<Task><Principal><LogonType>InteractiveToken</LogonType></Principal>"
               "<Settings></Settings></Task>")
        got = tc._parse_xml(xml)
        self.assertEqual(got["RunLevel"], "LeastPrivilege")
        self.assertEqual(got["ExecutionTimeLimit"], "(not set)")
        drift = [k for k, v in tc.EXPECTED.items() if k in got and got[k] != v]
        self.assertEqual(sorted(drift),
                         ["ExecutionTimeLimit", "LogonType", "RunLevel"])

    def test_an_unread_field_is_never_reported_as_never_ran(self):
        # A task that has genuinely never run reports Windows' 11/30/1999 sentinel, so a
        # blank field can only mean the read failed. Saying "never" there is a false claim
        # from the very page built to stop the app overstating its own state.
        from stonescan import taskcheck as tc
        orig = tc._query_list
        tc._query_list = lambda: {}
        try:
            s = tc.check()
        finally:
            tc._query_list = orig
        if s.supported and s.installed:          # only meaningful where a task exists
            self.assertTrue(s.degraded)
            self.assertIn("could not be read", s.reason)
            self.assertEqual(s.last_run, "")

    def test_benign_scheduler_states_are_not_flagged_as_failures(self):
        from stonescan import taskcheck as tc
        for benign in (0, 0x41300, 0x41301, 0x41303, 0x41325):
            self.assertFalse(tc.TaskState(installed=True, last_result=benign).failing,
                             hex(benign))
        for bad in (200, 2147942667, -2147024629, 1):
            self.assertTrue(tc.TaskState(installed=True, last_result=bad).failing, bad)
        # An uninstalled task is not a failure either — it is a normal state.
        self.assertFalse(tc.TaskState(installed=False).failing)

    def test_the_drive_absent_messages_name_the_drive(self):
        from stonescan import taskcheck as tc
        for code in (200, 2147942667):
            msg = tc.describe_result(code)
            self.assertNotIn("{drive}", msg, "the placeholder was left unformatted")
            self.assertTrue(any(c.endswith(":") for c in msg.split()) or "drive" in msg, msg)

    def test_a_stray_byte_cannot_make_a_live_task_vanish(self):
        # schtasks emits console-codepage bytes despite declaring UTF-16, so one accented
        # character in the project path breaks every strict decode. Returning "" there would
        # report a registered task as "not installed" — the worst lie this block can tell.
        from stonescan import taskcheck as tc
        payload = b'<?xml version="1.0"?><Task><Principal><LogonType>S4U</LogonType>' \
                  b'</Principal></Task>\xe9'
        orig = tc._run
        tc._run = lambda args: (0, payload)
        try:
            xml = tc._query_xml()
        finally:
            tc._run = orig
        self.assertIn("<LogonType>S4U</LogonType>", xml)

    def test_the_reinstall_command_is_pasteable_and_says_elevated(self):
        from stonescan import taskcheck as tc
        cmd = tc.reinstall_command()
        self.assertNotIn("<project>", cmd, "a placeholder is not a command")
        self.assertIn("ELEVATED", cmd)
        self.assertIn("install-refresh-task.ps1", cmd)

    def test_it_never_writes_to_the_scheduler(self):
        # NG-1/NG-3: the page must not be able to register, change, run or delete the task.
        from pathlib import Path
        src = Path("stonescan/taskcheck.py").read_text(encoding="utf-8")
        for verb in ("/create", "/change", "/run", "/delete", "/end", "Register-ScheduledTask"):
            self.assertNotIn(verb, src, f"taskcheck must not be able to {verb}")

    def test_a_never_run_task_does_not_report_the_1999_sentinel(self):
        # A registered task that has not fired reports 11/30/1999 as its last run time, so
        # printing the field verbatim gave "last ran 11/30/1999 12:00:00 AM and has not run
        # yet" — true, and nonsense. Seen live after registering the 03/05/07 triggers.
        from stonescan import taskcheck as tc
        s = tc.TaskState(installed=True, last_result=0x00041303,
                         last_run="11/30/1999 12:00:00 AM", next_run="8/6/2026 3:00:00 AM")
        self.assertTrue(s.never_run)
        self.assertFalse(s.failing, "not-yet-run is a benign state, not a failure")

    def test_a_task_that_has_run_still_reports_its_run_time(self):
        from stonescan import taskcheck as tc
        for code in (0, 200, 0x8007010B):
            self.assertFalse(tc.TaskState(installed=True, last_result=code).never_run,
                             f"0x{code & 0xFFFFFFFF:08X} has run")

    def test_an_unreadable_result_is_not_claimed_to_be_never_run(self):
        # The half that matters. "We could not tell" must not become a confident "has not run
        # yet" — asserting more than we know is the exact thing this module exists to stop.
        from stonescan import taskcheck as tc
        self.assertFalse(tc.TaskState(installed=True, last_result=None).never_run)
        self.assertFalse(tc.TaskState(installed=False, last_result=0x00041303).never_run)

    def test_the_health_page_omits_the_sentinel_and_keeps_unknown(self):
        # End to end through the real template, both branches.
        from fastapi.testclient import TestClient
        from stonescan import taskcheck as tc
        from stonescan.web import app as webapp

        def render(state):
            orig = tc.check
            tc.check = lambda: state
            try:
                return TestClient(webapp.app).get("/health").text
            finally:
                tc.check = orig

        never = render(tc.TaskState(installed=True, last_result=0x00041303,
                                    last_result_text="has not run yet",
                                    last_run="11/30/1999 12:00:00 AM",
                                    next_run="8/6/2026 3:00:00 AM"))
        self.assertNotIn("1999", never)
        self.assertIn("has not run yet", never)
        self.assertIn("8/6/2026", never)
        # A task whose run fields could not be read must still say so, not borrow the
        # never-run wording.
        unread = render(tc.TaskState(installed=True, last_result=None,
                                     last_result_text="unknown", last_run="", next_run=""))
        self.assertIn("unknown", unread)

    def test_a_broken_schtasks_cannot_break_the_page(self):
        from stonescan import taskcheck as tc
        orig = tc._run

        def boom(args):
            raise OSError("schtasks is not on PATH")

        tc._run = boom
        try:
            s = tc.check()          # must NOT raise
        finally:
            tc._run = orig
        self.assertFalse(s.installed)


class FinishDerivationTests(unittest.TestCase):
    """AIL-22: finish is empty on 96.3% of rows while 35% of item names state one. Derived on
    reclassify so it reaches the stored catalog without a re-crawl."""

    def test_derives_from_the_name_when_the_column_is_empty(self):
        self.assertEqual(nz.derive_finish("Absolute Black Polished 3CM"), "Polished")
        self.assertEqual(nz.derive_finish("Taj Mahal Leathered"), "Leathered")
        self.assertEqual(nz.derive_finish("Bianco Carrara"), "")   # NG-5: never invents

    def test_the_structured_value_always_wins(self):
        # The supplier's own field beats a word in the marketing name.
        self.assertEqual(nz.derive_finish("Absolute Black Polished", "Honed"), "Honed")

    def test_values_are_canonicalised(self):
        # Live catalog holds Matt 97 and Leather 64 as distinct strings from Matte/Leathered.
        self.assertEqual(nz.derive_finish("", "Matt"), "Matte")
        self.assertEqual(nz.derive_finish("", "Leather"), "Leathered")
        self.assertEqual(nz.derive_finish("Something Leather"), "Leathered")

    def test_two_finishes_collapse_to_one_value_instead_of_their_own(self):
        # "Honed / Matte" is 727 live rows and "Polished and Honed" 15. Left alone each
        # becomes its own facet entry; picking one of the two would invent a fact.
        self.assertEqual(nz.derive_finish("", "Honed / Matte"), nz.MULTIPLE_FINISH)
        self.assertEqual(nz.derive_finish("", "DUAL FINISH (POLISHED/HONED)"),
                         nz.MULTIPLE_FINISH)
        self.assertEqual(nz.derive_finish("Calacatta Polished Honed"), nz.MULTIPLE_FINISH)

    def test_trade_names_are_not_finishes(self):
        # _FINISH_RE strips these when building the key, but antique (485 rows) and velvet
        # (286) are trade names, and a bare "DUAL" names no finish at all.
        for name in ("Antique Beige", "Velvet Grey", "NEW CALEDONIA - DUAL",
                     "VISCOUNT WHITE DUAL"):
            self.assertEqual(nz.derive_finish(name), "", name)

    def test_an_unrecognised_structured_value_is_kept_not_discarded(self):
        # "Textured" 78, "Nature" 43, "Soft" 10 — the supplier's own answer, just not ours.
        self.assertEqual(nz.derive_finish("", "Textured"), "Textured")
        # ...but a value that says nothing becomes empty rather than a facet entry.
        self.assertEqual(nz.derive_finish("", "Unspecified"), "")

    def test_the_crawl_time_row_builders_derive_it_too(self):
        # The feature's lifespan, not a nicety: replace_materials deletes and re-inserts a
        # supplier's rows on every crawl, so a value only reclassify knew how to produce
        # would be gone by the next morning and the facet back to 3.7% coverage.
        from stonescan.providers.base import material_row
        row = material_row(name="Absolute Black Polished 3CM", crawled_at="2026-08-04",
                           item_id="1", source_url="", finish="")
        self.assertEqual(row["finish"], "Polished")
        row = material_row(name="Statuario", crawled_at="2026-08-04", item_id="2",
                           source_url="", finish="Matt")
        self.assertEqual(row["finish"], "Matte", "the crawl path must canonicalise too")

    def test_derivation_is_idempotent(self):
        # reclassify reads the column it also writes. A value that says nothing must fall
        # through to the name on BOTH passes, or pass 1 blanks it and pass 2 derives
        # something different from the same input.
        first = nz.derive_finish("Bianco Polished", "Unspecified")
        self.assertEqual(first, "Polished")
        self.assertEqual(nz.derive_finish("Bianco Polished", first), first)
        for name, col in (("Taj Mahal Leathered", ""), ("X", "Matt"), ("Y", "Honed / Matte")):
            once = nz.derive_finish(name, col)
            self.assertEqual(nz.derive_finish(name, once), once, f"{name!r}/{col!r}")

    def test_a_two_finish_value_using_the_bare_stem_is_still_Multiple(self):
        # "Duo Finish (Polish and Leathered)" is live. Without a bare 'polish' stem only
        # Leathered matched, so the row was filed as leathered-only — the single-finish
        # claim the Multiple rule exists to prevent, and inconsistent with the sibling
        # spelling "Polished & Leathered" which already resolved correctly.
        self.assertEqual(nz.derive_finish("", "Duo Finish (Polish and Leathered)"),
                         nz.MULTIPLE_FINISH)
        self.assertEqual(nz.derive_finish("", "Polished & Leathered"), nz.MULTIPLE_FINISH)

    def test_deriving_a_finish_never_changes_material_key(self):
        # AC-6 / NG-1: _FINISH_RE already strips these words from the key and must keep
        # doing so. If derivation ever fed the key, every merge would silently move.
        for name in ("Absolute Black Polished 3CM", "Taj Mahal Leathered",
                     "Calacatta Honed / Matte", "Statuario Matt", "Bianco Carrara"):
            before = nz.material_key(name, "Granite")
            nz.derive_finish(name)
            self.assertEqual(nz.material_key(name, "Granite"), before, name)
        # And two listings differing only by finish still share one key.
        self.assertEqual(nz.material_key("Absolute Black Polished", "Granite"),
                         nz.material_key("Absolute Black Honed", "Granite"))


class FinishFilterTests(unittest.TestCase):
    """The facet has to actually filter — on every search path, not just the live one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        self.conn.execute("INSERT INTO suppliers (id, host, item_count) VALUES (1,'s.com',9)")
        for i, (name, fin) in enumerate([
                ("Absolute Black", "Polished"), ("Absolute Black", "Honed"),
                ("Taj Mahal", "Leathered"), ("Carrara", "")], start=1):
            self.conn.execute(
                "INSERT INTO materials (id, supplier_id, item_name, name_norm, material_key,"
                " material_type, color, finish, available_slabs) VALUES (?,1,?,?,?,?,?,?,1)",
                (i, name, name.lower(), f"k{i}", "Granite", "Black", fin))
        self.conn.commit()
        db.rebuild_product_rollup(self.conn)

    def tearDown(self):
        self.conn.close()

    def _run(self, **kw):
        from stonescan.web.app import _search
        base = dict(q="", material_type="", color="", thickness="", supplier="",
                    limit=50, offset=0)
        base.update(kw)
        total, rows, _ = _search(self.conn, **base)
        return total, {r["item_name"] for r in rows}

    def _search(self, **kw):
        return self._run(**kw)[1]

    def test_the_finish_filter_narrows_results(self):
        self.assertEqual(self._search(finish="Leathered"), {"Taj Mahal"})
        # Four rows, but only three distinct names — Absolute Black is listed twice, once
        # per finish, which is exactly the case this facet exists to separate.
        self.assertEqual(self._run(finish="")[0], 4)

    def test_it_does_not_silently_no_op_on_the_rollup_fast_path(self):
        # The rollup has no finish column, so if fast_ok did not decline the filter this
        # would come back unfiltered — the exact failure AC-3 names.
        self.assertTrue(self.conn.execute("SELECT 1 FROM product_rollup LIMIT 1").fetchone())
        total, names = self._run(finish="Honed")
        self.assertEqual(names, {"Absolute Black"})
        # Assert on the TOTAL, not the name set: four rows carry only three distinct names,
        # so a set-length guard here could never have failed and would have proved nothing.
        self.assertEqual(total, 1, "the rollup path ignored the finish filter")

    def test_it_composes_with_the_other_filters(self):
        self.assertEqual(self._search(finish="Polished", material_type="Granite"),
                         {"Absolute Black"})
        self.assertEqual(self._search(finish="Polished", material_type="Marble"), set())


class ResidueTests(unittest.TestCase):
    """AIL-19: when a crawl fails, replace_materials never runs, so the old rows survive with
    their original crawled_at while suppliers.last_crawled advances. Six SlabWare hosts have
    served 2026-07-16 material under an advancing attempt stamp — and the item page asserted
    the attempt date as the collection date."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _ago(hours):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
            timespec="seconds")

    def _supplier(self, host, *, attempted, data_at, key="k1", error=""):
        cur = self.conn.execute(
            "INSERT INTO suppliers (host, item_count, last_crawled, last_error) VALUES (?,?,?,?)",
            (host, 1, attempted, error))
        sid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO materials (supplier_id, item_name, material_key, material_type,"
            " crawled_at) VALUES (?,?,?,?,?)", (sid, f"Stone {host}", key, "Granite", data_at))
        self.conn.commit()
        return sid

    def test_data_older_than_its_own_attempt_is_residue(self):
        stale = self._supplier("stale.com", attempted=self._ago(2), data_at=self._ago(24 * 15),
                               error="403 Forbidden")
        fresh = self._supplier("fresh.com", attempted=self._ago(2), data_at=self._ago(2),
                               key="k2")
        got = db.residue_supplier_ids(self.conn)
        self.assertIn(stale, got)
        self.assertNotIn(fresh, got)

    def test_an_errored_crawl_that_still_stored_items_is_not_residue(self):
        # Partial-crawl preservation: a host can log a 403 from a secondary endpoint and
        # still store its items in the same run. `status` calls that "stale"; the data is
        # current, and only the data-vs-attempt comparison can tell the two apart.
        sid = self._supplier("partial.com", attempted=self._ago(2), data_at=self._ago(2),
                             error="403 Forbidden")
        self.assertNotIn(sid, db.residue_supplier_ids(self.conn))
        health = {h["host"]: h for h in db.supplier_health(self.conn)}
        self.assertEqual(health["partial.com"]["status"], "stale")   # unchanged vocabulary
        self.assertFalse(health["partial.com"]["residue"])           # but not residue

    def test_a_global_cutoff_would_condemn_the_catalog_and_the_rule_does_not(self):
        # 91.9% of live rows predate the newest crawl day, because that day was a targeted
        # retry of 41 hosts. Anything keyed on "older than the newest crawl" is unusable.
        for i in range(10):
            self._supplier(f"old{i}.com", attempted=self._ago(24 * 9),
                           data_at=self._ago(24 * 9), key=f"k{i}")
        self._supplier("today.com", attempted=self._ago(1), data_at=self._ago(1), key="kt")
        newest = self.conn.execute("SELECT MAX(crawled_at) t FROM materials").fetchone()["t"]
        would_flag = self.conn.execute(
            "SELECT COUNT(DISTINCT supplier_id) c FROM materials WHERE crawled_at < ?",
            (newest,)).fetchone()["c"]
        self.assertEqual(would_flag, 10, "the global rule really would flag the whole catalog")
        self.assertEqual(db.residue_supplier_ids(self.conn), set(), "per-supplier rule is quiet")

    def test_rollup_row_is_flagged_when_ANY_contributor_is_stale(self):
        # The stated rule (AC-4): any, not all. Both directions covered.
        self._supplier("a.com", attempted=self._ago(2), data_at=self._ago(24 * 15), key="shared")
        self._supplier("b.com", attempted=self._ago(2), data_at=self._ago(2), key="shared")
        self._supplier("c.com", attempted=self._ago(2), data_at=self._ago(2), key="clean")
        rows = [{"id": 1, "material_key": "shared"}, {"id": 3, "material_key": "clean"}]
        got = db.groups_with_residue(self.conn, rows)
        self.assertIn("shared", got, "one stale contributor of two must flag the row")
        self.assertNotIn("clean", got)
        # An all-stale rule would NOT have flagged 'shared' — that is the difference.
        self.assertEqual(db.groups_with_residue(self.conn, []), set())

    def test_a_keyless_row_is_matched_on_its_own_id(self):
        # Products with no material_key group as 'id:<id>' in the rollup.
        sid = self._supplier("k.com", attempted=self._ago(2), data_at=self._ago(24 * 15), key="")
        mid = self.conn.execute(
            "SELECT id FROM materials WHERE supplier_id=?", (sid,)).fetchone()["id"]
        got = db.groups_with_residue(self.conn, [{"id": mid, "material_key": ""}])
        self.assertEqual(got, {f"id:{mid}"})

    def test_the_threshold_is_hours_not_calendar_days(self):
        # The stored stamps are '2026-08-03T23:49:00+00:00' but SQLite's datetime() returns
        # '2026-08-02 23:49:00' — space separator, offset stripped. Comparing the raw column
        # against that is a TEXT comparison diverging at position 10 on 'T' vs ' ', so the
        # rule degenerated to a date-only test: 26.8h went unflagged while a SMALLER 25.0h
        # gap straddling midnight was caught. My first tests only used 360h vs 2h and saw
        # none of it.
        db._residue_cache.clear()
        for host, attempted, data_at in (
                ("gap47.com", "2026-08-03T23:49:00+00:00", "2026-08-02T01:00:00+00:00"),
                ("gap27.com", "2026-08-03T23:49:00+00:00", "2026-08-02T21:00:00+00:00"),
                ("gap25.com", "2026-08-03T00:30:00+00:00", "2026-08-01T23:30:00+00:00")):
            self._supplier(host, attempted=attempted, data_at=data_at, key=host)
        for host in ("under23.com", "under2.com"):
            self._supplier(host, attempted="2026-08-03T23:49:00+00:00",
                           data_at="2026-08-03T00:49:00+00:00" if host == "under23.com"
                           else "2026-08-03T21:49:00+00:00", key=host)
        ids = db.residue_supplier_ids(self.conn, use_cache=False)
        hosts = {r["id"]: r["host"] for r in self.conn.execute("SELECT id, host FROM suppliers")}
        flagged = {hosts[i] for i in ids}
        self.assertEqual(flagged, {"gap47.com", "gap27.com", "gap25.com"},
                         "the 24-48h band is exactly where the format bug hid")

    def test_the_residue_set_is_cached_but_invalidatable(self):
        sid = self._supplier("c.com", attempted=self._ago(2), data_at=self._ago(24 * 15))
        self.assertIn(sid, db.residue_supplier_ids(self.conn))
        self.conn.execute("UPDATE suppliers SET last_crawled = ? WHERE id = ?",
                          (self._ago(24 * 15), sid))
        self.conn.commit()
        self.assertIn(sid, db.residue_supplier_ids(self.conn), "still cached")
        db._residue_cache.clear()
        self.assertNotIn(sid, db.residue_supplier_ids(self.conn), "recomputed after clear")

    def test_a_narrowed_result_set_suppresses_the_chip(self):
        # The row aggregates only the filtered contributors, while the residue lookup sees
        # the whole catalog — so filtering to one healthy supplier must not carry the
        # warning along. Type and colour describe the stone, so they narrow nothing.
        self.assertTrue(db.narrows_contributors(supplier="Blackstone SD"))
        self.assertTrue(db.narrows_contributors(in_stock=1))
        self.assertTrue(db.narrows_contributors(location="Dallas"))
        self.assertTrue(db.narrows_contributors(min_length=100))
        self.assertFalse(db.narrows_contributors(material_type="Granite", color="White"))
        self.assertFalse(db.narrows_contributors())

    def test_discovery_puts_an_items_but_errored_host_outside_live(self):
        # A failed crawl leaves the previous rows in place, so items > 0 says nothing about
        # whether the catalog is reachable today. SlabWare read 9 live when it had 3.
        from stonescan.web.app import discovery_status
        self.assertEqual(discovery_status(probed=True, items=273, error="403 Forbidden"),
                         "broken")
        self.assertEqual(discovery_status(probed=True, items=273, error=""), "live")
        self.assertEqual(discovery_status(probed=True, items=0, error=""), "empty")
        self.assertEqual(discovery_status(probed=False, items=0, error=""), "unprobed")
        # A robots decline still wins over the generic error bucket.
        self.assertEqual(
            discovery_status(probed=True, items=5, error="robots-blocked: disallowed"),
            "blocked")

    def test_no_rows_are_deleted_by_any_of_this(self):
        self._supplier("x.com", attempted=self._ago(2), data_at=self._ago(24 * 15))
        before = self.conn.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"]
        db.residue_supplier_ids(self.conn)
        db.supplier_health(self.conn)
        after = self.conn.execute("SELECT COUNT(*) c FROM materials").fetchone()["c"]
        self.assertEqual(before, after)   # NG-1: a 15-day-old listing is still a lead


class OutputFailureTests(_SuppliersFileCase):
    """AIL-27: on 2026-08-03 a three-hour crawl committed every row, then died on a cosmetic
    print() thirteen lines before reconcile_rejections. It was recorded FAILED despite having
    succeeded, and the 249 rejections it earned went unstamped."""

    def _run_with_dead_stdout(self, exc, entries=()):
        """Run a crawl whose every console write raises, and give back the sink."""
        import asyncio
        from stonescan.ingest import run_all
        sink = _DeadStdout(exc)
        real = sys.stdout
        sys.stdout = sink
        try:
            asyncio.run(run_all(list(entries), db_path=self.path))   # must NOT raise
        finally:
            sys.stdout = real
        return sink

    def _history(self):
        return (Path(self.path).resolve().parent / "refresh-history.log").read_text(
            encoding="utf-8")

    def test_an_oserror_on_write_does_not_end_the_run(self):
        # The exact shape of 8/3: EINVAL, not EPIPE, because on Windows that is how a write
        # to a dead pipe surfaces.
        sink = self._run_with_dead_stdout(OSError(22, "Invalid argument"))
        self.assertGreater(sink.attempts, 0, "the test never actually exercised a write")
        self.assertIn("Done", self._history())

    def test_a_unicode_error_on_write_does_not_end_the_run(self):
        # The same bug in a different coat: a supplier name the console's cp1252 codepage
        # cannot encode. Catching only OSError would leave this one fatal.
        self._run_with_dead_stdout(
            UnicodeEncodeError("charmap", "→", 0, 1, "character maps to <undefined>"))
        self.assertIn("Done", self._history())

    def test_the_run_still_completes_its_tail_and_records_done_not_failed(self):
        # AC-5 + AC-6, the actual regression: the work AFTER the last DB write must happen.
        # reconcile_rejections is the specific casualty from 8/3.
        from stonescan import discover as disc
        called = []
        real = disc.reconcile_rejections

        def spy(*a, **k):
            called.append(True)
            return real(*a, **k)

        disc.reconcile_rejections = spy
        self.addCleanup(setattr, disc, "reconcile_rejections", real)

        self._run_with_dead_stdout(OSError(22, "Invalid argument"))
        self.assertTrue(called, "reconcile_rejections never ran — the tail died again")
        hist = self._history()
        self.assertIn("Done", hist)
        self.assertNotIn("FAILED", hist)

    def test_many_failed_writes_leave_exactly_one_note(self):
        # A crawl prints per supplier; one line per failure would bury the history log.
        for i in range(6):
            self._supplier_row(f"h{i}.example.com")
        attempted = _register_fake_provider(self)
        sink = self._run_with_dead_stdout(
            OSError(22, "Invalid argument"),
            [{"host": f"h{i}.example.com", "provider": "fake"} for i in range(6)])
        self.assertEqual(len(attempted), 6)
        self.assertGreater(sink.attempts, 6, "not enough writes failed to prove the point")
        notes = [ln for ln in self._history().splitlines() if "console output lost" in ln]
        self.assertEqual(len(notes), 1, f"expected exactly one note, got {len(notes)}")
        self.assertIn("OSError", notes[0])

    def test_state_resets_so_a_second_run_can_report_its_own_failure(self):
        # The web Refresh button starts a second crawl in the same process. A stuck flag
        # would leave that run silently mute about its own console dying.
        self._run_with_dead_stdout(OSError(22, "Invalid argument"))
        self._run_with_dead_stdout(OSError(22, "Invalid argument"))
        notes = [ln for ln in self._history().splitlines() if "console output lost" in ln]
        self.assertEqual(len(notes), 2, "the second run did not report its own output loss")

    def test_a_healthy_run_leaves_no_note_and_still_prints(self):
        import asyncio
        import io
        from stonescan.ingest import run_all
        buf = io.StringIO()
        real = sys.stdout
        sys.stdout = buf
        try:
            asyncio.run(run_all([], db_path=self.path))
        finally:
            sys.stdout = real
        self.assertIn("product rollup", buf.getvalue(), "say() stopped printing")
        self.assertNotIn("console output lost", self._history())

    # --- AIL-31: the seven sites AIL-27 did not reach -----------------------

    def test_a_print_inside_init_db_does_not_cost_the_night_its_ledger_row(self):
        # THE reproduction. db.py's _migrate prints when fix_mm_thickness repairs something,
        # and _migrate runs inside init_db, which run_all calls BEFORE it has a ledger row.
        # A raise there is swallowed by _ledger, run_id stays None, _ledger_finish returns
        # early — and the night ends with no refresh_runs row while refresh-history.log says
        # Done. Identical DB, identical code, only stdout differs.
        conn = db.connect(self.path)
        conn.execute("INSERT INTO suppliers (id, host) VALUES (91,'mm.example.com')")
        conn.execute("INSERT INTO materials (supplier_id, item_id, item_name, name_norm,"
                     " uom, thickness) VALUES (91,'i','N','N','MM','12cm')")
        conn.commit()
        conn.close()
        self._run_with_dead_stdout(OSError(22, "Invalid argument"))
        conn = db.connect(self.path)
        try:
            rows = [(r["outcome"]) for r in conn.execute("SELECT outcome FROM refresh_runs")]
        finally:
            conn.close()
        self.assertEqual(rows, ["done"], "the night lost its ledger row to a cosmetic print")
        self.assertIn("Done", self._history())

    def test_a_failure_before_the_run_starts_is_carried_across_not_erased(self):
        # desktop.run_refresh prints two lines before run_all installs the notifier. reset()
        # clearing the flag would erase the only evidence the night had: the run would go
        # mute and record nothing about why.
        from stonescan import output
        output.reset(None)
        self.addCleanup(output.reset, None)      # this module's state is process-global
        self._fail_once()                        # a write fails while no notifier exists
        self.assertTrue(output.failed())
        notes = []
        output.reset(notes.append)               # this is what run_all does
        self.assertEqual(len(notes), 1, "the earlier failure was silently erased")
        self.assertIn("already failing", notes[0])

    def _fail_once(self):
        from stonescan import output
        real = sys.stdout
        sys.stdout = _DeadStdout(OSError(22, "Invalid argument"))
        try:
            output.say("x")
        finally:
            sys.stdout = real

    def test_a_deferred_flush_failure_is_caught_where_it_can_be(self):
        # Buffering makes a write failure arrive LATE. If it lands during interpreter
        # shutdown CPython flushes outside any try block and exits 120 — so a crawl already
        # recorded 'done' gets reported as a failed run by refresh.ps1.
        from stonescan import output

        class LateFail:
            def write(self, *a):
                return 0                          # accepted, buffered, no error yet
            def flush(self):
                raise OSError(22, "Invalid argument")
            def isatty(self):
                return False

        notes = []
        output.reset(None)                        # clear anything a previous test left owed
        output.reset(notes.append)
        self.addCleanup(output.reset, None)
        real = sys.stdout
        sys.stdout = LateFail()
        try:
            output.say("buffered")                # succeeds
            self.assertFalse(output.failed(), "the write should not have failed yet")
            output.flush()                        # must NOT raise
        finally:
            sys.stdout = real
        self.assertTrue(output.failed())
        self.assertEqual(len(notes), 1)

    def test_shutdown_flush_really_does_exit_120(self):
        # The premise behind the test above, measured rather than assumed — if CPython ever
        # stops doing this, the guard is solving a problem that no longer exists.
        import subprocess
        p = subprocess.run([sys.executable, "-c", "import os;os.close(1);print('x')"],
                           capture_output=True)
        self.assertEqual(p.returncode, 120)

    def test_a_dead_stdout_does_not_abort_umis_branch_loop(self):
        # umi.crawl's own `except Exception` wraps the whole body, so a raise from the print
        # in the branch loop does not skip one branch — it fails the ENTIRE crawl and returns
        # ok=False. Three consecutive nights of that reaches AUTO_REJECT_STREAK and
        # discover.reject_by_streak writes "rejected" into suppliers.json, which nothing
        # exempts a hand-seeded supplier from.
        import asyncio
        import json as _json

        import httpx

        from stonescan.providers import umi

        seen = []

        async def fake_get(client, path, params=None):
            b = (params or {}).get("branch")
            seen.append((path, b))
            if path == "IMat.php" and b == "connecticut":
                raise httpx.HTTPError("boom")     # the branch whose failure gets printed
            if path == "IMat.php":
                # A real item from every OTHER branch, so `ok` reflects the loop surviving
                # rather than the fake simply returning nothing everywhere.
                return [{"item": f"code-{b}", "MaterialName": f"Stone {b}",
                         "GroupName": "GRANITE", "lots": 0}]
            return []

        from stonescan import output
        orig_get = umi._get
        umi._get = fake_get
        self.addCleanup(setattr, umi, "_get", orig_get)
        self.addCleanup(output.reset, None)       # process-global; do not leak to the next test
        real = sys.stdout
        sys.stdout = _DeadStdout(OSError(22, "Invalid argument"))
        try:
            out = asyncio.run(umi.crawl({"host": "umistone.com"}, delay=0))
        finally:
            sys.stdout = real
        tried = [b for p, b in seen if p == "IMat.php"]
        self.assertGreater(len(tried), 1,
                           "the branch loop stopped at the first failure — the print raised "
                           "out into crawl()'s own except and killed the whole supplier")
        self.assertTrue(out.ok, "UMI was recorded as a failed crawl because of a print")

    def test_no_crawl_executed_module_contains_a_bare_print(self):
        # The durable half of this issue. AIL-27 converted 58 sites and left 7; nothing in the
        # tree stopped the eighth. Providers are enumerated by GLOB, not by name — six of the
        # seven adapters have no prints today and an eighth must be covered on arrival.
        import ast
        root = Path(__file__).resolve().parent.parent / "stonescan"
        named = ["db.py", "desktop.py", "ingest.py", "crawler.py", "discover.py",
                 "normalize.py", "robots.py", "denylist.py", "slabs.py", "spill.py"]
        files = [root / n for n in named] + sorted(root.glob("providers/*.py"))
        # CLI-only prints are legitimate (NG-1): a human is watching a terminal, and a write
        # failure there is the report dying, not a crawl. Exempt by enclosing function.
        cli_only = {"main", "report", "_report"}
        offenders = []
        for f in files:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in cli_only:
                    continue
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                            and sub.func.id == "print"):
                        offenders.append(f"{f.name}:{sub.lineno} in {node.name}()")
        self.assertEqual(offenders, [], "use output.say() on any path a crawl executes: "
                                        + "; ".join(offenders))


class RefreshLedgerTests(_SuppliersFileCase):
    """AIL-20: refresh-history.log only gets a terminal line if the process reaches the end
    of run_all, so a run killed mid-crawl left nothing at all — 4 of 11 logged runs have a
    start and no outcome, indistinguishable from a night the task never fired."""

    def _run_row(self, run_id):
        conn = db.connect(self.path)
        try:
            return conn.execute("SELECT * FROM refresh_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            conn.close()

    def _states(self):
        conn = db.connect(self.path)
        try:
            return [r["state"] for r in db.recent_refresh_runs(conn)]
        finally:
            conn.close()

    def _insert(self, *, started, heartbeat, finished=None, outcome=None):
        conn = db.connect(self.path)
        conn.execute("INSERT INTO refresh_runs (started_at, heartbeat_at, finished_at, outcome)"
                     " VALUES (?,?,?,?)", (started, heartbeat, finished, outcome))
        conn.commit()
        conn.close()

    @staticmethod
    def _ago(minutes):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
            timespec="seconds")

    def test_a_completed_run_records_an_outcome(self):
        import asyncio
        from stonescan.ingest import run_all
        asyncio.run(run_all([], db_path=self.path))
        rows = self._states()
        self.assertEqual(rows, ["done"])

    def test_a_run_that_stopped_moving_reads_as_interrupted(self):
        # The distinction the whole ledger exists for: no outcome AND no recent heartbeat.
        self._insert(started=self._ago(600), heartbeat=self._ago(400))
        self.assertEqual(self._states(), ["interrupted"])

    def test_a_slow_run_still_moving_is_not_called_interrupted(self):
        # The 07-24 run legitimately took two hours. A long run whose heartbeat is advancing
        # must never be libelled as a crash — misreading it that way is the trap in AC-2.
        self._insert(started=self._ago(180), heartbeat=self._ago(1))
        self.assertEqual(self._states(), ["running"])

    def test_the_slowest_real_supplier_gap_does_not_read_as_interrupted(self):
        # Measured from the 2026-08-03 full run: run_providers is strictly sequential, so the
        # gap between ticks is just the slowest supplier — stonetrash took 35.3 min and
        # umistone 30.1. A cutoff under those turns a healthy run into an INTERRUPTED badge.
        self._insert(started=self._ago(200), heartbeat=self._ago(36))
        self.assertEqual(self._states(), ["running"])
        self.assertGreater(db.INTERRUPTED_AFTER_MIN, 36)

    def test_a_run_superseded_by_a_later_one_is_interrupted_immediately(self):
        # The stronger signal: if a newer run began after this one last moved, this one is
        # over regardless of the clock. Without it, last night's dead run keeps claiming to
        # be "running" until it ages out, even while tonight's is underway.
        # The older run's heartbeat is well inside the staleness window, so the time-based
        # rule alone would still call it "running" — only the supersede rule catches it.
        self._insert(started=self._ago(90), heartbeat=self._ago(30))
        self._insert(started=self._ago(20), heartbeat=self._ago(1))    # began after it stopped
        self.assertLess(30, db.INTERRUPTED_AFTER_MIN, "heartbeat must not be stale on time")
        self.assertEqual(self._states(), ["running", "interrupted"])

    def test_a_tail_failure_is_stamped_failed_not_left_looking_interrupted(self):
        # run_all's tail holds SQLite's single write lock. A helper that raises mid-write used
        # to leave that connection open, so the 'failed' stamp — which opens its own
        # connection — waited out the 10s busy_timeout and was swallowed, and the run showed
        # as interrupted with the error text lost. That is the exact distinction AC-1 promises.
        import asyncio
        import time
        from stonescan import db as dbmod
        from stonescan.ingest import run_all

        orig = dbmod.rebuild_product_rollup

        def raise_after_a_write(conn):
            conn.execute("DELETE FROM product_rollup")      # uncommitted: holds the lock
            raise sqlite3.OperationalError("database or disk is full")

        dbmod.rebuild_product_rollup = raise_after_a_write
        self.addCleanup(setattr, dbmod, "rebuild_product_rollup", orig)

        t0 = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            asyncio.run(run_all([], db_path=self.path))
        elapsed = time.monotonic() - t0

        conn = db.connect(self.path)
        row = conn.execute("SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row["outcome"], "failed")
        self.assertIn("disk is full", row["detail"])
        self.assertIsNotNone(row["finished_at"])
        # It must not have sat on the busy timeout to get there.
        self.assertLess(elapsed, 5, "the failed stamp blocked on the write lock")

    def test_the_heartbeat_ticks_once_per_supplier_but_not_for_skipped_hosts(self):
        # A host the circuit breaker abandons was never asked, so it is not an attempt.
        import asyncio
        from stonescan import ingest
        from stonescan.ingest import run_all

        hosts = [f"h{i}.example.com" for i in range(10)]
        for h in hosts:
            self._supplier_row(h)
        _register_fake_provider(self)
        orig = ingest.PROVIDER_ERROR_LIMIT
        ingest.PROVIDER_ERROR_LIMIT = 3
        self.addCleanup(setattr, ingest, "PROVIDER_ERROR_LIMIT", orig)

        asyncio.run(run_all([{"host": h, "provider": "fake"} for h in hosts],
                            db_path=self.path))
        conn = db.connect(self.path)
        row = conn.execute("SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row["planned"], 10)
        self.assertEqual(row["attempts"], 3, "breaker-skipped hosts were counted as attempts")
        self.assertEqual(row["outcome"], "done")

    def test_a_ledger_failure_cannot_fail_the_crawl(self):
        import asyncio
        from stonescan import db as dbmod
        from stonescan.ingest import run_all

        def boom(*a, **k):
            raise sqlite3.OperationalError("ledger is on fire")

        for name in ("start_refresh_run", "heartbeat_refresh_run", "finish_refresh_run"):
            orig = getattr(dbmod, name)
            setattr(dbmod, name, boom)
            self.addCleanup(setattr, dbmod, name, orig)
        asyncio.run(run_all([], db_path=self.path))          # must NOT raise
        log = Path(self.path).resolve().parent / "refresh-history.log"
        self.assertIn("Done", log.read_text(encoding="utf-8"))

    def test_recent_runs_degrades_on_a_db_predating_the_table(self):
        # /health has no try/except; a snapshot DB without the table must not 500 the page.
        conn = db.connect(self.path)
        conn.execute("DROP TABLE IF EXISTS refresh_runs")
        conn.commit()
        self.assertEqual(db.recent_refresh_runs(conn), [])
        conn.close()


class DimensionUnitTests(unittest.TestCase):
    """AIL-23: avg_length/avg_width are stored unitless and read as inches by every consumer.
    Two of 130 suppliers publish metres and centimetres, which made `carrara venatino jumbo`
    roll up to 541,288 ft2 and inverted size filtering in both directions."""

    METRES = "graniteslabsuk.stoneprofitsweb.com"
    CM = "stoneyarduk.stoneprofitsweb.com"

    def setUp(self):
        import shutil
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        self.addCleanup(self.conn.close)

    def _mat(self, host, length, width, *, uom="", name="Slab"):
        sid = db.upsert_supplier(self.conn, host)
        self.conn.execute(
            "INSERT INTO materials (supplier_id, item_id, item_name, name_norm, avg_length,"
            " avg_width, uom, crawled_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, f"i{length}-{width}", name, name.upper(), length, width, uom,
             "2026-08-01T00:00:00"))
        self.conn.commit()
        return sid

    def _dims(self, table="materials", cols=("avg_length", "avg_width")):
        return [tuple(r) for r in self.conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY id")]

    # --- the rule -----------------------------------------------------------

    def test_metres_and_centimetres_both_become_inches(self):
        from stonescan.normalize import dimension_to_inches as f
        self.assertAlmostEqual(f(3.13, self.METRES), 123.2, places=1)   # 3.13m
        self.assertAlmostEqual(f(282.84, self.CM), 111.4, places=1)     # 282.84cm

    def test_the_uom_label_is_not_consulted(self):
        # The finding that shapes the whole rule. graniteslabsuk labels its METRES "MM", and
        # 7 of stoneyarduk's centimetre rows say "MM" too. Trusting the label would divide
        # 3.13 by 25.4 and store a 0.12-inch slab.
        from stonescan.normalize import dimension_to_inches as f
        self.assertAlmostEqual(f(3.13, self.METRES), 123.2, places=1)
        self.assertAlmostEqual(f(274.97, self.CM), 108.3, places=1)   # a "MM"-labelled cm row

    def test_the_decision_is_per_row_not_per_supplier(self):
        # AC-2. One supplier, two scales, one pass: a per-supplier factor gets one wrong
        # whichever it picks.
        from stonescan.normalize import fix_dimension_rows
        rows = [{"avg_length": 282.84, "avg_width": 160.5},    # centimetres
                {"avg_length": 111.4, "avg_width": 63.2}]      # already inches
        fix_dimension_rows(rows, self.CM)
        self.assertAlmostEqual(rows[0]["avg_length"], 111.4, places=1)
        self.assertEqual(rows[1]["avg_length"], 111.4)         # untouched

    def test_other_suppliers_are_never_touched(self):
        # NG-3, and it is not a formality. Magnitude alone is unsafe in general: 210
        # stonetrash rows, 75 klz rows and 51 sii rows sit between 1 and 20 because they are
        # TILES measured in inches, and a global rule would multiply them by 39.
        from stonescan.normalize import dimension_to_inches as f
        for host in ("stonetrash.com", "klz.stoneprofitsweb.com", "sii.stoneprofitsweb.com"):
            self.assertEqual(f(12.0, host), 12.0)
            self.assertEqual(f(145.0, host), 145.0)

    def test_values_already_in_inches_are_left_alone(self):
        # The band between the two guards. This is what makes the pass idempotent, so it is
        # asserted directly rather than only through the second-run test.
        from stonescan.normalize import dimension_to_inches as f
        for v in (20.0, 63.0, 111.4, 123.2, 144.9):
            self.assertEqual(f(v, self.CM), v)
            self.assertEqual(f(v, self.METRES), v)

    def test_zero_and_junk_survive_unchanged(self):
        from stonescan.normalize import dimension_to_inches as f
        for v in (0, 0.0, None, "", "n/a"):
            self.assertEqual(f(v, self.METRES), v)

    # --- the stored repair --------------------------------------------------

    def test_reclassify_repairs_stored_rows_without_a_recrawl(self):
        # NG-2: 88k rows were collected before the rule existed, and re-crawling two UK
        # suppliers costs as much as everything else does.
        from stonescan.reclassify import repair_dimensions
        self._mat(self.METRES, 3.13, 1.74, uom="MM")
        self._mat(self.CM, 282.84, 160.52, uom="CM")
        self.assertEqual(repair_dimensions(self.conn), 2)
        got = self._dims()
        self.assertAlmostEqual(got[0][0], 123.2, places=1)
        self.assertAlmostEqual(got[1][0], 111.4, places=1)

    def test_running_the_repair_twice_changes_nothing(self):
        # AC-5, and the one that would be catastrophic to get wrong: reclassify runs after
        # every classifier change, so a pass that divided again each time would walk the
        # catalog down to nothing over a few weeks.
        from stonescan.reclassify import repair_dimensions
        self._mat(self.METRES, 3.13, 1.74)
        self._mat(self.CM, 282.84, 160.52)
        repair_dimensions(self.conn)
        after_one = self._dims()
        self.assertEqual(repair_dimensions(self.conn), 0)
        self.assertEqual(self._dims(), after_one)

    def test_slab_rows_are_repaired_alongside_materials(self):
        # AC-4. slabs.length/width feed the per-slab size shown on an item page, so leaving
        # them behind would have the material page and the slab list disagree.
        from stonescan.reclassify import repair_dimensions
        sid = self._mat(self.CM, 282.84, 160.52)
        self.conn.execute(
            "INSERT INTO slabs (supplier_id, item_id, slab_no, length, width, crawled_at)"
            " VALUES (?,?,?,?,?,?)", (sid, "i1", "1", 311.8, 168.38, "2026-08-01T00:00:00"))
        self.conn.commit()
        repair_dimensions(self.conn)
        (ln, wd), = self._dims("slabs", ("length", "width"))
        self.assertAlmostEqual(ln, 122.8, places=1)
        self.assertAlmostEqual(wd, 66.3, places=1)

    def test_the_square_footage_becomes_plausible(self):
        # AC-6. _SQFT divides L x W by 144, so a centimetre pair inflates area by 6.45x per
        # slab — which is how one key reached 541,288 ft2 and put 13 unit artifacts in the
        # top 100 by total_sqft.
        from stonescan.reclassify import repair_dimensions
        self._mat(self.CM, 282.84, 160.52)
        before = self._dims()[0]
        repair_dimensions(self.conn)
        after = self._dims()[0]
        self.assertGreater(before[0] * before[1] / 144, 300)    # absurd for one slab
        self.assertLess(after[0] * after[1] / 144, 60)          # ~49 sq ft: a real slab

    def test_size_filtering_stops_being_inverted(self):
        # Both directions of the reported bug in one assertion: none of graniteslabsuk's rows
        # passed min_length >= 100 and almost all of stoneyarduk's falsely did.
        from stonescan.reclassify import repair_dimensions
        self._mat(self.METRES, 3.13, 1.74)     # a real 123in slab, failing the filter
        self._mat(self.CM, 163.64, 100.0)      # a real 64in slab, passing it falsely
        repair_dimensions(self.conn)
        passing = [r["avg_length"] for r in self.conn.execute(
            "SELECT avg_length FROM materials WHERE avg_length >= 100 ORDER BY id")]
        self.assertEqual(len(passing), 1)
        self.assertAlmostEqual(passing[0], 123.2, places=1)

    # --- unbuilt's thickness ------------------------------------------------

    def test_an_inch_thickness_is_read_as_inches_not_centimetres(self):
        # AC-3. Unbuilt sends inch thicknesses beside uom="EA"; thickness_unit() rightly
        # refuses to read a selling unit as a length, so all 1,320 Cosentino rows fell back
        # to centimetres.
        from stonescan.providers.base import material_row
        r = material_row(name="Dekton Sirius", crawled_at="x", thickness="0.79",
                         uom="EA", thickness_uom="in")
        self.assertEqual(r["thickness"], "2cm")
        self.assertEqual(r["uom"], "EA", "uom must not be rewritten - it gates tile_sf")

    def test_every_dekton_thickness_lands_on_its_millimetre_spec(self):
        # 0.16/0.31/0.47/0.79/1.18 in are 4/8/12/20/30 mm rounded to 2dp. Converting at 2dp
        # gives 0.41/0.79/1.19/2.01/3cm, so the 476 rows that are plainly 2cm sit under
        # "2.01cm" and miss the 2cm filter entirely.
        from stonescan.normalize import normalize_thickness as nt
        for inches, expect in (("0.16", "0.4cm"), ("0.31", "0.8cm"), ("0.47", "1.2cm"),
                               ("0.79", "2cm"), ("1.18", "3cm")):
            self.assertEqual(nt(inches, "Dekton", "in"), expect, f"{inches}in")

    def test_metric_thicknesses_keep_their_second_decimal(self):
        # The 1dp rounding is scoped to inch input on purpose — a millimetre or centimetre
        # value is already exact and must not be blurred.
        from stonescan.normalize import normalize_thickness as nt
        self.assertEqual(nt("12.7", "X", "mm"), "1.27cm")
        self.assertEqual(nt("1.27", "X", "cm"), "1.27cm")

    def test_a_row_without_thickness_uom_behaves_exactly_as_before(self):
        from stonescan.providers.base import material_row
        a = material_row(name="Slab 3cm", crawled_at="x", thickness="30", uom="mm")
        self.assertEqual(a["thickness"], "3cm")

    def test_stored_inch_thicknesses_are_repaired_without_a_recrawl(self):
        # NG-2 again: thickness_uom fixes the next crawl, but the 1,320 rows already stored
        # would sit outside the 2cm filter until then.
        from stonescan.reclassify import repair_thickness_units
        sid = db.upsert_supplier(self.conn, "unbuilt.co")
        for i, t in enumerate(("0.16cm", "0.31cm", "0.47cm", "0.63cm", "0.79cm", "1.18cm")):
            self.conn.execute(
                "INSERT INTO materials (supplier_id, item_id, item_name, name_norm, thickness,"
                " uom) VALUES (?,?,?,?,?,'EA')", (sid, f"u{i}", "Dekton", "DEKTON", t))
        self.conn.commit()
        self.assertEqual(repair_thickness_units(self.conn), 6)
        got = [r["thickness"] for r in self.conn.execute(
            "SELECT thickness FROM materials ORDER BY id")]
        self.assertEqual(got, ["0.4cm", "0.8cm", "1.2cm", "1.6cm", "2cm", "3cm"])
        # And the point of it: those rows now answer the 2cm filter.
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM materials WHERE thickness = '2cm'").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_repairing_thickness_twice_changes_nothing(self):
        # AC-5. Magnitude cannot guard this one — 0.79cm and 0.8cm are both plausible stone —
        # so idempotency rests on every repaired value being nominal, and nominal values
        # never firing the rule.
        from stonescan.reclassify import repair_thickness_units
        sid = db.upsert_supplier(self.conn, "unbuilt.co")
        for i, t in enumerate(("0.79cm", "2cm", "3cm")):
            self.conn.execute(
                "INSERT INTO materials (supplier_id, item_id, item_name, name_norm, thickness)"
                " VALUES (?,?,?,?,?)", (sid, f"u{i}", "D", "D", t))
        self.conn.commit()
        self.assertEqual(repair_thickness_units(self.conn), 1)
        self.assertEqual(repair_thickness_units(self.conn), 0)
        got = [r["thickness"] for r in self.conn.execute(
            "SELECT thickness FROM materials ORDER BY id")]
        self.assertEqual(got, ["2cm", "2cm", "3cm"])

    def test_other_suppliers_odd_thicknesses_are_untouched(self):
        # 539 rows in the catalog read 0.79cm and only 476 are unbuilt's; the rest are other
        # suppliers' genuine 7.9mm product. Host scoping is what keeps them out of this.
        from stonescan.normalize import repair_inch_thickness as f
        for host in ("stonetrash.com", "klz.stoneprofitsweb.com"):
            for t in ("0.79cm", "0.16cm", "1.27cm"):
                self.assertEqual(f(t, host), t)

    def test_a_value_that_is_nominal_neither_way_is_left_alone(self):
        # 1.27cm (a real 12.7mm) reads as 3.2cm in inches, which is not a nominal thickness
        # either — so the rule declines rather than guessing.
        from stonescan.normalize import repair_inch_thickness as f
        self.assertEqual(f("1.27cm", "unbuilt.co"), "1.27cm")


class SpillMergeTests(unittest.TestCase):
    """AIL-29: this is the only thing besides a crawl that writes into the primary catalog,
    and unlike a crawl it writes data it did not collect. Every test here is about a way it
    could damage the thing it is supposed to rescue."""

    def setUp(self):
        import shutil
        from stonescan import spill
        self.spill = spill
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.primary = os.path.join(self.tmp, "primary.db")
        self.spilldb = os.path.join(self.tmp, "spill.db")
        db.init_db(self.primary).close()
        db.init_db(self.spilldb).close()

    # --- fixtures -----------------------------------------------------------

    def _stock(self, path, host, *, when, names=("Alpha",), slabs=3, qty=5.0):
        """Give a database one supplier's worth of crawl output, stamped `when`."""
        conn = db.connect(path)
        sid = db.upsert_supplier(conn, host, last_crawled=when, company=host.split(".")[0])
        db.replace_materials(conn, sid, [
            {"supplier_id": sid, "item_id": f"i{i}", "item_name": n, "name_norm": n.lower(),
             "material_key": f"{n.lower()}|granite", "material_type": "Granite",
             "available_slabs": slabs, "crawled_at": when, "thickness": "3cm"}
            for i, n in enumerate(names)])
        db.replace_slabs(conn, sid, [
            {"supplier_id": sid, "item_id": "i0", "slab_no": "1", "location": "Yard",
             "qty": qty, "crawled_at": when}], when)
        db.snapshot_history(conn, sid, when[:10])
        conn.commit()
        conn.close()
        return sid

    def _names(self, path, host):
        conn = db.connect(path)
        try:
            return sorted(r["item_name"] for r in conn.execute(
                "SELECT m.item_name FROM materials m JOIN suppliers s ON s.id = m.supplier_id"
                " WHERE s.host = ?", (host,)))
        finally:
            conn.close()

    def _merge(self, **kw):
        return self.spill.merge_spill(self.primary, self.spilldb, **kw)

    # --- newest wins, per supplier, strictly --------------------------------

    def test_a_newer_spill_supplier_is_merged(self):
        self._stock(self.primary, "a.example.com", when="2026-08-01T03:00:00", names=("Old",))
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00",
                    names=("Fresh", "Newer"))
        r = self._merge()
        self.assertEqual(r.status, "merged")
        self.assertEqual(r.moved, ["a.example.com"])
        self.assertEqual(self._names(self.primary, "a.example.com"), ["Fresh", "Newer"])

    def test_a_stale_spill_supplier_is_left_alone(self):
        # AC-3. The drive comes back at unpredictable times, so a normal crawl can easily
        # have already refreshed a supplier by the time anyone merges last night's spill.
        self._stock(self.primary, "a.example.com", when="2026-08-05T03:00:00", names=("New",))
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00", names=("Old",))
        r = self._merge()
        self.assertEqual(r.moved, [])
        self.assertEqual(r.skipped, ["a.example.com"])
        self.assertEqual(self._names(self.primary, "a.example.com"), ["New"])

    def test_both_directions_inside_one_merge(self):
        # AC-3 as written: a spill newer for A and older for B must move A ONLY. A merge that
        # decided per-spill rather than per-supplier would get one of these wrong whichever
        # way it chose.
        self._stock(self.primary, "a.example.com", when="2026-08-01T03:00:00", names=("A-old",))
        self._stock(self.primary, "b.example.com", when="2026-08-05T03:00:00", names=("B-new",))
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00", names=("A-new",))
        self._stock(self.spilldb, "b.example.com", when="2026-08-02T03:00:00", names=("B-old",))
        r = self._merge()
        self.assertEqual(r.moved, ["a.example.com"])
        self.assertEqual(r.skipped, ["b.example.com"])
        self.assertEqual(self._names(self.primary, "a.example.com"), ["A-new"])
        self.assertEqual(self._names(self.primary, "b.example.com"), ["B-new"])

    def test_equal_timestamps_move_nothing(self):
        # AC-2. "Newest wins" with >= instead of > would rewrite the catalog with identical
        # data every night, reassigning every materials.id for no reason.
        same = "2026-08-04T03:00:00"
        self._stock(self.primary, "a.example.com", when=same, names=("Mine",))
        self._stock(self.spilldb, "a.example.com", when=same, names=("Theirs",))
        r = self._merge()
        self.assertEqual(r.moved, [])
        self.assertEqual(self._names(self.primary, "a.example.com"), ["Mine"])

    def test_merging_twice_is_a_no_op(self):
        # AC-9, and it falls out of AC-2 rather than being enforced separately: the merge
        # copies crawled_at, so the second pass sees equal stamps.
        self._stock(self.primary, "a.example.com", when="2026-08-01T03:00:00", names=("Old",))
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00", names=("New",))
        self.assertEqual(self._merge().moved, ["a.example.com"])
        second = self._merge()
        self.assertEqual(second.moved, [])
        self.assertEqual(self._names(self.primary, "a.example.com"), ["New"])

    def test_a_supplier_with_no_materials_cannot_empty_the_primary(self):
        # The sharpest edge in the whole feature. A spill whose crawl failed for a host still
        # has a suppliers row; replaying it would DELETE the primary's materials for that host
        # and insert nothing — a merge that silently deletes a working catalog.
        self._stock(self.primary, "a.example.com", when="2026-08-01T03:00:00", names=("Keep",))
        conn = db.connect(self.spilldb)
        db.upsert_supplier(conn, "a.example.com", last_error="Cloudflare 403",
                           last_crawled="2026-08-09T03:00:00")
        conn.close()
        r = self._merge()
        self.assertEqual(r.moved, [])
        self.assertEqual(self._names(self.primary, "a.example.com"), ["Keep"])

    def test_a_supplier_only_the_spill_has_is_added(self):
        self._stock(self.spilldb, "new.example.com", when="2026-08-04T03:00:00", names=("N",))
        r = self._merge()
        self.assertEqual(r.moved, ["new.example.com"])
        self.assertEqual(self._names(self.primary, "new.example.com"), ["N"])

    # --- replay, not copy ---------------------------------------------------

    def test_rows_are_replayed_under_the_primarys_own_supplier_id(self):
        # AC-4. materials.id is reassigned every crawl and the app keys off
        # (supplier_id, item_id), so a merge that carried the spill's ids across would point
        # every watchlist and sourcing-list entry at the wrong supplier.
        self._stock(self.primary, "z.example.com", when="2026-08-01T03:00:00")   # takes id 1
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00")   # also id 1
        self._merge()
        conn = db.connect(self.primary)
        try:
            rows = conn.execute(
                "SELECT s.host host, COUNT(*) n FROM materials m"
                " JOIN suppliers s ON s.id = m.supplier_id GROUP BY s.host").fetchall()
            got = {r["host"]: r["n"] for r in rows}
            orphans = conn.execute(
                "SELECT COUNT(*) c FROM materials m LEFT JOIN suppliers s"
                " ON s.id = m.supplier_id WHERE s.id IS NULL").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(got, {"z.example.com": 1, "a.example.com": 1})
        self.assertEqual(orphans, 0)

    def test_slabs_and_history_come_across_with_their_own_dates(self):
        # AC-5's positive half. History keeps the spill's snapshot_date rather than being
        # restamped today: the rows describe the night the drive was missing, and relabelling
        # them would tell the alert digest that last night's stock is tonight's.
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00", qty=9.0)
        self._merge()
        conn = db.connect(self.primary)
        try:
            self.assertEqual(conn.execute("SELECT qty FROM slabs").fetchone()["qty"], 9.0)
            dates = [r["snapshot_date"] for r in
                     conn.execute("SELECT DISTINCT snapshot_date FROM history")]
        finally:
            conn.close()
        self.assertEqual(dates, ["2026-08-04"])

    # --- what must not move -------------------------------------------------

    def test_user_owned_data_on_the_primary_survives_untouched(self):
        # AC-5. The primary holds everything the user made; the spill is a packaged app's
        # empty defaults. A merge that treated the two symmetrically would wipe the lot.
        self._stock(self.primary, "a.example.com", when="2026-08-01T03:00:00", names=("Old",))
        conn = db.connect(self.primary)
        db.add_watch(conn, "blue marble", "2026-08-01")
        lid = db.create_list(conn, "Kitchen job", "2026-08-01")
        db.add_alias(conn, "old|granite", "new|granite", "Granite")
        db.add_rejection(conn, "sig-1")
        db.add_to_compare(conn, "new|granite", "New", "", "")
        conn.commit()
        before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("watchlist", "lists", "material_aliases", "merge_rejections",
                            "compare_tray")}
        conn.close()
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00", names=("New",))
        self.assertEqual(self._merge().moved, ["a.example.com"])
        conn = db.connect(self.primary)
        try:
            after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                     for t in before}
            watch = [r["query"] for r in conn.execute("SELECT query FROM watchlist")]
            lname = conn.execute("SELECT name FROM lists WHERE id = ?", (lid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(before, after)
        self.assertEqual(watch, ["blue marble"])
        self.assertEqual(lname["name"], "Kitchen job")

    def test_confirmed_merges_are_re_applied_to_merged_rows(self):
        # AC-8. Replayed rows arrive with the material_key the crawl derived, which is exactly
        # what a curator merge exists to override. Without the fold, one spill merge silently
        # undoes every decision on /quality — the trap run_all already documents.
        conn = db.connect(self.primary)
        db.add_alias(conn, "alpha|granite", "canonical|granite", "Granite")
        conn.commit()
        conn.close()
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00", names=("Alpha",))
        self.assertEqual(self._merge().moved, ["a.example.com"])
        conn = db.connect(self.primary)
        try:
            keys = [r["material_key"] for r in
                    conn.execute("SELECT material_key FROM materials")]
            rollup = conn.execute("SELECT COUNT(*) c FROM product_rollup").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(keys, ["canonical|granite"])
        self.assertGreater(rollup, 0, "the search fast path was not rebuilt after the merge")

    # --- all of it or none of it -------------------------------------------

    def test_a_failure_part_way_leaves_the_primary_unchanged(self):
        # AC-7. A merge that banked supplier A and died on B would leave the catalog in a
        # state nobody chose and no record of which half is which.
        self._stock(self.primary, "a.example.com", when="2026-08-01T03:00:00", names=("A-old",))
        self._stock(self.primary, "b.example.com", when="2026-08-01T03:00:00", names=("B-old",))
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00", names=("A-new",))
        self._stock(self.spilldb, "b.example.com", when="2026-08-04T03:00:00", names=("B-new",))

        def boom(host):
            if host == "b.example.com":
                raise RuntimeError("disk went away mid-merge")

        r = self._merge(on_supplier=boom)
        self.assertEqual(r.status, "failed")
        self.assertIn("disk went away", r.reason)
        # Not "b is unchanged" — NOTHING is, including the supplier that had already been
        # written when the failure hit.
        self.assertEqual(self._names(self.primary, "a.example.com"), ["A-old"])
        self.assertEqual(self._names(self.primary, "b.example.com"), ["B-old"])
        conn = db.connect(self.primary)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM spill_merges").fetchone()["c"], 0,
                "a rolled-back merge must not leave a record claiming it happened")
        finally:
            conn.close()

    def test_a_failed_merge_does_not_take_the_crawl_with_it(self):
        # It runs at the top of run_all. Raising here would turn a recoverable merge problem
        # into a lost night, which is the thing this whole chain exists to stop.
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00")

        def boom(host):
            raise RuntimeError("nope")

        r = self._merge(on_supplier=boom)      # must NOT raise
        self.assertEqual(r.status, "failed")

    # --- refusals -----------------------------------------------------------

    def test_an_older_spill_schema_is_refused_not_merged(self):
        # AC-10. The local copy is only as current as the last build. A spill missing a column
        # the primary has would replay NULL into it for every row — a spill from before
        # AIL-22 would blank every derived finish. Refusing is the only honest option.
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00")
        conn = db.connect(self.spilldb)
        conn.executescript(
            "ALTER TABLE materials RENAME TO m_old;"
            "CREATE TABLE materials AS SELECT id, supplier_id, item_id, item_name, name_norm,"
            " material_key, material_type, crawled_at FROM m_old;"
            "DROP TABLE m_old;")
        conn.commit()
        conn.close()
        r = self._merge()
        self.assertEqual(r.status, "refused")
        self.assertIn("missing", r.reason)
        self.assertIn("materials", r.reason)

    def test_a_newer_spill_schema_is_also_refused(self):
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00")
        conn = db.connect(self.spilldb)
        conn.execute("ALTER TABLE slabs ADD COLUMN from_the_future TEXT")
        conn.commit()
        conn.close()
        r = self._merge()
        self.assertEqual(r.status, "refused")
        self.assertIn("NEWER", r.reason)

    def test_no_spill_database_is_a_normal_state(self):
        r = self.spill.merge_spill(self.primary, os.path.join(self.tmp, "absent.db"))
        self.assertEqual(r.status, "no-spill")

    def test_the_spill_is_never_the_primary(self):
        # A misconfigured STONESCAN_SPILL pointing at the live catalog would have it replay
        # into itself. Cheap to check, unbounded to debug.
        r = self.spill.merge_spill(self.primary, self.primary)
        self.assertEqual(r.status, "no-spill")

    # --- bookkeeping --------------------------------------------------------

    def test_a_successful_merge_is_recorded_for_health(self):
        # AC-11. A spill crawl runs against a different database on a different drive, so it
        # leaves no refresh_runs row here — without this the night reads as "nothing happened".
        self._stock(self.primary, "b.example.com", when="2026-08-09T03:00:00")
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00")
        self._stock(self.spilldb, "b.example.com", when="2026-08-04T03:00:00")
        self._merge()
        conn = db.connect(self.primary)
        try:
            rows = db.recent_spill_merges(conn)
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["moved"], 1)
        self.assertEqual(rows[0]["skipped"], 1)
        self.assertIn("a.example.com", rows[0]["detail"])

    def test_it_backs_the_primary_up_before_writing_and_only_then(self):
        # AC-6: a merge is at least as dangerous as a crawl, which already backs up first.
        # The second half matters too — a ~25s checkpoint+copy on 364 nights with no spill
        # would be a real cost for nothing.
        bak = Path(self.primary + ".bak")
        self._merge()
        self.assertFalse(bak.exists(), "backed up with nothing to merge")
        self._stock(self.primary, "a.example.com", when="2026-08-01T03:00:00", names=("Old",))
        self._stock(self.spilldb, "a.example.com", when="2026-08-04T03:00:00", names=("New",))
        self._merge()
        self.assertTrue(bak.exists(), "no backup taken before writing")
        conn = db.connect(str(bak))
        try:
            names = [r["item_name"] for r in conn.execute("SELECT item_name FROM materials")]
        finally:
            conn.close()
        self.assertEqual(names, ["Old"], "the backup was taken AFTER the merge, not before")

    def test_the_default_spill_path_matches_where_the_installer_spills_to(self):
        # install-refresh-task.ps1 runs %ProgramData%\StoneScanner\StoneScanner.exe, and
        # desktop.setup_env puts the database in data\ beside the exe. If these two ever
        # disagree the merge silently finds nothing, forever, and says so quietly.
        # tests/__init__.py pins STONESCAN_SPILL for the whole suite so no test merges the
        # machine's real spill; drop it for this one assertion and put it straight back.
        saved = os.environ.pop("STONESCAN_SPILL", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "STONESCAN_SPILL", saved)
        p = self.spill.default_spill_db()
        self.assertEqual(p.name, "stonescan.db")
        self.assertEqual(p.parent.name, "data")
        self.assertEqual(p.parent.parent.name, "StoneScanner")
        install = Path("install-refresh-task.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $env:ProgramData "StoneScanner"', install)
        self.assertIn('Join-Path $LocalCopy "StoneScanner.exe"', install)


_GUARD_BLOCK_RE = __import__("re").compile(r"^\$guard = @\(.*?^\) -join '; '$",
                                           __import__("re").S | __import__("re").M)


class RefreshGuardDecisionTests(unittest.TestCase):
    """AIL-28: the night has now been lost twice (2026-08-04, 2026-08-05) because everything
    runnable lives on a removable drive that was absent at 03:00. The guard's three-way
    decision — run D:, wait for a later trigger, or spill to the local copy — is what stops
    that, and it is a string built inside a PowerShell installer, i.e. exactly the kind of
    code that normally goes untested until the night it matters.

    So these run the REAL guard text, lifted out of install-refresh-task.ps1 and executed.
    Only the five values the installer interpolates are substituted; every branch, comparison
    and log line is the one that ships. A copy of the logic here would pass forever while the
    installer drifted out from under it.
    """

    @classmethod
    def setUpClass(cls):
        if sys.platform != "win32":
            raise unittest.SkipTest("the scheduled-task guard is Windows-only")

    def _guard_block(self) -> str:
        src = Path("install-refresh-task.ps1").read_text(encoding="utf-8")
        m = _GUARD_BLOCK_RE.search(src)
        self.assertIsNotNone(
            m, "could not find the `$guard = @(...) -join '; '` block in the installer — if "
               "it was restructured, fix this extraction rather than pasting a copy here")
        return m.group(0)

    def _stub_ps1(self, path: Path, marker: Path, code: int = 0) -> None:
        """A stand-in for refresh.ps1: records that it ran, and where from."""
        path.write_text(
            f"Set-Content -LiteralPath '{marker.as_posix()}' -Value $PWD.Path\n"
            f"exit {code}\n", encoding="utf-8-sig")

    def _stub_cmd(self, path: Path, marker: Path, code: int = 0) -> None:
        """A stand-in for StoneScanner.exe --refresh. .cmd rather than .ps1 because the guard
        invokes the spill as a native executable and must keep doing so."""
        path.write_text(f"@echo off\r\n>\"{marker}\" echo %CD% %*\r\nexit /b {code}\r\n",
                        encoding="ascii")

    def _run_guard(self, *, project_script: Path, spill_exe: Path, sandbox: Path | None = None,
                   wait_minutes: int = -1):
        """Compose and execute the shipped guard. Returns (exit_code, task_log_text).

        wait_minutes defaults to -1 so the deadline is already past when the loop first
        checks it: the give-up path is reached immediately instead of after 20 real minutes.

        `sandbox` reuses a previous call's %ProgramData%, which is how the once-a-day test
        gives a second run sight of the first run's stamp file.
        """
        import shutil
        import subprocess
        tmp = Path(sandbox) if sandbox else Path(tempfile.mkdtemp())
        if sandbox is None:
            self.addCleanup(shutil.rmtree, tmp, True)
        harness = tmp / "harness.ps1"
        harness.write_text("\n".join([
            f"$script = '{project_script}'",
            f"$spillExe = '{spill_exe}'",
            f"$WaitMinutes = {wait_minutes}",
            "$notReachable = 200",
            self._guard_block(),
            # Redirect the task log into the sandbox. The guard resolves it at RUN time from
            # %ProgramData%, which is the whole point of that design — it must never depend
            # on the project drive being there.
            f"$env:ProgramData = '{tmp}'",
            "Invoke-Expression $guard",
        ]) + "\n", encoding="utf-8-sig")   # BOM: 5.1 reads a BOM-less file as cp1252
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                           capture_output=True, timeout=120)
        log = tmp / "StoneScanner" / "refresh-task.log"
        return p.returncode, (log.read_text(encoding="utf-8") if log.exists() else "")

    # --- the decision table -------------------------------------------------

    def test_drive_present_runs_the_project_script(self):
        # AC-3: the normal path. Nothing about the spill may change what happens when the
        # drive is simply there.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        ran, spilled = tmp / "ran.txt", tmp / "spilled.txt"
        script, exe = tmp / "refresh.ps1", tmp / "StoneScanner.cmd"
        self._stub_ps1(script, ran, code=0)
        self._stub_cmd(exe, spilled, code=0)
        # The spill is now unconditional once the wait expires, so ONLY the drive check can
        # be keeping us off it. AC-7.
        code, log = self._run_guard(project_script=script, spill_exe=exe)
        self.assertEqual(code, 0)
        self.assertTrue(ran.exists(), "the project script did not run")
        self.assertFalse(spilled.exists(), "spilled while the drive was present")
        self.assertNotIn("SPILL", log)
        self.assertIn("refresh.ps1 exited 0", log)

    def test_drive_present_propagates_the_crawls_exit_code(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        script, exe = tmp / "refresh.ps1", tmp / "StoneScanner.cmd"
        self._stub_ps1(script, tmp / "ran.txt", code=7)
        self._stub_cmd(exe, tmp / "spilled.txt")
        code, log = self._run_guard(project_script=script, spill_exe=exe)
        self.assertEqual(code, 7)
        self.assertIn("refresh.ps1 exited 7", log)

    def test_drive_absent_spills_on_the_very_first_trigger(self):
        # The rule REVERSED on 2026-08-05. It used to defer to the last trigger of the day on
        # the theory that D: often reappears by mid-morning — but that trades a whole
        # known-lost night for a crawl that might not come. A spill that later proves
        # unnecessary costs nothing: AIL-29 takes a supplier from the spill only when it is
        # strictly newer, so a real D: crawl afterwards simply supersedes it.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        spilled = tmp / "spilled.txt"
        exe = tmp / "StoneScanner.cmd"
        self._stub_cmd(exe, spilled, code=0)
        code, log = self._run_guard(project_script=tmp / "gone" / "refresh.ps1",
                                    spill_exe=exe)
        self.assertEqual(code, 0)
        self.assertTrue(spilled.exists(), "did not spill on the first trigger")
        self.assertIn("--refresh", spilled.read_text(encoding="utf-8", errors="replace"))
        self.assertIn("SPILL", log)
        self.assertNotIn("GAVE UP", log)

    def test_it_spills_at_most_once_a_day(self):
        # What makes spilling early safe. A crawl takes ~3h, so a 03:20 spill finishes about
        # 06:20 and leaves the 07:00 trigger free to start a SECOND full crawl of ~135 live
        # supplier sites the same night. MultipleInstances=IgnoreNew cannot help — by then
        # the first one has finished. Both runs share one %ProgramData%, as three triggers on
        # one machine do.
        import shutil
        sandbox = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, sandbox, True)
        spilled = sandbox / "spilled.txt"
        exe = sandbox / "StoneScanner.cmd"
        self._stub_cmd(exe, spilled, code=0)
        gone = sandbox / "gone" / "refresh.ps1"

        first_code, first_log = self._run_guard(project_script=gone, spill_exe=exe,
                                                sandbox=sandbox)
        self.assertEqual(first_code, 0)
        self.assertIn("SPILL", first_log)
        spilled.unlink()                      # so a second spill is unmistakable

        second_code, second_log = self._run_guard(project_script=gone, spill_exe=exe,
                                                  sandbox=sandbox)
        self.assertEqual(second_code, 200)
        self.assertFalse(spilled.exists(), "crawled ~135 supplier sites twice in one night")
        self.assertIn("already spilled today", second_log)

    def test_the_stamp_is_written_before_the_crawl_not_after(self):
        # One attempt per day, and it must stay predictable when the spill dies. If the stamp
        # were written on completion, a spill that failed two hours in would start again from
        # the top at the next trigger. The stub exits non-zero to stand in for that.
        import shutil
        sandbox = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, sandbox, True)
        exe = sandbox / "StoneScanner.cmd"
        self._stub_cmd(exe, sandbox / "spilled.txt", code=9)
        gone = sandbox / "gone" / "refresh.ps1"
        code, _ = self._run_guard(project_script=gone, spill_exe=exe, sandbox=sandbox)
        self.assertEqual(code, 9, "the spill's own exit code must propagate")
        stamp = sandbox / "StoneScanner" / "last-spill.txt"
        self.assertTrue(stamp.exists(), "a failed spill left no stamp — it will retry today")
        code2, log2 = self._run_guard(project_script=gone, spill_exe=exe, sandbox=sandbox)
        self.assertEqual(code2, 200)
        self.assertIn("already spilled today", log2)

    def test_a_spill_runs_from_the_local_copys_own_folder(self):
        # AC-6, positively stated: the spill's working directory is the local copy, so the
        # exe's app_dir() resolves to it and desktop.setup_env writes ITS data\ folder —
        # its database, its log, its refresh_runs ledger.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        local = tmp / "local"
        local.mkdir()
        spilled = tmp / "spilled.txt"
        exe = local / "StoneScanner.cmd"
        self._stub_cmd(exe, spilled)
        self._run_guard(project_script=tmp / "gone" / "refresh.ps1", spill_exe=exe)
        self.assertIn(str(local).lower(),
                      spilled.read_text(encoding="utf-8", errors="replace").lower())

    def test_a_spill_writes_nothing_to_the_project_drive(self):
        # AC-6. The give-up path is where a well-meaning "log what happened" line would land
        # on the very drive that isn't there — which is the bug the %ProgramData% log exists
        # to prevent, and it would come straight back if someone added one here.
        import shutil
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        project = tmp / "project"
        project.mkdir()
        for name in ("stonescan.db", "refresh.log", "suppliers.json"):
            (project / name).write_text("before", encoding="utf-8")
        before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
                  for p in project.iterdir()}
        exe = tmp / "StoneScanner.cmd"
        self._stub_cmd(exe, tmp / "spilled.txt")
        # refresh.ps1 is missing from a directory that exists — the drive is "there" as far
        # as the filesystem is concerned, but the guard's own check still fails, so we reach
        # the spill with a writable project directory sitting right next to it.
        self._run_guard(project_script=project / "refresh.ps1", spill_exe=exe)
        after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
                 for p in project.iterdir()}
        self.assertEqual(before, after, "a spill modified the project directory")

    def test_no_local_copy_yet_is_a_clean_give_up(self):
        # A checkout that has never run build_exe.ps1 has nothing to spill into. It must say
        # so and exit 200, not fail in some way that reads as a crawl error.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        code, log = self._run_guard(project_script=tmp / "gone" / "refresh.ps1",
                                    spill_exe=tmp / "nothing-here" / "StoneScanner.exe")
        self.assertEqual(code, 200)
        self.assertIn("no local copy", log)

    # --- what the installer registers ---------------------------------------

    def test_the_installer_registers_a_trigger_per_time_not_a_restart_policy(self):
        # AC-2. -RestartCount 4 was believed to cover a missing drive and does not: on
        # 2026-08-05 the run exited 200 at 03:23:48 and no restart ever fired.
        src = Path("install-refresh-task.ps1").read_text(encoding="utf-8")
        self.assertIn('$At = @("03:00", "05:00", "07:00")', src)
        self.assertIn("$trigger = @(foreach ($t in $At) {", src)
        self.assertIn("New-ScheduledTaskTrigger -Daily -At $when", src)
        self.assertIn("-MultipleInstances IgnoreNew", src)

    def test_the_spill_waits_for_a_gui_subsystem_process(self):
        # The 2026-08-06 defect, and the reason the tests above could not catch it: they stub
        # the spill with a .cmd, which is CONSOLE-subsystem, and `&` does wait for those. The
        # real StoneScanner.exe is built console=False, and `&` returns immediately from a
        # GUI-subsystem process — measured at 0.33s against the real exe while the crawl ran
        # on for two hours, with $LASTEXITCODE empty.
        #
        # pythonw.exe is GUI-subsystem too, so a copy of it is a faithful stand-in. It also
        # takes a script argument, which lets the child prove it was waited for: the guard
        # only sees the marker if it blocked until the child wrote it.
        import shutil
        import subprocess
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if not pyw.exists():
            self.skipTest("pythonw.exe not present in this interpreter's directory")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        marker = tmp / "child-finished.txt"
        work = tmp / "work.py"
        work.write_text(
            "import os, time\n"
            "time.sleep(2)\n"
            f"open(r'{marker}', 'w').write('done')\n"
            "os._exit(7)\n", encoding="utf-8")

        # Drive the two forms directly rather than through the guard, because the guard
        # hard-codes '--refresh' and Python rejects that as an unknown option before it can
        # run anything. What is under test is the LAUNCH FORM, which is what the guard picks.
        def run(form: str):
            if form == "call-operator":
                cmd = f'& "{pyw}" "{work}"; Write-Output ("code=[" + $LASTEXITCODE + "]")'
            else:
                cmd = (f'$p = Start-Process -FilePath "{pyw}" -ArgumentList "{work}" '
                       f'-PassThru -Wait; Write-Output ("code=[" + $p.ExitCode + "]")')
            if marker.exists():
                marker.unlink()
            p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                "-ExecutionPolicy", "Bypass", "-Command", cmd],
                               capture_output=True, text=True, timeout=120)
            return p.stdout.strip(), marker.exists()

        code, finished = run("start-process")
        self.assertEqual(code, "code=[7]",
                         "Start-Process -PassThru -Wait must return the child's real code")
        self.assertTrue(finished, "Start-Process -Wait did not wait for the child")

        # And the shipped guard must use that form. Asserting the launch line directly,
        # because the behavioural half above cannot run through the guard's fixed argument.
        guard = self._guard_block()
        spill_branch = next(ln for ln in guard.splitlines() if "function GiveUp" in ln)
        self.assertIn("Start-Process -FilePath `$spill", spill_branch)
        self.assertIn("-PassThru -Wait", spill_branch)
        self.assertNotIn("& `$spill", spill_branch,
                         "the call operator does not wait for a console=False exe")
        self.assertNotIn("`$LASTEXITCODE", spill_branch,
                         "$LASTEXITCODE is empty for a GUI-subsystem process")
        # The D: path is a different case and must keep using $LASTEXITCODE: refresh.ps1 is
        # a SCRIPT run in-process, where it is set correctly. Scoping this assertion to the
        # spill branch is the point, not an oversight.
        self.assertIn("`$c = `$LASTEXITCODE", guard)

    def test_the_spill_has_no_clock_condition_left(self):
        # The old rule compared the clock against $At[-1]. Nothing may reintroduce a
        # time-of-day gate: the spill fires whenever the wait expires, and the ONLY thing
        # bounding it is the once-a-day stamp.
        guard = self._guard_block()
        self.assertNotIn("lastAt", guard, "a time-of-day gate came back")
        self.assertIn("last-spill.txt", guard)
        self.assertIn("already spilled today", guard)
        src = Path("install-refresh-task.ps1").read_text(encoding="utf-8")
        self.assertNotIn("$lastAt", src)

    def test_the_at_times_are_validated_before_the_task_is_unregistered(self):
        # The installer unregisters and then re-registers, so anything that throws in between
        # leaves NO task at all. A [timespan] the guard cannot parse must be caught here.
        src = Path("install-refresh-task.ps1").read_text(encoding="utf-8")
        validate = src.index("[void][timespan]$t")
        unregister = src.index("Unregister-ScheduledTask")
        self.assertLess(validate, unregister,
                        "-At validation must run BEFORE the task is unregistered")

    def test_registering_the_task_does_not_start_a_crawl(self):
        # `-At "03:00"` dates the trigger to TODAY at 03:00, already past by the time anyone
        # installs, and -StartWhenAvailable exists to run missed triggers — so it fires one
        # immediately. With one trigger that was surprising; with three it means installing
        # after 07:00 launches an unrequested three-hour crawl of ~135 live supplier sites.
        # Run the real trigger-building block and check every boundary is still ahead of us.
        import re
        import shutil
        import subprocess
        src = Path("install-refresh-task.ps1").read_text(encoding="utf-8")
        m = re.search(r"^\$trigger = @\(foreach.*?^\}\)$", src, re.S | re.M)
        self.assertIsNotNone(m, "could not find the trigger-building block")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        harness = tmp / "triggers.ps1"
        harness.write_text("\n".join([
            '$At = @("03:00", "05:00", "07:00")',
            "$now = Get-Date",
            m.group(0),
            "$trigger | ForEach-Object { Write-Output $_.StartBoundary }",
        ]) + "\n", encoding="utf-8-sig")
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                           capture_output=True, timeout=120, text=True)
        from datetime import datetime
        stamps = [s.strip() for s in p.stdout.splitlines() if s.strip()]
        self.assertEqual(len(stamps), 3, f"expected 3 triggers, got {p.stdout!r} {p.stderr!r}")
        now = datetime.now()
        for s in stamps:
            # StartBoundary is local time with no offset when built this way.
            when = datetime.fromisoformat(s.split("+")[0].split("Z")[0])
            self.assertGreater(when, now, f"trigger {s} is in the past and would fire at once")

    def test_the_build_and_the_installer_name_the_same_local_copy(self):
        # AC-1. Two files agreeing by coincidence is a drift waiting to happen: the build
        # would faithfully sync a folder the task never looks at.
        build = Path("build_exe.ps1").read_text(encoding="utf-8")
        install = Path("install-refresh-task.ps1").read_text(encoding="utf-8")
        where = 'Join-Path $env:ProgramData "StoneScanner"'
        self.assertIn(where, build)
        self.assertIn(where, install)

    def test_the_sync_cannot_delete_the_local_copys_data(self):
        # AC-1's sharpest edge. /MIR without /XD data would delete the local database on the
        # next build — including, after AIL-29 exists, a spill crawl that had not been merged
        # back yet. Losing a night to a missing drive is bad; deleting the night's work
        # because someone rebuilt the exe is worse.
        build = Path("build_exe.ps1").read_text(encoding="utf-8")
        self.assertIn("/MIR", build)
        # A FULL PATH, never the bare name. robocopy matches a bare /XD name at every depth,
        # so `/XD data` also skipped _internal\geonamescache\data — 164 MB of offline city
        # coordinates, the only source geocode.py resolves the map from. The copy launched
        # and looked healthy; its map simply had nothing to resolve against. Caught by
        # diffing the two trees after the first real build, which is the only way it shows.
        self.assertIn('/XD (Join-Path $local "data")', build)
        self.assertNotIn("/XD data ", build, "a bare /XD name matches at every depth")
        self.assertIn("$sync -ge 8", build,
                      "robocopy's exit code is a bitmask; 1-7 are success")
        self.assertIn("$global:LASTEXITCODE = 0", build,
                      "robocopy's bitmask must not become the script's own exit code")


class VoteAfterCrawlTests(_SuppliersFileCase):
    """AIL-32: `recover_by_majority_vote` is the project's only cross-row classification, and
    it lived only in the `reclassify` CLI. `replace_materials` deletes a supplier's rows
    outright, so every crawl undid it and the catalog regressed overnight, every night."""

    def _mat(self, sid, name, mtype, item_id="i1"):
        self.conn.execute(
            "INSERT OR IGNORE INTO suppliers (id, host) VALUES (?,?)", (sid, f"s{sid}.example.com"))
        self.conn.execute(
            "INSERT INTO materials (supplier_id, item_id, item_name, name_norm, material_type,"
            " material_key, crawled_at) VALUES (?,?,?,?,?,?,?)",
            (sid, item_id, name, name.upper(), mtype,
             f"{name.lower()}|{mtype.lower()}", "2026-08-01T00:00:00"))
        self.conn.commit()

    def _types(self, name):
        return sorted(r["material_type"] for r in self.conn.execute(
            "SELECT material_type FROM materials WHERE item_name = ?", (name,)))

    def _run(self):
        import asyncio
        from stonescan.ingest import run_all
        asyncio.run(run_all([], db_path=self.path))

    def test_a_crawl_now_recovers_other_rows_by_sibling_vote(self):
        self._mat(1, "Absolute Black", "Granite")
        self._mat(2, "Absolute Black", "Granite")
        self._mat(3, "Absolute Black", "Other")       # the stray a crawl leaves behind
        self._run()
        self.assertEqual(self._types("Absolute Black"), ["Granite"] * 3)

    def test_the_vote_runs_before_aliases_so_its_keys_get_folded(self):
        # AC-2, and it needs the case that actually DISCRIMINATES. On the live catalog both
        # orders converge, so a test that just runs each and compares totals proves nothing.
        # The discriminating case is an alias keyed on the POST-vote key: with the vote first,
        # the row becomes 'absolute black|granite' and the alias folds it. With aliases first,
        # the row is still 'absolute black|other', the alias never matches, and the curator's
        # merge is silently orphaned.
        self._mat(1, "Absolute Black", "Granite")
        self._mat(2, "Absolute Black", "Granite")
        self._mat(3, "Absolute Black", "Other")
        db.add_alias(self.conn, "absolute black|granite", "canonical black|granite", "Granite")
        self.conn.commit()
        self._run()
        keys = sorted(r["material_key"] for r in self.conn.execute(
            "SELECT material_key FROM materials WHERE item_name = 'Absolute Black'"))
        self.assertEqual(keys, ["canonical black|granite"] * 3,
                         "the vote's key was not folded — aliases ran first")

    def test_a_tie_is_still_left_in_other(self):
        # NG-4: the decision rules are untouched. A strict majority only.
        self._mat(1, "Split Stone", "Granite")
        self._mat(2, "Split Stone", "Marble")
        self._mat(3, "Split Stone", "Other")
        self._run()
        self.assertIn("Other", self._types("Split Stone"))

    def test_a_row_with_no_typed_siblings_is_untouched(self):
        self._mat(1, "Lonely Stone", "Other")
        self._run()
        self.assertEqual(self._types("Lonely Stone"), ["Other"])

    def test_an_existing_type_is_never_overwritten(self):
        # The vote only ever promotes OUT of 'Other'. A row that already has a concrete type
        # keeps it even when its siblings disagree.
        self._mat(1, "Taj Mahal", "Quartzite")
        self._mat(2, "Taj Mahal", "Granite")
        self._mat(3, "Taj Mahal", "Granite")
        self._run()
        self.assertEqual(self._types("Taj Mahal"), ["Granite", "Granite", "Quartzite"])

    def test_it_moves_a_row_that_the_vote_types_differently(self):
        # AC-8. The vote is not a faithful restore: on the live catalog 2 of the recovered
        # rows come back under a DIFFERENT type than they held before. That is the vote
        # working as designed — siblings outvote a lone row — but it must be visible in the
        # count the run reports rather than hidden inside "recovered N".
        self._mat(1, "Blue Roma", "Quartzite")
        self._mat(2, "Blue Roma", "Quartzite")
        self._mat(3, "Blue Roma", "Other")
        self._run()
        self.assertEqual(self._types("Blue Roma"), ["Quartzite"] * 3)

    def test_two_consecutive_runs_are_stable(self):
        # AC-7: the crawl reverts, the vote restores, and nothing drifts on a second pass.
        self._mat(1, "Absolute Black", "Granite")
        self._mat(2, "Absolute Black", "Granite")
        self._mat(3, "Absolute Black", "Other")
        self._run()
        first = (self._types("Absolute Black"),
                 sorted(r["material_key"] for r in self.conn.execute(
                     "SELECT material_key FROM materials")))
        self._run()
        second = (self._types("Absolute Black"),
                  sorted(r["material_key"] for r in self.conn.execute(
                      "SELECT material_key FROM materials")))
        self.assertEqual(first, second)

    def test_a_run_that_recovers_nothing_says_nothing(self):
        import asyncio
        import io
        from stonescan.ingest import run_all
        self._mat(1, "Absolute Black", "Granite")
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            asyncio.run(run_all([], db_path=self.path))
        finally:
            sys.stdout = real
        self.assertNotIn("recovered", buf.getvalue())

    def test_ingest_imports_reclassify_so_the_frozen_build_carries_it(self):
        # AC-4. PyInstaller's analysis starts at main.py and follows imports; nothing shipped
        # imported reclassify, so it was the one stonescan module absent from the bundle.
        # An import from ingest is the fix — NOT a hiddenimports entry (NG-2), which would
        # ship a module with no caller and hide the real gap.
        import ast
        tree = ast.parse(Path("stonescan/ingest.py").read_text(encoding="utf-8"))
        top_level = {n
                     for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
                     for n in (a.name for a in node.names)}
        self.assertIn("reclassify", top_level,
                      "reclassify must be imported at ingest MODULE level — a function-local "
                      "import would run fine but PyInstaller's analysis would still miss it")
        spec = Path("stonescan.spec").read_text(encoding="utf-8")
        self.assertNotIn("stonescan.reclassify", spec,
                         "NG-2: the fix is a real import, not a hiddenimports entry")


class LostNightsTests(_SuppliersFileCase):
    """AIL-30: two consecutive nights were lost (2026-08-04, 2026-08-05) and nobody noticed,
    because both the ledger and the task state require someone to go and look at them."""

    TODAY = "2026-08-05"

    def _run(self, day, *, outcome=None, at="03:00:00"):
        """One ledger row on `day`. outcome None = a run that never reported one."""
        stamp = f"{day}T{at}+00:00"
        conn = db.connect(self.path)
        conn.execute("INSERT INTO refresh_runs (started_at, heartbeat_at, finished_at, outcome)"
                     " VALUES (?,?,?,?)",
                     (stamp, stamp, stamp if outcome else None, outcome))
        conn.commit()
        conn.close()

    def _days(self):
        conn = db.connect(self.path)
        try:
            return db.lost_refresh_days(conn, today=self.TODAY)
        finally:
            conn.close()

    def test_two_bad_days_warn_and_one_does_not(self):
        self._run("2026-08-03", outcome="done")
        self._run("2026-08-04", outcome="failed")
        self._run("2026-08-05", outcome="failed")
        self.assertEqual(self._days(), 2)
        self.assertGreaterEqual(2, db.LOST_REFRESH_WARN_DAYS)

    def test_one_bad_day_is_below_the_threshold(self):
        # NG-3: a single missed night is noise. A warning that fires on every hiccup is one
        # nobody reads by the time it matters.
        self._run("2026-08-04", outcome="done")
        self._run("2026-08-05", outcome="failed")
        self.assertLess(self._days(), db.LOST_REFRESH_WARN_DAYS)

    def test_three_failures_in_one_night_is_one_day(self):
        # AC-6. AIL-28 adds 03:00/05:00/07:00 triggers, so a bad night will soon produce three
        # rows as a matter of course; counting rows would treble every failure overnight.
        self._run("2026-08-04", outcome="done")
        self._run("2026-08-05", outcome="failed", at="03:00:00")
        self._run("2026-08-05", outcome="failed", at="05:00:00")
        self._run("2026-08-05", outcome="failed", at="07:00:00")
        self.assertEqual(self._days(), 1)

    def test_a_success_after_failures_the_same_day_clears_it(self):
        self._run("2026-08-02", outcome="done")
        self._run("2026-08-03", outcome="failed")
        self._run("2026-08-04", outcome="failed")
        self._run("2026-08-05", outcome="failed", at="03:00:00")
        self._run("2026-08-05", outcome="done", at="05:00:00")
        self.assertEqual(self._days(), 0)

    def test_an_empty_ledger_does_not_warn(self):
        # AC-5: a fresh install is not a failure, and warning there teaches people to ignore
        # the warning before it has ever meant anything.
        self.assertEqual(self._days(), 0)

    def test_a_gap_with_no_runs_at_all_still_counts(self):
        # THE case this issue exists for. On 2026-08-04 the task never fired, so there is no
        # row whose outcome could be inspected: a "was the last run OK?" query reads the
        # healthy 08-03 row and reports nothing wrong. Only date arithmetic sees the hole.
        self._run("2026-08-03", outcome="done")
        self.assertEqual(self._days(), 2)

    def test_never_a_single_success_counts_from_the_first_run(self):
        # No success to subtract from, so the first run's own day is lost too — inclusive.
        self._run("2026-08-04", outcome="failed")
        self._run("2026-08-05", outcome="failed")
        self.assertEqual(self._days(), 2)

    def test_an_unfinished_run_is_not_a_success(self):
        # Interrupted and still-running rows carry outcome NULL. Neither is evidence that the
        # catalog got refreshed, and treating "a run exists" as "a run worked" is how the
        # ledger would come to reassure us about the exact nights it was written to expose.
        self._run("2026-08-02", outcome="done")
        self._run("2026-08-03")
        self._run("2026-08-04")
        self.assertEqual(self._days(), 3)

    def test_it_degrades_on_a_db_predating_the_table(self):
        # This now renders in base.html, i.e. on EVERY page. A snapshot DB shipped before
        # refresh_runs existed must not take the whole app down.
        conn = db.connect(self.path)
        conn.execute("DROP TABLE IF EXISTS refresh_runs")
        conn.commit()
        self.assertEqual(db.lost_refresh_days(conn), 0)
        conn.close()


class CatalogFreshnessTests(unittest.TestCase):
    """AIL-20 AC-4: 'crawled 2026-07-16 → 2026-07-31' reads as fresh while 114 of 140
    suppliers sit at the old end of that range. A range describes its ends, not its
    distribution."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)

    def tearDown(self):
        self.conn.close()

    def _supplier_with_data(self, host, crawled_at):
        cur = self.conn.execute("INSERT INTO suppliers (host, item_count) VALUES (?,1)", (host,))
        self.conn.execute(
            "INSERT INTO materials (supplier_id, item_name, material_key, material_type,"
            " crawled_at) VALUES (?,?,?,?,?)",
            (cur.lastrowid, f"Stone {host}", f"k{host}", "Granite", crawled_at))
        self.conn.commit()

    @staticmethod
    def _ago(hours):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
            timespec="seconds")

    def test_counts_only_suppliers_actually_refreshed_recently(self):
        self._supplier_with_data("fresh1.com", self._ago(2))
        self._supplier_with_data("fresh2.com", self._ago(20))
        self._supplier_with_data("old1.com", self._ago(50))       # two days back
        self._supplier_with_data("old2.com", self._ago(400))      # weeks back
        s = db.stats(self.conn, use_cache=False)
        self.assertEqual(s["suppliers_with_data"], 4)
        self.assertEqual(s["suppliers_current"], 2)
        self.assertEqual(s["fresh_hours"], db.FRESH_HOURS)
        # The old range endpoints still exist — the count is what makes them honest.
        self.assertTrue(s["last_updated"] > s["oldest_updated"])

    def test_a_uniformly_current_catalog_reports_every_supplier_current(self):
        self._supplier_with_data("a.com", self._ago(1))
        self._supplier_with_data("b.com", self._ago(3))
        s = db.stats(self.conn, use_cache=False)
        self.assertEqual((s["suppliers_current"], s["suppliers_with_data"]), (2, 2))


class StartOfRunRejectionTests(_SuppliersFileCase):
    """AIL-25: a crawl that cannot finish can never reject the hosts that are making it not
    finish, because the tail pass only ever sees hosts this run attempted. The start-of-run
    pass works off the streaks already in the DB instead."""

    def test_stamps_a_qualifying_host_that_was_never_attempted(self):
        today = date(2026, 8, 4)
        self._write_suppliers([{"host": "dead.com"}, {"host": "fine.com"}])
        self._supplier_row("dead.com", empty_streak=3, last_error="403 Forbidden")
        self._supplier_row("fine.com", empty_streak=2)
        # No crawled-hosts argument exists here at all — the tail pass would touch neither.
        self.assertEqual(discover.reject_by_streak(self.path, today=today), ["dead.com"])
        hosts = {s["host"]: s for s in self._read_suppliers()}
        self.assertEqual(hosts["dead.com"]["rejected"]["at"], today.isoformat())
        self.assertIn("403 Forbidden", hosts["dead.com"]["rejected"]["reason"])
        self.assertNotIn("rejected", hosts["fine.com"])      # below threshold, untouched

    def test_never_restores_so_a_hand_triaged_rejection_survives(self):
        # Restore keys off streak == 0, and a curator's rejection on a host that has never
        # been crawled also has streak 0. Sweeping every host would quietly undo it.
        self._write_suppliers([{"host": "manual.com",
                                "rejected": {"reason": "asked us to stop", "at": "2026-08-01"}}])
        self._supplier_row("manual.com", empty_streak=0)
        self.assertEqual(discover.reject_by_streak(self.path, today=date(2026, 8, 4)), [])
        self.assertIn("rejected", self._read_suppliers()[0])

    def test_does_not_refresh_the_date_on_an_existing_rejection(self):
        # Re-stamping nightly would keep moving `at` forward, so the 90-day lapse could never
        # elapse and a rejection would quietly become permanent.
        self._write_suppliers([{"host": "old.com",
                                "rejected": {"reason": "dead", "at": "2026-01-01"}}])
        self._supplier_row("old.com", empty_streak=9)
        self.assertEqual(discover.reject_by_streak(self.path, today=date(2026, 8, 4)), [])
        self.assertEqual(self._read_suppliers()[0]["rejected"]["at"], "2026-01-01")

    def test_a_stamped_host_is_dropped_from_this_runs_crawl_list(self):
        import asyncio
        from stonescan.ingest import run_all
        self._write_suppliers([{"host": "dead.com", "provider": "fake"},
                               {"host": "live.com", "provider": "fake"}])
        self._supplier_row("dead.com", empty_streak=3)
        self._supplier_row("live.com", empty_streak=0)
        attempted = _register_fake_provider(self)
        asyncio.run(run_all([{"host": "dead.com", "provider": "fake"},
                             {"host": "live.com", "provider": "fake"}], db_path=self.path))
        self.assertEqual(attempted, ["live.com"])            # stamped first, then never asked
        self.assertIn("rejected", {s["host"]: s for s in self._read_suppliers()}["dead.com"])

    def test_only_bypasses_the_start_of_run_pass(self):
        import asyncio
        from stonescan.ingest import run_all
        self._write_suppliers([{"host": "dead.com"}])
        self._supplier_row("dead.com", empty_streak=4)
        # --only sets honor_rejections=False: a request for named hosts must not restamp
        # the rest of the catalog on its way past.
        asyncio.run(run_all([], db_path=self.path, honor_rejections=False))
        self.assertNotIn("rejected", self._read_suppliers()[0])
        asyncio.run(run_all([], db_path=self.path))
        self.assertIn("rejected", self._read_suppliers()[0])


class CircuitBreakerTests(_SuppliersFileCase):
    """AIL-25: one washed-out provider must not cost the whole run — 192 of SlabWare's 212
    tenants answer 403, and at ~0.4 catalogs/min that alone outlasts the night."""

    def test_trips_on_the_fifteenth_consecutive_error_not_the_fourteenth(self):
        from stonescan.ingest import PROVIDER_ERROR_LIMIT, _Breaker
        self.assertEqual(PROVIDER_ERROR_LIMIT, 15)
        b = _Breaker()
        for _ in range(PROVIDER_ERROR_LIMIT - 1):
            b.record("slabware", False, "403 Forbidden")
        self.assertFalse(b.tripped("slabware"))
        b.record("slabware", False, "403 Forbidden")
        self.assertTrue(b.tripped("slabware"))

    def test_a_success_resets_the_run_of_errors(self):
        from stonescan.ingest import PROVIDER_ERROR_LIMIT, _Breaker
        b = _Breaker()
        for _ in range(PROVIDER_ERROR_LIMIT - 1):
            b.record("slabware", False, "403 Forbidden")
        b.record("slabware", True, "")
        for _ in range(PROVIDER_ERROR_LIMIT - 1):
            b.record("slabware", False, "403 Forbidden")
        self.assertFalse(b.tripped("slabware"))

    def test_a_robots_block_neither_advances_nor_resets(self):
        from stonescan.ingest import _Breaker
        b = _Breaker(limit=3)
        b.record("slabware", False, "403 Forbidden")
        b.record("slabware", False, "robots-blocked: disallowed")   # a decision, not a failure
        b.record("slabware", False, "403 Forbidden")
        self.assertFalse(b.tripped("slabware"))     # the block did not count as the third
        b.record("slabware", False, "403 Forbidden")
        self.assertTrue(b.tripped("slabware"))      # ...nor did it reset the two before it

    def test_a_successful_but_empty_catalog_resets(self):
        from stonescan.ingest import _Breaker
        b = _Breaker(limit=3)
        b.record("slabware", False, "403 Forbidden")
        b.record("slabware", False, "403 Forbidden")
        b.record("slabware", False, "")             # fetched fine, catalog simply empty
        b.record("slabware", False, "403 Forbidden")
        b.record("slabware", False, "403 Forbidden")
        self.assertFalse(b.tripped("slabware"))

    def test_an_empty_stone_profits_catalog_is_not_an_error(self):
        # The two crawl paths report an empty catalog differently and only one LOOKS empty:
        # a provider leaves `error` blank, while the Stone Profits crawler fills it with a
        # sentence that reads like a failure. Scored as an error, fifteen empty catalogs in a
        # row would abandon the largest source we have — and empty ones return fastest, so
        # under as_completed they arrive bunched at the front.
        from stonescan.crawler import EMPTY_CATALOG_ERROR
        from stonescan.ingest import _Breaker, _breaker_outcome
        self.assertEqual(_breaker_outcome(False, EMPTY_CATALOG_ERROR), "ok")
        b = _Breaker(limit=3)
        for _ in range(5):
            b.record("stoneprofits", False, EMPTY_CATALOG_ERROR)
        self.assertFalse(b.tripped("stoneprofits"))
        # ...while the genuine failure it is easily mistaken for still counts.
        for _ in range(3):
            b.record("stoneprofits", False, "Timeout 30000ms exceeded")
        self.assertTrue(b.tripped("stoneprofits"))

    def test_a_trip_inside_the_retry_pass_is_still_reported(self):
        # The retry builds its own breaker, so it can abandon hosts of its own. Interleaved
        # successes keep the main pass under the limit while the retry list — errors only —
        # runs straight through it, which makes this the one place a cap could go unlogged.
        import asyncio
        from stonescan import ingest, providers
        from stonescan.ingest import run_all
        from stonescan.providers.base import SupplierData

        good = {"s1.example.com", "s2.example.com"}

        async def crawl(entry, **kw):
            h = entry["host"]
            if h in good:
                return SupplierData(host=h, ok=True, materials=[], error="")
            return SupplierData(host=h, ok=False, error="403 Forbidden")

        orig_get = providers.get
        providers.get = lambda name: crawl if name == "mix" else orig_get(name)
        self.addCleanup(setattr, providers, "get", orig_get)
        orig = ingest.PROVIDER_ERROR_LIMIT
        ingest.PROVIDER_ERROR_LIMIT = 3
        self.addCleanup(setattr, ingest, "PROVIDER_ERROR_LIMIT", orig)

        hosts = ["e1.example.com", "e2.example.com", "s1.example.com", "e3.example.com",
                 "e4.example.com", "s2.example.com", "e5.example.com", "e6.example.com"]
        asyncio.run(run_all([{"host": h, "provider": "mix"} for h in hosts],
                            db_path=self.path, retry_errored=True))

        log = Path(self.path).resolve().parent / "refresh-history.log"
        self.assertIn("circuit breaker", log.read_text(encoding="utf-8"),
                      "a cap applied during the retry pass went unrecorded")

    def test_one_dead_provider_does_not_abandon_a_healthy_one(self):
        from stonescan.ingest import _Breaker
        b = _Breaker(limit=2)
        b.record("slabware", False, "403 Forbidden")
        b.record("slabware", False, "403 Forbidden")
        self.assertTrue(b.tripped("slabware"))
        self.assertFalse(b.tripped("umi"))

    def test_abandoned_hosts_are_neither_recorded_nor_retried(self):
        import asyncio
        from stonescan import ingest
        from stonescan.ingest import run_all

        hosts = [f"h{i}.example.com" for i in range(10)]
        for h in hosts:
            self._supplier_row(h, empty_streak=1, last_error="old error")
        attempted = _register_fake_provider(self)

        orig = ingest.PROVIDER_ERROR_LIMIT
        ingest.PROVIDER_ERROR_LIMIT = 3
        self.addCleanup(setattr, ingest, "PROVIDER_ERROR_LIMIT", orig)

        asyncio.run(run_all([{"host": h, "provider": "fake"} for h in hosts],
                            db_path=self.path, retry_errored=True))

        # AC-5: stopped at the limit instead of grinding through all ten.
        self.assertEqual(attempted, hosts[:3])
        # AC-7: the seven it never asked must look exactly as they did before the run. If a
        # skipped host banked an empty crawl, three such nights would auto-reject a healthy
        # supplier we never actually contacted — the breaker manufacturing its own evidence.
        for h in hosts[3:]:
            row = self._row(h)
            self.assertEqual(row["empty_streak"], 1, f"{h}: streak moved without a crawl")
            self.assertEqual(row["last_error"], "old error", f"{h}: last_error overwritten")

    def test_the_retry_pass_skips_an_abandoned_provider_but_not_a_healthy_one(self):
        import asyncio
        from stonescan import ingest
        from stonescan.ingest import run_all
        from stonescan import providers
        from stonescan.providers.base import SupplierData

        # 'dead' washes out; 'live' fails once, which is a normal retryable blip.
        attempted: list[str] = []

        async def crawl(entry, **kw):
            attempted.append(entry["host"])
            return SupplierData(host=entry["host"], ok=False, error="403 Forbidden")

        orig_get = providers.get
        providers.get = lambda name: crawl if name in ("dead", "live") else orig_get(name)
        self.addCleanup(setattr, providers, "get", orig_get)

        orig = ingest.PROVIDER_ERROR_LIMIT
        ingest.PROVIDER_ERROR_LIMIT = 2
        self.addCleanup(setattr, ingest, "PROVIDER_ERROR_LIMIT", orig)

        entries = ([{"host": f"d{i}.example.com", "provider": "dead"} for i in range(4)]
                   + [{"host": "l0.example.com", "provider": "live"}])
        asyncio.run(run_all(entries, db_path=self.path, retry_errored=True))

        # The dead provider is asked twice, then abandoned and kept out of the retry.
        self.assertEqual([h for h in attempted if h.startswith("d")],
                         ["d0.example.com", "d1.example.com"])
        # The healthy provider still gets its retry — the breaker is per provider, and AC-8
        # must not turn into "one bad platform disables retries for everyone".
        self.assertEqual([h for h in attempted if h.startswith("l")],
                         ["l0.example.com", "l0.example.com"])


class StorefrontFilterTests(unittest.TestCase):
    """A mirror that is really a single-type storefront (American Quartz sells only KLZ's
    quartz) can be re-scoped to that type — but only after a human confirms, and never in a
    way that empties a supplier."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)

    def tearDown(self):
        self.conn.close()

    def _supplier(self, sid, host, token, types, *, company="", products=""):
        """A supplier whose catalog is {material_type: n_items}."""
        total = sum(types.values())
        self.conn.execute(
            "INSERT INTO suppliers (id, host, token, company, products, item_count) "
            "VALUES (?,?,?,?,?,?)", (sid, host, token, company, products, total))
        i = 0
        for mtype, n in types.items():
            for _ in range(n):
                i += 1
                self.conn.execute(
                    """INSERT INTO materials
                         (supplier_id, item_id, item_name, name_norm, material_key,
                          material_type)
                       VALUES (?,?,?,?,?,?)""",
                    (sid, str(i), f"Stone {i}", f"STONE {i}", f"stone {i}|{mtype.lower()}",
                     mtype))
        self.conn.commit()

    def _mirror_pair(self):
        """The real shape: klz (842 across types) mirrored by americanquartz, which sells
        only the quartz slice."""
        cat = {"Quartz": 165, "Quartzite": 180, "Granite": 160, "Marble": 97}
        self._supplier(1, "klz.example.com", "klz", cat, company="KLZ",
                       products="Granite, Marble, Quartz, Quartzite")
        self._supplier(2, "americanquartz.example.com", "klz", cat,
                       company="American Quartz", products="Experts in Quartz Surfaces")
        db.detect_mirrors(self.conn, entries=[])

    def _items(self, sid, mtype=""):
        return db._distinct_item_count(self.conn, sid, mtype)

    # --- inference ---------------------------------------------------------
    def test_infers_the_single_named_type(self):
        self._mirror_pair()
        p = db.supplier_filter_proposals(self.conn)["americanquartz.example.com"]
        self.assertEqual(p["material_type"], "Quartz")
        self.assertEqual((p["matched"], p["total"]), (165, 602))

    def test_no_proposal_when_several_types_are_named(self):
        # "GS Granite" + products listing four types -> nothing single to infer (AC-5).
        cat = {"Granite": 200, "Marble": 128, "Quartz": 100}
        self._supplier(1, "gsgraniteroseville.example.com", "gs", cat, company="GS Granite",
                       products="Granite, Marble, Quartz, Quartzite")
        self._supplier(2, "gsgranitesavage.example.com", "gs", cat, company="GS Granite",
                       products="Granite, Marble, Quartz, Quartzite")
        db.detect_mirrors(self.conn, entries=[])
        p = db.supplier_filter_proposals(self.conn)["gsgranitesavage.example.com"]
        self.assertIsNone(p["material_type"])

    def test_inference_is_whole_word(self):
        # "Quartzite Imports" must infer Quartzite, never Quartz from the substring.
        self.assertEqual(db._infer_filter_type("quartzite imports", ["Quartz", "Quartzite"]),
                         "Quartzite")
        self.assertEqual(db._infer_filter_type("experts in quartz surfaces",
                                               ["Quartz", "Quartzite", "Granite"]), "Quartz")
        self.assertIsNone(db._infer_filter_type("stone gallery", ["Quartz", "Granite"]))

    def test_non_mirror_suppliers_get_no_proposal(self):
        # NG-5: a lone supplier whose name implies a niche is never proposed a filter.
        self._supplier(1, "quartzworld.example.com", "qw", {"Quartz": 10, "Granite": 5},
                       company="Quartz World", products="Quartz")
        db.detect_mirrors(self.conn, entries=[])
        self.assertEqual(db.supplier_filter_proposals(self.conn), {})

    # --- confirm -----------------------------------------------------------
    def test_confirm_rescopes_and_clears_the_mirror(self):
        self._mirror_pair()
        kept = db.add_supplier_filter(self.conn, "americanquartz.example.com", "Quartz")
        self.assertEqual(kept, 165)
        self.assertEqual(self._items(2), 165)
        self.assertEqual(self._items(1), 602)          # canonical untouched
        db.detect_mirrors(self.conn, entries=[])       # 165 != 602 -> no longer a mirror
        self.assertEqual(db.mirror_report(self.conn), [])
        self.assertEqual(db.stats(self.conn, use_cache=False)["suppliers"], 2)

    def test_confirm_is_refused_when_it_would_empty_the_supplier(self):
        self._mirror_pair()
        kept = db.add_supplier_filter(self.conn, "americanquartz.example.com", "Soapstone")
        self.assertEqual(kept, 0)
        self.assertEqual(self._items(2), 602)          # nothing dropped
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM supplier_filters").fetchone()[0], 0)

    def test_confirm_also_drops_the_filtered_out_slabs(self):
        self._mirror_pair()
        self.conn.execute("INSERT INTO slabs (supplier_id, item_id, slab_no) VALUES (2,'1','A')")
        self.conn.execute("INSERT INTO slabs (supplier_id, item_id, slab_no) VALUES (2,'600','B')")
        self.conn.commit()
        db.add_supplier_filter(self.conn, "americanquartz.example.com", "Quartz")
        kept_ids = {r[0] for r in self.conn.execute(
            "SELECT item_id FROM slabs WHERE supplier_id = 2")}
        self.assertEqual(kept_ids, {"1"})              # item 600 is Quartzite -> its slab goes

    # --- persistence across a re-crawl -------------------------------------
    def test_filter_is_reapplied_after_a_recrawl_refetches_everything(self):
        self._mirror_pair()
        db.add_supplier_filter(self.conn, "americanquartz.example.com", "Quartz")
        # Simulate the next crawl re-storing the vanity host's WHOLE tenant catalog.
        for i in range(1, 181):
            self.conn.execute(
                """INSERT INTO materials (supplier_id, item_id, item_name, name_norm,
                                          material_key, material_type)
                   VALUES (2,?,?,?,?,'Quartzite')""",
                (f"r{i}", f"Re {i}", f"RE {i}", f"re {i}|quartzite"))
        self.conn.commit()
        self.assertEqual(self._items(2), 345)
        n = db.apply_supplier_filters(self.conn)       # the ingest hook
        self.assertEqual(n, 1)
        self.assertEqual(self._items(2), 165)          # re-scoped, like apply_aliases re-folds

    def test_reapply_skips_a_filter_that_would_match_nothing(self):
        self._mirror_pair()
        db.add_supplier_filter(self.conn, "americanquartz.example.com", "Quartz")
        self.conn.execute("DELETE FROM materials WHERE supplier_id = 2")
        self.conn.commit()
        self.assertEqual(db.apply_supplier_filters(self.conn), 0)   # never empties further

    # --- reject ------------------------------------------------------------
    def test_reject_suppresses_the_same_proposal(self):
        self._mirror_pair()
        db.reject_supplier_filter(self.conn, "americanquartz.example.com", "Quartz")
        p = db.supplier_filter_proposals(self.conn)["americanquartz.example.com"]
        self.assertIsNone(p["material_type"])          # not re-offered
        self.assertEqual(len(db.mirror_report(self.conn)), 1)   # and it stays a mirror

    def test_reject_is_specific_to_the_proposed_type(self):
        self._mirror_pair()
        db.reject_supplier_filter(self.conn, "americanquartz.example.com", "Granite")
        p = db.supplier_filter_proposals(self.conn)["americanquartz.example.com"]
        self.assertEqual(p["material_type"], "Quartz")  # a different proposal still stands


class SlabCountCollapseTests(unittest.TestCase):
    """A materials row is one inventory identifier, not one product. Where a tenant sends
    one row per slab and repeats the item's total on each, summing them multiplies the
    count — so a multi-row item collapses to the number of slab-identifier rows, while a
    single-row item (whose one value IS the item's count) is left alone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=5)

    def tearDown(self):
        self.conn.close()

    def _row(self, supplier_id, item_id, *, idone=None, slabs=0, form="SLAB",
             length=0, width=0, uom=""):
        # idone=None -> NULL. UNIQUE(supplier_id, item_id, idone) treats '' as a real
        # value, so a tenant's dimensionless item-level rows arrive as NULL, not ''.
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, idone, item_name, name_norm, material_key,
                  material_type, product_form, available_slabs, uom, avg_length, avg_width)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (supplier_id, item_id, idone, "Stone", "STONE", "stone|granite",
             "Granite", form, slabs, uom, length, width))
        self.conn.commit()

    def _slabs_for(self, supplier_id, item_id):
        return self.conn.execute(
            "SELECT SUM(COALESCE(available_slabs,0)) FROM materials "
            "WHERE supplier_id=? AND item_id=?", (supplier_id, item_id)).fetchone()[0]

    # --- the core split -----------------------------------------------------
    def test_multi_row_item_collapses_to_its_identifier_count(self):
        # The georgianstone shape: 6 slab rows, each repeating an item total of 114.
        for i in range(6):
            self._row(1, "190", idone=f"101{i}", slabs=114, length=100, width=60)
        self.assertEqual(self._slabs_for(1, "190"), 684, "sum before the fix")
        self.assertEqual(db.collapse_item_slab_counts(self.conn), 1)
        self.assertEqual(self._slabs_for(1, "190"), 6, "6 identifier rows = 6 slabs")

    def test_single_row_item_is_left_alone(self):
        # One row stating 17 slabs IS the item's count — exact against the gallery 70%
        # of the time on real data, so it must not be reduced to 1.
        self._row(1, "500", idone="a", slabs=17, length=100, width=60)
        self.assertEqual(db.collapse_item_slab_counts(self.conn), 0)
        self.assertEqual(self._slabs_for(1, "500"), 17)

    def test_one_slab_per_row_yard_still_sums_to_its_row_count(self):
        # The OHM shape and the guarantee of test_per_slab_row_supplier_sums_within_the_yard:
        # 5 rows of 1 slab at one yard stays 5, because 5 identifier rows = 5 slabs.
        for i in range(5):
            self._row(1, "300", idone=f"s{i}", slabs=1, length=100, width=60)
        db.collapse_item_slab_counts(self.conn)
        self.assertEqual(self._slabs_for(1, "300"), 5)

    # --- the traps ----------------------------------------------------------
    def test_area_survives_the_collapse(self):
        # AC-3: parking the count on a dimensionless item-level summary row would drop
        # _SQFT (SUM(slabs x L x W)) to zero. The representative must carry dimensions.
        self._row(1, "190", slabs=8785, length=0, width=0)   # summary row (NULL idone)
        for i in range(4):
            self._row(1, "190", idone=f"i{i}", slabs=114, length=120, width=70)
        db.collapse_item_slab_counts(self.conn)
        area = self.conn.execute(
            "SELECT SUM(COALESCE(available_slabs,0) * COALESCE(avg_length,0) "
            "* COALESCE(avg_width,0)) / 144.0 FROM materials "
            "WHERE supplier_id=1 AND item_id='190'").fetchone()[0]
        self.assertEqual(self._slabs_for(1, "190"), 4, "4 identifier rows")
        self.assertGreater(area, 0, "the count must not land on a dimensionless row")

    def test_group_with_no_identifier_rows_falls_back_to_the_largest_figure(self):
        self._row(1, "700", slabs=40, length=100, width=60)
        self._row(1, "700", idone="", slabs=25, length=100, width=60)
        db.collapse_item_slab_counts(self.conn)
        self.assertEqual(self._slabs_for(1, "700"), 40, "no invented number")

    def test_tiles_are_untouched(self):
        # available_slabs holds square feet for tiles; the app handles that separately.
        for i in range(3):
            self._row(1, "800", idone=f"t{i}", slabs=2493, form="TILE", uom="SF")
        self.assertEqual(db.collapse_item_slab_counts(self.conn), 0)
        self.assertEqual(self._slabs_for(1, "800"), 7479)

    def test_idempotent(self):
        for i in range(6):
            self._row(1, "190", idone=f"x{i}", slabs=114, length=100, width=60)
        db.collapse_item_slab_counts(self.conn)
        first = self._slabs_for(1, "190")
        db.collapse_item_slab_counts(self.conn)
        self.assertEqual(self._slabs_for(1, "190"), first, "a second pass must not re-reduce")
        self.assertEqual(first, 6)

    def test_items_are_collapsed_independently_within_a_supplier(self):
        for i in range(3):
            self._row(1, "A", idone=f"a{i}", slabs=50, length=100, width=60)
        for i in range(7):
            self._row(1, "B", idone=f"b{i}", slabs=50, length=100, width=60)
        self._row(1, "C", idone="c0", slabs=12, length=100, width=60)
        self.assertEqual(db.collapse_item_slab_counts(self.conn), 2)
        self.assertEqual(self._slabs_for(1, "A"), 3)
        self.assertEqual(self._slabs_for(1, "B"), 7)
        self.assertEqual(self._slabs_for(1, "C"), 12, "single-row item untouched")

    def test_same_item_id_at_two_suppliers_does_not_merge(self):
        # item_id is unique per-supplier only, so the grouping must include supplier_id.
        for i in range(4):
            self._row(1, "999", idone=f"p{i}", slabs=30, length=100, width=60)
        for i in range(2):
            self._row(2, "999", idone=f"q{i}", slabs=30, length=100, width=60)
        self.assertEqual(db.collapse_item_slab_counts(self.conn), 2)
        self.assertEqual(self._slabs_for(1, "999"), 4)
        self.assertEqual(self._slabs_for(2, "999"), 2)


class MaterialKeyTailTests(unittest.TestCase):
    """The same stone must key the same however a supplier chose to write it — with or
    without its own type word, "Slabs", or an mm/inch size — without merging stones whose
    real name happens to contain a type word."""

    def test_type_word_tails_collapse_to_one_key(self):
        want = nz.material_key("Taj Mahal", "Quartzite")
        for variant in ("Taj Mahal Quartzite", "Quartzite Taj Mahal", "Taj Mahal Slabs",
                        "Taj Mahal Slab", "Taj Mahal Finish",
                        "Taj Mahal Quartzite Slab 30mm", "Taj Mahal Quartzite 20 mm",
                        "TAJ MAHAL QUARTZITE SLAB"):
            self.assertEqual(nz.material_key(variant, "Quartzite"), want, variant)

    def test_supplier_category_prefix_is_stripped(self):
        # The dominant real shape: the supplier prefixes their category onto every name.
        self.assertEqual(nz.material_key("GRANITE AZUL PLATINO 3CM - POLISHED", "Granite"),
                         nz.material_key("Azul Platino", "Granite"))
        self.assertEqual(nz.material_key("QUARTZITE - AZURE - 3cm -", "Quartzite"),
                         nz.material_key("Azure", "Quartzite"))

    def test_only_the_rows_own_type_is_stripped(self):
        # "Blue Granite" filed as Marble keeps its name — stripping the whole type
        # vocabulary would merge it with an unrelated "Blue".
        self.assertEqual(nz.material_key("Blue Granite", "Marble"), "blue granite|marble")

    def test_multi_word_types_are_dropped_as_a_phrase(self):
        # AC-2: token-wise removal would turn "Grey Stone" into "Grey".
        self.assertEqual(nz.material_key("Grey Stone", "Engineered Stone"),
                         "grey stone|engineered stone")
        self.assertEqual(nz.material_key("Sintered Stone Infinity White", "Sintered Stone"),
                         nz.material_key("Infinity White", "Sintered Stone"))

    def test_never_reduces_a_name_to_nothing(self):
        # AC-3: a listing named only for its type, or only "Slab", keeps a usable key.
        self.assertEqual(nz.material_key("Quartzite", "Quartzite"), "quartzite|quartzite")
        self.assertEqual(nz.material_key("Slab", "Granite"), "slab|granite")
        self.assertEqual(nz.material_key("Slabs", "Marble"), "slabs|marble")

    def test_untyped_rows_are_untouched(self):
        # 'Other'/accessory aren't words suppliers put in product names.
        self.assertEqual(nz.material_key("Taj Mahal", "Other"), "taj mahal|other")

    def test_earlier_key_hygiene_still_holds(self):
        clean = nz.material_key("Cassablanca", "Quartzite")
        self.assertEqual(nz.material_key("Cassablanca Polished126.5 x 78.5", "Quartzite"), clean)
        self.assertEqual(nz.material_key("Cassablanca Unpolished 3cm", "Quartzite"), clean)


class AliasRemapTests(unittest.TestCase):
    """A confirmed merge is two stored material_keys. When the key rules change they must
    be re-derived, or apply_aliases folds rows into a key nothing produces any more."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=4)

    def tearDown(self):
        self.conn.close()

    def test_remap_key_re_derives_a_stored_key(self):
        self.assertEqual(nz.remap_key("taj mahal quartzite|quartzite"), "taj mahal|quartzite")
        self.assertEqual(nz.remap_key("black pearl finish|granite"), "black pearl|granite")
        self.assertEqual(nz.remap_key("taj mahal|quartzite"), "taj mahal|quartzite")
        self.assertEqual(nz.remap_key(""), "")
        self.assertEqual(nz.remap_key("no-pipe"), "no-pipe")

    def test_a_merge_survives_the_key_change(self):
        # The curator merged the granite spelling into the quartzite one under the OLD
        # keys. After the rules change, the fold must still reach real rows.
        db.add_alias(self.conn, "taj mahal|granite", "taj mahal quartzite|quartzite")
        db.remap_aliases(self.conn, nz.remap_key)
        _insert(self.conn, 1, "Taj Mahal", "Granite")
        _insert(self.conn, 2, "Taj Mahal Quartzite", "Quartzite")
        self.assertEqual(db.apply_aliases(self.conn), 1)
        keys = {r[0] for r in self.conn.execute("SELECT DISTINCT material_key FROM materials")}
        self.assertEqual(keys, {"taj mahal|quartzite"}, "both rows land on the live key")

    def test_a_merge_the_new_rules_already_make_is_dropped(self):
        db.add_alias(self.conn, "taj mahal quartzite|quartzite", "taj mahal|quartzite")
        moved = db.remap_aliases(self.conn, nz.remap_key)
        self.assertEqual(moved["dropped"], 1, "both sides now key the same; nothing to fold")
        self.assertEqual(db.quality_stats(self.conn)["aliases"], 0)

    def test_remap_is_idempotent(self):
        db.add_alias(self.conn, "taj mahal|granite", "taj mahal quartzite|quartzite")
        first = db.remap_aliases(self.conn, nz.remap_key)
        rows1 = db.list_aliases(self.conn)
        second = db.remap_aliases(self.conn, nz.remap_key)
        self.assertEqual(second["remapped"], 0, "a second pass moves nothing")
        self.assertEqual([r["alias_key"] for r in db.list_aliases(self.conn)],
                         [r["alias_key"] for r in rows1])
        self.assertEqual(first["kept"], second["kept"])

    def test_unrelated_merges_are_left_alone(self):
        db.add_alias(self.conn, "alpha|granite", "beta|granite")
        db.remap_aliases(self.conn, nz.remap_key)
        al = db.list_aliases(self.conn)
        self.assertEqual((al[0]["alias_key"], al[0]["canonical_key"]),
                         ("alpha|granite", "beta|granite"))


class SlabCountPropagationTests(unittest.TestCase):
    """The corrected per-item slab count has to reach every surface that quotes a
    quantity — and the one-off change in how stock is counted must not read as a
    catalog-wide sell-off in the alert digest."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=4)

    def tearDown(self):
        self.conn.close()

    def _row(self, supplier_id, item_id, *, idone=None, slabs=0, form="SLAB", uom="",
             name="Stone", length=0, width=0):
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, idone, item_name, name_norm, material_key,
                  material_type, product_form, available_slabs, uom, avg_length, avg_width)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (supplier_id, item_id, idone, name, name.upper(), f"{name.lower()}|granite",
             "Granite", form, slabs, uom, length, width))
        self.conn.commit()

    # --- AC-1: history records the corrected figure -------------------------
    def test_resnapshot_uses_the_collapsed_count(self):
        for i in range(6):
            self._row(1, "190", idone=f"s{i}", slabs=114)
        db.snapshot_history(self.conn, 1, "2026-08-01")           # during the crawl
        before = self.conn.execute(
            "SELECT SUM(slabs) FROM history WHERE snapshot_date='2026-08-01'").fetchone()[0]
        self.assertEqual(before, 684, "the pre-collapse snapshot keeps the inflated sum")

        db.collapse_item_slab_counts(self.conn)
        self.assertEqual(db.resnapshot_history(self.conn, "2026-08-01"), 1)
        after = self.conn.execute(
            "SELECT SUM(slabs) FROM history WHERE snapshot_date='2026-08-01'").fetchone()[0]
        self.assertEqual(after, 6, "history now matches what the rest of the app shows")

    def test_resnapshot_only_touches_suppliers_already_snapshotted_today(self):
        self._row(1, "a", idone="x", slabs=3)
        self._row(2, "b", idone="y", slabs=4)
        db.snapshot_history(self.conn, 1, "2026-08-01")   # only supplier 1 was crawled
        self.assertEqual(db.resnapshot_history(self.conn, "2026-08-01"), 1)
        sids = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT supplier_id FROM history WHERE snapshot_date='2026-08-01'")}
        self.assertEqual(sids, {1}, "an uncrawled supplier must not gain a today row")

    def test_resnapshot_marks_rows_rebased(self):
        self._row(1, "a", idone="x", slabs=3)
        db.snapshot_history(self.conn, 1, "2026-08-01")
        self.assertEqual(self.conn.execute(
            "SELECT MAX(rebased) FROM history").fetchone()[0], 0)
        db.resnapshot_history(self.conn, "2026-08-01")
        self.assertEqual(self.conn.execute(
            "SELECT MIN(rebased) FROM history").fetchone()[0], 1)

    # --- AC-2: the step change must not fire false alerts -------------------
    def test_drops_are_suppressed_across_the_rebase_boundary(self):
        from stonescan.web.app import _classify_alert
        # A listing that "fell" 684 -> 6 purely because the counting changed.
        self.assertIsNone(_classify_alert(6, 684, crossed_rebase=True))
        self.assertEqual(_classify_alert(6, 684), "dropped", "normally a real drop")
        # Sold-out is the same story across the boundary.
        self.assertIsNone(_classify_alert(0, 500, crossed_rebase=True))
        self.assertEqual(_classify_alert(0, 500), "soldout")

    def test_up_direction_still_reports_across_the_boundary(self):
        from stonescan.web.app import _classify_alert
        self.assertEqual(_classify_alert(9, 0, crossed_rebase=True), "restock")
        self.assertEqual(_classify_alert(4, None, crossed_rebase=True), "listed")

    def test_suppression_is_one_comparison_only(self):
        from stonescan.web.app import _classify_alert
        # Once both snapshots are on the new accounting the flag is false again.
        self.assertEqual(_classify_alert(2, 40, crossed_rebase=False), "dropped")

    # --- AC-3: a tile's square feet is never a slab count -------------------
    def test_a_tile_on_a_list_does_not_report_its_square_feet_as_slabs(self):
        self._row(1, "800", idone="t0", slabs=2493, form="TILE", uom="SF", name="Riverwhite")
        mid = self.conn.execute("SELECT id FROM materials WHERE item_id='800'").fetchone()[0]
        lid = db.create_list(self.conn, "job", "2026-08-01")
        db.add_to_list(self.conn, lid, mid, "2026-08-01")
        it = db.get_list_items(self.conn, lid)[0]
        self.assertEqual(it["live_slabs"] or 0, 0, "2493 SF must not read as slabs")
        self.assertEqual(round(it["live_tile_sf"] or 0), 2493, "it is surfaced as area")

    def test_a_slab_listing_on_a_list_still_reports_its_slabs(self):
        self._row(1, "801", idone="s0", slabs=7, name="Ubatuba")
        mid = self.conn.execute("SELECT id FROM materials WHERE item_id='801'").fetchone()[0]
        lid = db.create_list(self.conn, "job", "2026-08-01")
        db.add_to_list(self.conn, lid, mid, "2026-08-01")
        it = db.get_list_items(self.conn, lid)[0]
        self.assertEqual(it["live_slabs"], 7)
        self.assertEqual(it["live_tile_sf"] or 0, 0)

    # --- AC-5: every surface agrees ----------------------------------------
    def test_list_and_history_agree_with_the_collapsed_count(self):
        for i in range(6):
            self._row(1, "190", idone=f"s{i}", slabs=114, name="Lunapearl")
        db.collapse_item_slab_counts(self.conn)
        db.snapshot_history(self.conn, 1, "2026-08-01", rebased=True)
        mid = self.conn.execute("SELECT id FROM materials WHERE item_id='190' LIMIT 1").fetchone()[0]
        lid = db.create_list(self.conn, "job", "2026-08-01")
        db.add_to_list(self.conn, lid, mid, "2026-08-01")
        hist = self.conn.execute(
            "SELECT SUM(slabs) FROM history WHERE snapshot_date='2026-08-01'").fetchone()[0]
        materials = self.conn.execute(
            "SELECT SUM(available_slabs) FROM materials WHERE supplier_id=1").fetchone()[0]
        self.assertEqual((hist, materials, db.get_list_items(self.conn, lid)[0]["live_slabs"]),
                         (6, 6, 6))


class LocationFilterTests(unittest.TestCase):
    """`slabs.location` is free text, and the columns the filter reads are GROUP_CONCAT
    lists. A bare substring match therefore let a two-letter yard code hit the middle of
    a city name — picking `ES` returned a quarter of the catalog."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=8)

    def tearDown(self):
        self.conn.close()

    def _mat(self, supplier_id, name, locations):
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key,
                  material_type, available_slabs, locations)
               VALUES (?,?,?,?,?,?,?,?)""",
            (supplier_id, f"{supplier_id}-{name}", name, name.upper(),
             f"{name.lower()}|granite", "Granite", 1, locations))
        self.conn.commit()

    def _geo(self, location, label, source):
        self.conn.execute(
            "INSERT OR REPLACE INTO location_geo (location, lat, lon, label, source) "
            "VALUES (?,?,?,?,?)", (location, 1.0, 2.0, label, source))
        self.conn.commit()

    def _slab_loc(self, supplier_id, location):
        self.conn.execute(
            "INSERT INTO slabs (supplier_id, item_id, location) VALUES (?,?,?)",
            (supplier_id, "x", location))
        self.conn.commit()

    def _find(self, location):
        from stonescan.web import app
        _t, rows, _ = app._search(self.conn, q="", material_type="", color="", thickness="",
                                  supplier="", location=location, limit=50, offset=0)
        return {r["material_key"] for r in rows}

    # --- AC-1: whole-token, not substring -----------------------------------
    def test_a_short_yard_code_does_not_match_inside_a_city_name(self):
        self._slab_loc(1, "ES"); self._slab_loc(2, "Charleston")
        self._mat(1, "Real", "ES")
        self._mat(2, "Bystander", "Charleston")
        self.assertEqual(self._find("ES"), {"real|granite"},
                         "'ES' must not hit the 'es' inside Charl-es-ton")

    def test_a_token_in_the_middle_of_the_list_still_matches(self):
        self._slab_loc(1, "ES")
        self._mat(1, "Mid", "Dallas,ES,Houston")
        self._mat(2, "Nope", "Dallas,Houston")
        self.assertEqual(self._find("ES"), {"mid|granite"})

    def test_a_value_containing_a_comma_still_matches(self):
        # AC-4: the columns are comma-JOINED, but the whole value still sits between
        # two delimiters, so an embedded comma is not a problem.
        self._slab_loc(1, "Atlanta, GA")
        self._mat(1, "Peach", "Atlanta, GA,Modesto")
        self._mat(2, "Other", "Modesto")
        self.assertEqual(self._find("Atlanta, GA"), {"peach|granite"})

    def test_the_supplier_fallback_still_applies(self):
        # A material with no slab-level location falls back to its supplier's yards.
        self._slab_loc(1, "KLZ")
        self.conn.execute("UPDATE suppliers SET locations = 'KLZ' WHERE id = 1")
        self.conn.commit()
        self._mat(1, "Fallback", "")
        self.assertEqual(self._find("KLZ"), {"fallback|granite"})

    # --- AC-2 / AC-3: grouping, and the guards on it ------------------------
    def test_city_spellings_fold_into_one_option(self):
        for v in ("Atlanta", "ATLANTA"):
            self._slab_loc(1, v)
            self._geo(v, "Atlanta, GA", "exact")
        self._mat(1, "A", "Atlanta")
        self._mat(2, "B", "ATLANTA")
        opts = db.location_options(self.conn)
        city = [o for o in opts if o["kind"] == "city"]
        self.assertEqual([o["value"] for o in city], ["Atlanta, GA"])
        self.assertEqual(sorted(city[0]["members"]), ["ATLANTA", "Atlanta"])
        self.assertEqual(self._find("Atlanta, GA"), {"a|granite", "b|granite"},
                         "the group must reach every spelling")

    def test_an_ambiguous_resolution_is_not_folded(self):
        self._slab_loc(1, "Albany")
        self._geo("Albany", "Albany, NY", "ambiguous")
        opts = {o["value"]: o for o in db.location_options(self.conn)}
        self.assertIn("Albany", opts)
        self.assertEqual(opts["Albany"]["kind"], "yard",
                         "an ambiguous city must not be presented as a resolved place")

    def test_a_name_that_merely_contains_the_city_is_not_folded(self):
        # The CLAUDE.md trap: containment puts "Baldwin Hills" (Los Angeles) under
        # "Baldwin, NY". Only an exact city match folds.
        self._slab_loc(1, "Baldwin Hills")
        self._geo("Baldwin Hills", "Baldwin, NY", "exact")
        opts = {o["value"]: o for o in db.location_options(self.conn)}
        self.assertIn("Baldwin Hills", opts)
        self.assertEqual(opts["Baldwin Hills"]["kind"], "yard")

    # --- AC-5: nothing is dropped ------------------------------------------
    def test_an_unresolvable_yard_code_is_still_offered_and_still_matches(self):
        self._slab_loc(1, "KLZ")          # no location_geo row at all
        self._mat(1, "Klzstone", "KLZ")
        opts = {o["value"]: o for o in db.location_options(self.conn)}
        self.assertEqual(opts["KLZ"]["kind"], "yard")
        self.assertEqual(self._find("KLZ"), {"klzstone|granite"})

    def test_every_raw_value_remains_reachable(self):
        for v in ("KLZ", "BF", "ES", "not indicated", "https://example.test/"):
            self._slab_loc(1, v)
        reachable = set()
        for o in db.location_options(self.conn):
            reachable.update(o["members"])
        self.assertEqual(reachable, {"KLZ", "BF", "ES", "not indicated",
                                     "https://example.test/"})

    def test_an_unknown_value_falls_back_to_itself(self):
        # A bookmarked URL or a saved search from before grouping existed must still work.
        self._slab_loc(1, "KLZ")
        self.assertEqual(db.location_members(self.conn, "Nowhere"), ["Nowhere"])
        self.assertEqual(db.location_members(self.conn, ""), [])


class ColourFamilyTests(unittest.TestCase):
    """`color` is free text with 983 distinct values, and the dropdown matched on
    equality — so "Gray" and "Grey" were separate choices that each returned part of the
    family, and a row coloured "Gray, White" was reachable from neither."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")
        self.conn = db.init_db(self.path)
        _seed_suppliers(self.conn, upto=6)

    def tearDown(self):
        self.conn.close()

    def _mat(self, supplier_id, name, color):
        self.conn.execute(
            """INSERT INTO materials
                 (supplier_id, item_id, item_name, name_norm, material_key,
                  material_type, color, available_slabs)
               VALUES (?,?,?,?,?,?,?,1)""",
            (supplier_id, f"{supplier_id}-{name}", name, name.upper(),
             f"{name.lower()}|granite", "Granite", color))
        self.conn.commit()

    def _find(self, color):
        from stonescan.web import app
        _t, rows, _ = app._search(self.conn, q="", material_type="", color=color,
                                  thickness="", supplier="", limit=50, offset=0)
        return {r["material_key"] for r in rows}

    # --- AC-2: word-level, whole family -------------------------------------
    def test_gray_and_grey_are_one_family(self):
        self._mat(1, "Amer", "Gray")
        self._mat(2, "Brit", "Grey")
        self._mat(3, "Light", "Light Grey")
        self.assertEqual(self._find("Gray"),
                         {"amer|granite", "brit|granite", "light|granite"})

    def test_a_multi_colour_row_is_reachable_from_each_of_its_colours(self):
        self._mat(1, "Both", "Gray, White")
        self.assertIn("both|granite", self._find("Gray"))
        self.assertIn("both|granite", self._find("White"))

    def test_case_and_punctuation_variants_fold(self):
        self._mat(1, "A", "Off White")
        self._mat(2, "B", "Off-White")
        self._mat(3, "C", "White.")
        self.assertEqual(self._find("White"),
                         {"a|granite", "b|granite", "c|granite"})

    def test_a_word_inside_another_word_is_not_a_match(self):
        # The reason this is word-level and not LIKE '%red%'.
        self._mat(1, "Real", "Red")
        self._mat(2, "Fake", "Coloured")
        self.assertEqual(self._find("Red"), {"real|granite"})

    # --- AC-1: the option list ---------------------------------------------
    def test_options_are_families_present_plus_a_long_tail_bucket(self):
        from stonescan.web import app
        self._mat(1, "A", "Gray")
        self._mat(2, "B", "Grey")
        self._mat(3, "C", "Chartreuse Sparkle")   # in no family
        opts = app._color_options(self.conn)
        self.assertIn("Gray", opts)
        self.assertNotIn("Grey", opts, "Grey is folded into Gray, not its own option")
        self.assertIn(app._COLOR_OTHER, opts)

    def test_the_long_tail_stays_reachable(self):
        self._mat(1, "Odd", "Chartreuse Sparkle")
        self._mat(2, "Plain", "White")
        from stonescan.web import app
        self.assertEqual(self._find(app._COLOR_OTHER), {"odd|granite"})

    def test_no_option_is_offered_for_a_family_with_no_rows(self):
        from stonescan.web import app
        self._mat(1, "A", "White")
        self.assertNotIn("Purple", app._color_options(self.conn))

    # --- AC-5: a colour saved before families existed --------------------------
    def test_a_legacy_raw_value_widens_to_its_family(self):
        self._mat(1, "A", "Gray")
        self._mat(2, "B", "Grey")
        # A watchlist saved "Grey" back when it was its own option.
        self.assertEqual(self._find("Grey"), {"a|granite", "b|granite"},
                         "an old saved colour must widen, never silently stop matching")

    def test_a_legacy_value_with_no_family_still_matches_itself(self):
        self._mat(1, "Odd", "Chartreuse Sparkle")
        self._mat(2, "Other", "White")
        self.assertEqual(self._find("Chartreuse Sparkle"), {"odd|granite"})

    def test_an_unknown_colour_returns_nothing_rather_than_everything(self):
        self._mat(1, "A", "White")
        self.assertEqual(self._find("Nonesuch"), set())

    # --- AC-3: the two "Similar materials" sites ---------------------------
    def test_similar_materials_uses_the_family_not_the_spelling(self):
        from stonescan.web import app
        self._mat(1, "Anchor", "Grey")
        self._mat(2, "Cousin", "Gray")
        self._mat(3, "Stranger", "Red")
        fam = app._colors_for_choice(self.conn, "Grey")
        self.assertIn("Gray", fam)
        self.assertIn("Grey", fam)
        self.assertNotIn("Red", fam)

    def test_an_uncoloured_material_has_no_colour_restriction(self):
        from stonescan.web import app
        self.assertEqual(app._colors_for_choice(self.conn, ""), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
