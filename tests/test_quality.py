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
        self.assertEqual([len(d[k]) for k in ("restock", "listed", "dropped", "soldout")], [0, 0, 0, 0])
        self.assertEqual(app._alert_unread_count(self.conn), 0)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
