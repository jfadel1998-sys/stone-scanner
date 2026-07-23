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


if __name__ == "__main__":
    unittest.main(verbosity=2)
