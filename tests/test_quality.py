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
        from stonescan.providers import genericfeed as gf
        robots = ("User-agent: *\nDisallow: /cart\nDisallow: /*filter_*\n"
                  "User-agent: BadBot\nDisallow: /\n"
                  "Sitemap: https://x.com/sitemap_index.xml")
        sitemaps, disallows = gf._robots(robots)
        self.assertEqual(sitemaps, ["https://x.com/sitemap_index.xml"])
        self.assertIn("/cart", disallows)
        self.assertNotIn("/", disallows)  # BadBot's rule is not for us (User-agent: *)
        self.assertTrue(gf._disallowed("/cart", disallows))
        self.assertTrue(gf._disallowed("/shop/filter_color", disallows))
        self.assertFalse(gf._disallowed("/product/calacatta", disallows))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
