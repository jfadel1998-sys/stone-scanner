"""Tests for the stone reference and photo identification.

The point of this file is the honesty guarantee. Origin, quarry lists and market
prices cannot be derived from the catalog (measured: `origin` is 41/44 US warehouse
cities, `price_range` is mostly supplier tier codes), so they come from external
research — which makes "a plausible-sounding invention" the single most likely way
this feature goes wrong. These tests assert that an unsourced fact cannot reach the
file or the UI, and that identification reports agreement it can actually support.

    python -m unittest tests.test_reference
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stonescan import imagesearch, reference  # noqa: E402

SRC = [{"url": "https://example.test/a", "title": "A source"}]


def entry(**over):
    base = {
        "key": "taj mahal|quartzite",
        "display_name": "Taj Mahal",
        "formation": "natural",
        "stone_type": "Quartzite",
        "summary": "A pale Brazilian quartzite.",
        "origin": {"country": "Brazil", "region": "Espírito Santo",
                   "confidence": "high", "sources": SRC},
        "price_per_sqft": {"low": 60, "high": 100, "currency": "USD",
                           "basis": "installed", "confidence": "medium", "sources": SRC},
        "quarries": [{"name": "Quarry One", "region": "ES", "country": "Brazil",
                      "confidence": "medium", "sources": SRC}],
    }
    base.update(over)
    return base


class UnsourcedFactsTests(unittest.TestCase):
    """An unsourced fact must not be storable or displayable — this is the
    guarantee the whole feature rests on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = reference.REFERENCE_FILE
        reference.REFERENCE_FILE = Path(self.tmp.name) / "ref.json"
        self.addCleanup(setattr, reference, "REFERENCE_FILE", self._old)

    def test_import_strips_facts_that_have_no_source(self):
        added, _, notes = reference.import_entries([entry(
            origin={"country": "Brazil", "confidence": "high", "sources": []},
            price_per_sqft={"low": 1, "high": 2, "confidence": "high", "sources": []},
            quarries=[{"name": "Unsourced Quarry", "confidence": "high", "sources": []}],
        )])
        self.assertEqual(added, 1)
        s = reference.summarize(reference.lookup(material_key="taj mahal|quartzite"))
        self.assertIsNone(s["origin"], "an origin with no source must not be stored")
        self.assertIsNone(s["price"])
        self.assertEqual(s["quarries"], [])
        self.assertEqual(sorted(s["gaps"]), ["origin", "price", "quarries"])
        self.assertTrue(notes, "stripping must be reported, not silent")

    def test_sourced_facts_survive_intact(self):
        reference.import_entries([entry()])
        s = reference.summarize(reference.lookup(name="Taj Mahal"))
        self.assertEqual(s["origin"]["country"], "Brazil")
        self.assertEqual(s["origin_text"], "Espírito Santo, Brazil")
        self.assertEqual(len(s["quarries"]), 1)
        self.assertEqual(s["gaps"], [])

    def test_has_fact_requires_both_value_and_source(self):
        self.assertFalse(reference.has_fact({"country": "Brazil", "sources": []}))
        self.assertFalse(reference.has_fact({"country": "", "sources": SRC,
                                             "confidence": "high"}))
        self.assertFalse(reference.has_fact({"country": "Brazil", "sources": SRC,
                                             "confidence": "unknown"}))
        self.assertTrue(reference.has_fact({"country": "Brazil", "sources": SRC,
                                            "confidence": "low"}))

    def test_price_is_never_shown_without_its_basis_being_available(self):
        """$60-100 means very different things installed vs slab-only, so the
        rendered label always carries the basis when one is known."""
        self.assertIn("installed", reference.price_label(
            {"low": 60, "high": 100, "basis": "installed", "confidence": "high",
             "sources": SRC}))
        self.assertEqual(reference.price_label({"confidence": "unknown", "sources": []}), "")

    def test_corrupt_file_raises_rather_than_reading_as_empty(self):
        reference.REFERENCE_FILE.write_text("{not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            reference.load()


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = reference.REFERENCE_FILE
        reference.REFERENCE_FILE = Path(self.tmp.name) / "ref.json"
        self.addCleanup(setattr, reference, "REFERENCE_FILE", self._old)
        reference.import_entries([entry(also_known_as=["Taj Mahal Quartzite"])])

    def test_matches_on_trade_name_across_a_type_disagreement(self):
        """Suppliers disagree about type constantly — the same rock is filed as
        marble by one and quartzite by another. One researched entry must serve
        both, or the panel would go blank depending on who listed it."""
        self.assertIsNotNone(reference.lookup(material_key="taj mahal|marble"))
        self.assertIsNotNone(reference.lookup(material_key="taj mahal|quartzite"))

    def test_matches_aliases_and_is_punctuation_insensitive(self):
        self.assertIsNotNone(reference.lookup(name="taj-mahal"))
        self.assertIsNotNone(reference.lookup(name="TAJ MAHAL"))
        self.assertIsNotNone(reference.lookup(name="Taj Mahal Quartzite"))

    def test_unknown_stone_returns_none_not_a_guess(self):
        self.assertIsNone(reference.lookup(material_key="nonesuch|granite"))
        self.assertFalse(reference.summarize(None)["known"])


class IdentifyTests(unittest.TestCase):
    """Identification must report the agreement it can support — and search()
    dedupes to one row per material, so the counts have to come from the raw
    image ranking, not from the deduped list."""

    @staticmethod
    def row(key, score, matches, suppliers, window=40):
        return {"material_key": key, "item_name": key.split("|")[0].title(),
                "material_type": key.split("|")[1].title(), "score": score,
                "image_matches": matches, "distinct_suppliers": suppliers,
                "window_total": window, "supplier_name": "S"}

    def test_cross_supplier_agreement_beats_a_lone_better_score(self):
        res = [self.row("lonely|granite", 0.86, 1, 1),
               self.row("agreed|granite", 0.84, 14, 9)]
        best = imagesearch.identify(res)["best"]
        self.assertEqual(best["material_key"], "agreed|granite",
                         "nine suppliers agreeing outweighs one slightly closer image")

    def test_one_supplier_with_many_photos_is_not_nine_opinions(self):
        many_one = [self.row("prolific|granite", 0.84, 14, 1),
                    self.row("spread|granite", 0.84, 8, 8)]
        best = imagesearch.identify(many_one)["best"]
        self.assertEqual(best["material_key"], "spread|granite",
                         "14 photos from one supplier is one opinion, not 14")

    def test_near_exact_match_is_strong_even_from_a_single_supplier(self):
        res = [self.row("exact|quartzite", 0.99, 1, 1),
               self.row("other|granite", 0.61, 1, 1)]
        self.assertEqual(imagesearch.identify(res)["confidence"], "strong")

    def test_disagreement_reports_uncertain(self):
        res = [self.row("a|granite", 0.66, 1, 1), self.row("b|granite", 0.655, 1, 1),
               self.row("c|granite", 0.65, 1, 1)]
        out = imagesearch.identify(res)
        self.assertEqual(out["confidence"], "uncertain")
        self.assertGreaterEqual(len(out["candidates"]), 2, "alternates must be offered")

    def test_no_matches_is_not_an_identification(self):
        self.assertFalse(imagesearch.identify([])["known"])
        self.assertFalse(imagesearch.identify([{"score": 0.9}])["known"])


class PriceParsingTests(unittest.TestCase):
    """Only two shapes in `price_range` are real money, and they mean different
    things; everything else is a supplier tier code and must not become a price."""

    def _rows(self, *vals):
        return [{"price_range": v, "supplier_name": "S"} for v in vals]

    def setUp(self):
        from stonescan.web import app
        self.fn = app._observed_prices

    def test_tier_codes_never_become_money(self):
        out = self.fn(self._rows("Group 5", "Level 2", "$", "$$$", "A - Low",
                                 "Caesarstone B - Standard", ""))
        self.assertIsNone(out["per_sqft"])
        self.assertIsNone(out["slab_total"])

    def test_per_sqft_and_slab_totals_are_kept_apart(self):
        out = self.fn(self._rows("$11.9/sf", "$41.65/sf", "$570", "$794"))
        self.assertEqual(out["per_sqft"]["n"], 2)
        self.assertAlmostEqual(out["per_sqft"]["low"], 11.9)
        self.assertEqual(out["slab_total"]["n"], 2)
        self.assertAlmostEqual(out["slab_total"]["high"], 794)

    def test_commas_and_spacing_parse(self):
        out = self.fn(self._rows("$1,003", "$ 2.50 / sq ft"))
        self.assertAlmostEqual(out["slab_total"]["low"], 1003)
        self.assertAlmostEqual(out["per_sqft"]["low"], 2.5)


class LiveGateTests(unittest.TestCase):
    """lookup_live must not attach a same-named non-stone (or wrong-stone) page.
    These are the exact false matches a bare rock-word gate let through — a
    commercial name search hits bands, monuments and generic articles."""

    def test_rejects_same_named_nonmatches(self):
        self.assertFalse(reference._is_about(
            "Black Galaxy", "Morton Gneiss",
            "Morton gneiss, also known as rainbow gneiss, is an Archean-age gneiss."))
        self.assertFalse(reference._is_about(
            "Taj Mahal", "Stones of India",
            "India possesses a wide spectrum of dimensional stones including granite, marble."))
        self.assertFalse(reference._is_about(
            "Absolute Black", "Deafheaven", "Deafheaven is an American blackgaze rock band."))

    def test_requires_rock_words_even_when_the_name_matches(self):
        # The place "New Caledonia" is titled exactly right but is not about stone.
        self.assertFalse(reference._is_about(
            "New Caledonia", "New Caledonia",
            "New Caledonia is a French overseas territory in the southwest Pacific."))

    def test_accepts_a_genuine_stone_page(self):
        self.assertTrue(reference._is_about(
            "Taj Mahal", "Taj Mahal (quartzite)",
            "Taj Mahal is a quartzite quarried in Espirito Santo, Brazil."))


def brand_entry(name, maker, stype="Quartz", aka=None):
    """An engineered BRAND entry: a manufacturer instead of quarries."""
    return {
        "key": f"{name.lower()}|{stype.lower()}",
        "display_name": name,
        "formation": "engineered",
        "stone_type": stype,
        "manufacturer": maker,
        "also_known_as": aka or [],
        "summary": f"{name} is an engineered surface made by {maker}.",
        "origin": {"confidence": "unknown", "sources": []},
        "price_per_sqft": {"confidence": "unknown", "sources": []},
        "quarries": [],
    }


class EngineeredBrandTests(unittest.TestCase):
    """Engineered surfaces are branded product lines, so one entry must answer for every
    colour in that line — without ever outranking a stone's own researched entry."""

    def setUp(self):
        self.stones = [
            entry(),                                                    # natural, per-stone
            brand_entry("Silestone", "Cosentino"),
            brand_entry("Dekton", "Cosentino", "Sintered Stone"),
            brand_entry("Viatera", "LX Hausys", aka=["LX Viatera"]),
        ]

    def _lookup(self, name="", key=""):
        return reference.lookup(material_key=key, name=name, stones=self.stones)

    def test_brand_named_in_a_colour_resolves(self):
        for name in ("Calacatta Gold Silestone", "SILESTONE Blanco Maple 3cm"):
            hit = self._lookup(name=name)
            self.assertIsNotNone(hit, name)
            self.assertEqual(hit["display_name"], "Silestone", name)
        self.assertEqual(self._lookup(name="Dekton Aura 12mm")["display_name"], "Dekton")

    def test_brand_matches_from_the_material_key_too(self):
        hit = self._lookup(key="dekton keranium|sintered stone")
        self.assertEqual(hit["display_name"], "Dekton")

    def test_multiword_alias_wins_over_a_shorter_one(self):
        hit = self._lookup(name="LX Viatera Minuet 2cm")
        self.assertEqual(hit["display_name"], "Viatera")

    def test_a_stones_own_entry_beats_a_brand_mention(self):
        # The exact-name match must win, otherwise a researched stone would be
        # overwritten by any brand word that happens to appear beside it.
        self.assertEqual(self._lookup(name="Taj Mahal")["display_name"], "Taj Mahal")

    def test_unrelated_names_match_nothing(self):
        self.assertIsNone(self._lookup(name="Absolute Black"))
        self.assertIsNone(self._lookup(name="Blue Bahia Granite"))

    def test_engineered_entry_carries_a_maker_not_quarries(self):
        s = reference.summarize(brand_entry("Cambria", "Cambria USA"))
        self.assertEqual(s["manufacturer"], "Cambria USA")
        self.assertEqual(s["quarries"], [])
        self.assertEqual(s["origin_text"], "")      # honest blank, not an invented country


class CategoryPriceBandTests(unittest.TestCase):
    """A category price band is a claim about the world, so it obeys the same rule as
    every other fact here: no source, no band. And it must never be confused with the
    money actually observed in supplier catalogs."""

    def setUp(self):
        self._old = dict(reference._PRICE_BY_TYPE)
        self.addCleanup(lambda: reference._PRICE_BY_TYPE.update(self._old))

    def test_sourced_band_is_returned_with_its_basis(self):
        reference._PRICE_BY_TYPE["testite"] = {
            "low": 50, "high": 100, "currency": "USD", "basis": "installed",
            "confidence": "medium", "sources": SRC}
        band = reference.price_band_by_type("Testite")
        self.assertEqual((band["low"], band["high"]), (50, 100))
        label = reference.price_band_label(band)
        self.assertIn("/ sq ft", label)
        self.assertIn("installed", label, "basis must always be shown — installed is ~2x slab-only")

    def test_unsourced_band_is_withheld(self):
        reference._PRICE_BY_TYPE["nosource"] = {
            "low": 1, "high": 2, "currency": "USD", "basis": "installed",
            "confidence": "high", "sources": []}
        self.assertEqual(reference.price_band_by_type("nosource"), {})

    def test_unknown_category_returns_nothing_not_a_zero(self):
        self.assertEqual(reference.price_band_by_type("unobtainium"), {})
        self.assertEqual(reference.price_band_by_type(""), {})
        self.assertEqual(reference.price_band_label({}), "")

    def test_band_is_separate_from_catalog_observed_prices(self):
        # The two tiers answer different questions ("what the market charges" vs "what
        # this catalog lists"), so they must not share a shape that invites merging.
        from stonescan.web.app import _observed_prices
        rows = [{"price_range": "$11.90/sf", "supplier_name": "S1"}]
        observed = _observed_prices(rows)
        self.assertIn("per_sqft", observed)
        reference._PRICE_BY_TYPE["testite"] = {
            "low": 50, "high": 100, "currency": "USD", "basis": "installed",
            "confidence": "medium", "sources": SRC}
        band = reference.price_band_by_type("testite")
        self.assertNotIn("per_sqft", band)
        self.assertNotIn("slab_total", band)
        self.assertIn("sources", band, "a band always carries its citation; observed prices never do")


if __name__ == "__main__":
    unittest.main(verbosity=2)
