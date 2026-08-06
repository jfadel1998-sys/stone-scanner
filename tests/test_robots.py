"""Tests for robots.txt enforcement.

Pure parser/decision tests — no network. The point of this file is that the rules
we obey are the ones RFC 9309 actually specifies, because "we honor robots.txt" is
a claim the project makes to suppliers, and a subtly wrong matcher would make it
false in exactly the cases that matter (a site that disallows everything *except*
one path, or that addresses us by name).

    python -m unittest tests.test_robots
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stonescan import robots  # noqa: E402


def policy(text: str, agent: str = robots.AGENT_TOKEN) -> robots.RobotsPolicy:
    return robots.RobotsPolicy.parse(text, agent)


class PathMatchingTests(unittest.TestCase):
    def test_simple_disallow_blocks_prefix_only(self):
        p = policy("User-agent: *\nDisallow: /private")
        self.assertFalse(p.allows("/private").allowed)
        self.assertFalse(p.allows("/private/deep/page").allowed)
        self.assertTrue(p.allows("/public").allowed)

    def test_empty_disallow_allows_everything(self):
        p = policy("User-agent: *\nDisallow:")
        self.assertTrue(p.allows("/anything").allowed)

    def test_disallow_root_blocks_all(self):
        p = policy("User-agent: *\nDisallow: /")
        self.assertFalse(p.allows("/").allowed)
        self.assertFalse(p.allows("/catalog/item/1").allowed)

    def test_wildcard_and_end_anchor(self):
        p = policy("User-agent: *\nDisallow: /*.pdf$")
        self.assertFalse(p.allows("/docs/spec.pdf").allowed)
        self.assertTrue(p.allows("/docs/spec.pdf.html").allowed)

    def test_query_string_is_part_of_the_match(self):
        p = policy("User-agent: *\nDisallow: /*?print=")
        self.assertFalse(p.allows("/item/5?print=1").allowed)
        self.assertTrue(p.allows("/item/5?page=1").allowed)


class PrecedenceTests(unittest.TestCase):
    """The longest matching pattern wins; Allow wins an equal-length tie."""

    def test_longer_allow_beats_shorter_disallow(self):
        # This is unbuilt.co's actual shape: the whole API is closed except the
        # one listings endpoint the provider is built on. Getting this backwards
        # would silently drop a working supplier.
        p = policy("User-agent: *\nDisallow: /api/\nAllow: /api/listings/")
        self.assertFalse(p.allows("/api/internal").allowed)
        self.assertTrue(p.allows("/api/listings/?q=dekton").allowed)

    def test_longer_disallow_beats_shorter_allow(self):
        p = policy("User-agent: *\nAllow: /catalog/\nDisallow: /catalog/private/")
        self.assertTrue(p.allows("/catalog/item").allowed)
        self.assertFalse(p.allows("/catalog/private/x").allowed)

    def test_equal_length_tie_goes_to_allow(self):
        p = policy("User-agent: *\nDisallow: /x/\nAllow: /x/")
        self.assertTrue(p.allows("/x/y").allowed)

    def test_decision_reports_the_responsible_rule(self):
        d = policy("User-agent: *\nDisallow: /secret/").allows("/secret/x")
        self.assertEqual(d.reason, robots.BLOCKED)
        self.assertIn("/secret/", d.rule)


class AgentGroupTests(unittest.TestCase):
    def test_named_group_overrides_wildcard(self):
        p = policy("User-agent: *\nDisallow: /\n\n"
                   f"User-agent: {robots.AGENT_TOKEN}\nDisallow: /admin/")
        self.assertTrue(p.allows("/catalog").allowed)     # our group, not the *
        self.assertFalse(p.allows("/admin/x").allowed)

    def test_named_group_can_be_stricter_than_wildcard(self):
        p = policy("User-agent: *\nDisallow: /admin/\n\n"
                   f"User-agent: {robots.AGENT_TOKEN}\nDisallow: /")
        self.assertFalse(p.allows("/catalog").allowed)

    def test_other_agents_group_is_ignored(self):
        p = policy("User-agent: SomeOtherBot\nDisallow: /\n\n"
                   "User-agent: *\nDisallow: /admin/")
        self.assertTrue(p.allows("/catalog").allowed)

    def test_consecutive_user_agents_share_one_block(self):
        p = policy(f"User-agent: OtherBot\nUser-agent: {robots.AGENT_TOKEN}\n"
                   "Disallow: /shared/")
        self.assertFalse(p.allows("/shared/x").allowed)

    def test_matching_is_case_insensitive(self):
        p = policy("User-agent: stonescanner\nDisallow: /")
        self.assertFalse(p.allows("/x").allowed)

    def test_no_matching_group_and_no_wildcard_allows_all(self):
        p = policy("User-agent: SomeOtherBot\nDisallow: /")
        self.assertTrue(p.allows("/anything").allowed)


class ParsingRobustnessTests(unittest.TestCase):
    def test_comments_and_blank_lines_and_case(self):
        p = policy("# hello\n\nUSER-AGENT: *\n  DISALLOW: /x   # trailing\n")
        self.assertFalse(p.allows("/x").allowed)

    def test_rule_before_any_user_agent_is_ignored(self):
        p = policy("Disallow: /\nUser-agent: *\nDisallow: /admin/")
        self.assertTrue(p.allows("/catalog").allowed)

    def test_sitemaps_and_crawl_delay_are_read(self):
        p = policy("Sitemap: https://x.test/sitemap.xml\n"
                   "User-agent: *\nCrawl-delay: 2.5\nDisallow: /x")
        self.assertEqual(p.sitemaps, ["https://x.test/sitemap.xml"])
        self.assertEqual(p.crawl_delay, 2.5)
        self.assertEqual(p.allows("/ok").crawl_delay, 2.5)

    def test_garbage_does_not_raise(self):
        for junk in ("", "<!doctype html><html>404</html>", "\x00\x01", "Disallow"):
            self.assertTrue(policy(junk).allows("/x").allowed)


class FetchSemanticsTests(unittest.TestCase):
    """RFC 9309 2.3.1: 4xx means no restrictions, 5xx means assume disallow."""

    def test_4xx_means_no_robots_file(self):
        d = robots._policy_from_response(404, "", robots.AGENT_TOKEN).allows("/x")
        self.assertTrue(d.allowed)
        self.assertEqual(d.reason, robots.NO_ROBOTS)

    def test_5xx_is_unreachable_not_blocked(self):
        d = robots._policy_from_response(503, "", robots.AGENT_TOKEN).allows("/x")
        self.assertFalse(d.allowed)
        # The distinction matters: UNREACHABLE is our failure and gets retried,
        # BLOCKED is the supplier's choice and must not be.
        self.assertEqual(d.reason, robots.UNREACHABLE)

    def test_200_is_parsed(self):
        d = robots._policy_from_response(200, "User-agent: *\nDisallow: /",
                                         robots.AGENT_TOKEN).allows("/x")
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, robots.BLOCKED)


class UrlHandlingTests(unittest.TestCase):
    def test_origin_split(self):
        self.assertEqual(robots.origin_of("https://a.test/x?y=1"), "https://a.test")
        self.assertEqual(robots.origin_of("a.test"), "https://a.test")
        self.assertEqual(robots.origin_of("http://a.test:8080/x"), "http://a.test:8080")

    def test_path_includes_query(self):
        self.assertEqual(robots._path_of("https://a.test/x?y=1"), "/x?y=1")
        self.assertEqual(robots._path_of("https://a.test"), "/")


class CacheTests(unittest.TestCase):
    def test_robots_txt_itself_is_always_fetchable(self):
        """Otherwise a `Disallow: /` would forbid reading the file that says so."""
        import asyncio

        class _Cache(robots.RobotsCache):
            async def _fetch(self, origin):  # never consulted for /robots.txt
                raise AssertionError("should not fetch to check /robots.txt")

        d = asyncio.run(_Cache().check("https://a.test/robots.txt"))
        self.assertTrue(d.allowed)

    def test_one_fetch_per_origin(self):
        import asyncio
        calls: list[str] = []

        class _Cache(robots.RobotsCache):
            async def _fetch(self, origin):
                calls.append(origin)
                return robots.RobotsPolicy.parse("User-agent: *\nDisallow: /no")

        async def go():
            c = _Cache()
            return await asyncio.gather(
                c.check("https://a.test/1"), c.check("https://a.test/2"),
                c.check("https://a.test/no"), c.check("https://b.test/1"),
            )

        d1, d2, d3, d4 = asyncio.run(go())
        self.assertEqual(sorted(calls), ["https://a.test", "https://b.test"])
        self.assertTrue(d1.allowed and d2.allowed and d4.allowed)
        self.assertFalse(d3.allowed)


class PoliteClientTests(unittest.TestCase):
    """The gate has to hold on the URL actually fetched, including redirect hops."""

    def _client(self, robots_txt: str, handler):
        import httpx

        def route(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=robots_txt,
                                      headers={"content-type": "text/plain"})
            return handler(request)

        return robots.PoliteClient(transport=httpx.MockTransport(route),
                                   follow_redirects=True)

    def test_allowed_request_passes(self):
        import asyncio

        import httpx

        async def go():
            async with self._client("User-agent: *\nDisallow: /no",
                                    lambda r: httpx.Response(200, text="ok")) as c:
                return await c.get("https://a.test/yes")

        self.assertEqual(asyncio.run(go()).text, "ok")

    def test_disallowed_request_raises_and_is_recorded(self):
        import asyncio

        import httpx

        async def go():
            async with self._client("User-agent: *\nDisallow: /no",
                                    lambda r: httpx.Response(200, text="leaked")) as c:
                with self.assertRaises(robots.Disallowed):
                    await c.get("https://a.test/no/thing")
                return c.blocked

        blocked = asyncio.run(go())
        self.assertEqual(len(blocked), 1)
        self.assertIn("/no", blocked[0][1].rule)

    def test_redirect_into_a_disallowed_path_is_caught(self):
        """The reason the check is a request hook and not a get() wrapper: httpx
        follows redirects internally, so only a per-hop check sees the real URL."""
        import asyncio

        import httpx

        def handler(request):
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "/private/data"})
            return httpx.Response(200, text="leaked")

        async def go():
            async with self._client("User-agent: *\nDisallow: /private", handler) as c:
                with self.assertRaises(robots.Disallowed) as cm:
                    await c.get("https://a.test/start")
                return cm.exception

        self.assertIn("/private", str(asyncio.run(go())))

    def test_cross_origin_redirect_is_checked_against_the_new_origin(self):
        import asyncio

        import httpx

        def route(request):
            if request.url.path == "/robots.txt":
                body = ("User-agent: *\nDisallow:" if request.url.host == "a.test"
                        else "User-agent: *\nDisallow: /")
                return httpx.Response(200, text=body,
                                      headers={"content-type": "text/plain"})
            if request.url.host == "a.test":
                return httpx.Response(302, headers={"location": "https://b.test/x"})
            return httpx.Response(200, text="leaked")

        async def go():
            async with robots.PoliteClient(transport=httpx.MockTransport(route),
                                           follow_redirects=True) as c:
                with self.assertRaises(robots.Disallowed) as cm:
                    await c.get("https://a.test/x")
                return cm.exception

        self.assertIn("b.test", str(asyncio.run(go())))

    def test_robots_fetch_is_exempt_from_its_own_rules(self):
        """A `Disallow: /` must not make robots.txt itself unfetchable."""
        import asyncio

        import httpx

        async def go():
            async with self._client("User-agent: *\nDisallow: /",
                                    lambda r: httpx.Response(200, text="x")) as c:
                with self.assertRaises(robots.Disallowed):
                    await c.get("https://a.test/anything")
                return await c.robots.policy("https://a.test/")

        self.assertFalse(asyncio.run(go()).allows("/x").allowed)


class OverrideTests(unittest.TestCase):
    """A reviewed exception must be narrow, recorded, and impossible to add by
    accident — otherwise it is just a hole in the guarantee."""

    ENTRY = {"host": "unbuilt.co",
             "robots_override": {"allow_paths": ["/api/listings"],
                                 "reason": "reviewed 2026-07-20"}}

    def test_missing_reason_is_a_hard_error(self):
        with self.assertRaises(ValueError):
            robots.Override.from_entry(
                {"host": "x.test", "robots_override": {"allow_paths": ["/a"]}})

    def test_absent_override_is_none(self):
        self.assertIsNone(robots.Override.from_entry({"host": "x.test"}))
        self.assertIsNone(robots.Override.from_entry({}))

    def test_scoped_to_its_own_host_and_paths(self):
        ov = robots.Override.from_entry(self.ENTRY)
        self.assertTrue(ov.covers("https://unbuilt.co/api/listings?q=x"))
        self.assertTrue(ov.covers("https://unbuilt.co/api/listings/"))
        # A different path on the same host is untouched...
        self.assertFalse(ov.covers("https://unbuilt.co/admin"))
        # ...and so is the same path on somebody else's host.
        self.assertFalse(ov.covers("https://elsewhere.test/api/listings"))

    def test_override_rescues_a_block_but_not_an_unreachable(self):
        import asyncio

        ov = robots.Override.from_entry(self.ENTRY)

        class _Blocked(robots.RobotsCache):
            async def _fetch(self, origin):
                return robots.RobotsPolicy.parse(
                    "User-agent: *\nDisallow: /api/\nAllow: /api/listings/")

        class _Down(robots.RobotsCache):
            async def _fetch(self, origin):
                return robots.RobotsPolicy.unreachable("HTTP 503")

        async def go():
            b = _Blocked(overrides=[ov])
            d1 = await b.check("https://unbuilt.co/api/listings?q=x")   # redirect form
            d2 = await b.check("https://unbuilt.co/api/secret")
            d3 = await _Down(overrides=[ov]).check("https://unbuilt.co/api/listings")
            return d1, d2, d3, b.overridden

        d1, d2, d3, used = asyncio.run(go())
        self.assertTrue(d1.allowed, "the reviewed endpoint should be reachable")
        self.assertFalse(d2.allowed, "the override must not open the rest of /api/")
        self.assertFalse(d3.allowed, "'could not ask' is not something a human approved")
        self.assertEqual(len(used), 1, "override use is recorded for auditing")

    def test_real_suppliers_json_overrides_all_parse(self):
        """Every override actually configured must be well-formed, so a typo'd or
        undocumented one fails here rather than silently doing nothing."""
        from stonescan import discover

        n = 0
        for entry in discover.load_suppliers():
            ov = robots.Override.from_entry(entry)   # raises if reason is missing
            if ov:
                n += 1
                self.assertTrue(ov.reason.strip())
                self.assertTrue(ov.hosts and all(ov.hosts))
        self.assertGreaterEqual(n, 1, "expected the reviewed unbuilt.co exception")


class BlockMarkerTests(unittest.TestCase):
    """A block must be distinguishable from a failure, or the nightly retry pass
    re-asks a supplier who already said no."""

    def test_block_errors_are_recognizable(self):
        self.assertTrue(robots.is_block_error(robots.block_error("x")))
        self.assertFalse(robots.is_block_error("timeout loading catalog"))
        self.assertFalse(robots.is_block_error(""))
        self.assertFalse(robots.is_block_error(None))

    def test_disallowed_exception_is_marked_and_catchable_as_httpx(self):
        import httpx as _httpx
        exc = robots.Disallowed("https://a.test/x",
                                robots.Decision(False, robots.BLOCKED, "Disallow: /x"))
        self.assertIsInstance(exc, _httpx.HTTPError)
        self.assertTrue(robots.is_block_error(str(exc)))

    def test_unreachable_is_not_marked_as_a_block(self):
        exc = robots.Disallowed("https://a.test/x",
                                robots.Decision(False, robots.UNREACHABLE, "HTTP 503"))
        self.assertFalse(robots.is_block_error(str(exc)))


class HtmlSniffTests(unittest.TestCase):
    """Every Stone Profits tenant answers /robots.txt with its SPA shell at 200."""

    def test_spa_shell_counts_as_no_robots_file(self):
        p = robots._policy_from_response(200, "<!DOCTYPE html><html><head>...",
                                         robots.AGENT_TOKEN)
        d = p.allows("/catalog")
        self.assertTrue(d.allowed)
        self.assertEqual(d.reason, robots.NO_ROBOTS)

    def test_real_robots_txt_is_still_parsed(self):
        p = robots._policy_from_response(200, "User-agent: *\nDisallow: /x",
                                         robots.AGENT_TOKEN)
        self.assertFalse(p.allows("/x").allowed)

    def test_sniffer_does_not_eat_a_leading_comment(self):
        self.assertFalse(robots.looks_like_html("# robots\nUser-agent: *\nDisallow: /"))


class DenylistTests(unittest.TestCase):
    """A removal request has to survive the next discovery run."""

    def setUp(self):
        import tempfile

        from stonescan import denylist

        self.dl = denylist
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self._old_dl = denylist.DENYLIST_FILE
        denylist.DENYLIST_FILE = root / "denylist.json"
        self.addCleanup(setattr, denylist, "DENYLIST_FILE", self._old_dl)

        from stonescan import discover

        self.discover = discover
        self._old_sup = discover.SUPPLIERS_FILE
        discover.SUPPLIERS_FILE = root / "suppliers.json"
        discover.SUPPLIERS_FILE.write_text(json.dumps({"suppliers": [
            {"host": "keep.example.com", "name": "Keep"},
            {"host": "gone.example.com", "name": "Gone"},
        ]}), encoding="utf-8")
        self.addCleanup(setattr, discover, "SUPPLIERS_FILE", self._old_sup)

    def test_add_denies_host_and_subdomains(self):
        self.dl.add("gone.example.com", "asked us to stop")
        self.assertTrue(self.dl.is_denied("gone.example.com"))
        self.assertTrue(self.dl.is_denied("inventory.gone.example.com"))
        self.assertTrue(self.dl.is_denied("https://www.gone.example.com/x"))
        self.assertFalse(self.dl.is_denied("keep.example.com"))
        # A neighbouring domain that merely ends with the same text is NOT denied.
        self.assertFalse(self.dl.is_denied("notgone.example.com"))

    def test_add_prunes_the_supplier_list(self):
        self.dl.add("gone.example.com", "asked us to stop")
        hosts = [s["host"] for s in json.loads(
            self.discover.SUPPLIERS_FILE.read_text(encoding="utf-8"))["suppliers"]]
        self.assertEqual(hosts, ["keep.example.com"])

    def test_discovery_does_not_resurrect_a_denied_host(self):
        """The actual bug: absence from suppliers.json is not a removal."""
        self.dl.add("gone.example.com", "asked us to stop")
        added = self.discover.merge_discovered(
            {"gone.example.com": None, "inventory.gone.example.com": None,
             "fresh.example.com": None})
        hosts = [s["host"] for s in json.loads(
            self.discover.SUPPLIERS_FILE.read_text(encoding="utf-8"))["suppliers"]]
        self.assertEqual(added, 1)
        self.assertIn("fresh.example.com", hosts)
        self.assertNotIn("gone.example.com", hosts)
        self.assertNotIn("inventory.gone.example.com", hosts)

    def test_slabcloud_merge_honors_the_denylist(self):
        self.dl.add("gone.example.com", "asked us to stop")
        added = self.discover.merge_slabcloud([
            {"host": "gone.example.com", "slug": "g", "name": "G", "provider": "slabcloud"},
            {"host": "ok.slabcloud.com", "slug": "o", "name": "O", "provider": "slabcloud"},
        ])
        self.assertEqual(added, 1)

    def test_filter_entries_splits_crawlable_from_denied(self):
        self.dl.add("gone.example.com")
        keep, drop = self.dl.filter_entries(
            [{"host": "keep.example.com"}, {"host": "api.gone.example.com"}])
        self.assertEqual([e["host"] for e in keep], ["keep.example.com"])
        self.assertEqual([e["host"] for e in drop], ["api.gone.example.com"])

    def test_empty_denylist_denies_nothing(self):
        keep, drop = self.dl.filter_entries([{"host": "a.example.com"}])
        self.assertEqual(len(keep), 1)
        self.assertEqual(drop, [])

    def test_remove_undoes_a_denial(self):
        self.dl.add("gone.example.com")
        self.assertTrue(self.dl.remove("gone.example.com"))
        self.assertFalse(self.dl.is_denied("gone.example.com"))

    def test_corrupt_denylist_raises_rather_than_silently_allowing(self):
        self.dl.DENYLIST_FILE.write_text("{not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.dl.load()


class PurgeTests(unittest.TestCase):
    """Denying a host must actually erase its collected data — otherwise a supplier
    who asked to be removed stays searchable forever."""

    def setUp(self):
        import tempfile

        from stonescan import db

        self.db = db
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "t.db")
        conn = db.init_db(self.path)
        # Cleanups run LIFO: register the dir removal first and the connection close
        # second, so the connection closes BEFORE the dir is unlinked — Windows can't
        # delete a .db file that still has an open handle.
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.conn.close())
        sid = db.upsert_supplier(conn, host="gone.example.com", company="Gone Co",
                                 phone="555-1000", email="x@gone.example.com")
        keep = db.upsert_supplier(conn, host="keep.example.com", company="Keep Co")
        db.replace_materials(conn, sid, [
            {"supplier_id": sid, "item_id": f"i{i}", "item_name": f"Stone {i}",
             "name_norm": f"STONE {i}", "material_key": f"stone {i}|granite",
             "material_type": "Granite", "available_slabs": 3,
             "image_url": f"http://img/{i}.jpg", "crawled_at": "2026-07-19"}
            for i in range(5)])
        db.replace_materials(conn, keep, [
            {"supplier_id": keep, "item_id": "k1", "item_name": "Keep Stone",
             "name_norm": "KEEP STONE", "material_key": "keep|granite",
             "material_type": "Granite", "available_slabs": 2,
             "image_url": "http://img/keep.jpg", "crawled_at": "2026-07-19"}])
        db.replace_slabs(conn, sid, [
            {"supplier_id": sid, "item_id": "i0", "slab_no": "A", "crawled_at": "2026-07-19"}],
            "2026-07-19")
        db.snapshot_history(conn, sid, "2026-07-19")
        conn.execute("INSERT INTO image_vectors (image_url, vec) VALUES (?, ?)",
                     ("http://img/0.jpg", b"\x00" * 8))
        conn.execute("INSERT INTO image_vectors (image_url, vec) VALUES (?, ?)",
                     ("http://img/keep.jpg", b"\x00" * 8))
        conn.commit()
        self.sid, self.keep = sid, keep
        self.conn = conn

    def _count(self, table, sid):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE supplier_id = ?", (sid,)).fetchone()[0]

    def test_purge_erases_only_the_target(self):
        res = self.db.purge_supplier(self.conn, "gone.example.com")
        self.assertEqual(res["materials"], 5)
        self.assertEqual(res["slabs"], 1)
        self.assertEqual(self._count("materials", self.sid), 0)
        self.assertEqual(self._count("history", self.sid), 0)
        # The other supplier is untouched.
        self.assertEqual(self._count("materials", self.keep), 1)

    def test_purge_removes_derived_image_vectors_but_not_others(self):
        self.db.purge_supplier(self.conn, "gone.example.com")
        urls = {r[0] for r in self.conn.execute("SELECT image_url FROM image_vectors")}
        self.assertNotIn("http://img/0.jpg", urls)   # was the gone supplier's
        self.assertIn("http://img/keep.jpg", urls)   # the other supplier's survives

    def test_purge_scrubs_contact_details_but_keeps_the_row(self):
        self.db.purge_supplier(self.conn, "gone.example.com")
        r = self.conn.execute(
            "SELECT item_count, company, phone, email FROM suppliers WHERE id = ?",
            (self.sid,)).fetchone()
        self.assertEqual(r["item_count"], 0)
        self.assertIsNone(r["company"])
        self.assertIsNone(r["phone"])
        # Row kept (list_items FK depends on it); zeroed, not deleted.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM suppliers WHERE id = ?", (self.sid,)).fetchone()[0], 1)

    def test_purge_unknown_host_is_a_noop(self):
        res = self.db.purge_supplier(self.conn, "nosuch.example.com")
        self.assertEqual(res["materials"], 0)

    def test_purge_data_matches_subdomains(self):
        from stonescan import denylist
        # Deny the apex; a supplier stored under a subdomain must still be purged.
        sub = self.db.upsert_supplier(self.conn, host="inventory.gone.example.com",
                                      company="Sub")
        self.db.replace_materials(self.conn, sub, [
            {"supplier_id": sub, "item_id": "s1", "item_name": "S", "name_norm": "S",
             "material_key": "s|granite", "material_type": "Granite",
             "available_slabs": 1, "crawled_at": "2026-07-19"}])
        self.conn.commit()
        totals = denylist.purge_data("gone.example.com", self.path)
        # both gone.example.com and inventory.gone.example.com match the apex
        self.assertEqual(totals["suppliers"], 2)
        self.assertGreaterEqual(totals["materials"], 6)


class ChallengeDetectionTests(unittest.TestCase):
    """AIL-33: 192 slabware tenants were filed as BROKEN for answering with a Cloudflare
    managed challenge. That is the supplier declining automated access — the same statement
    robots.txt makes, said out of band — and it must be recorded, never circumvented."""

    def test_the_cloudflare_header_is_the_primary_signal(self):
        from stonescan import challenge
        self.assertTrue(challenge.detect(403, {"cf-mitigated": "challenge"}))
        self.assertTrue(challenge.detect(403, {"CF-Mitigated": "Challenge"}))  # case-insensitive

    def test_a_plain_403_is_not_a_challenge(self):
        # The asymmetry that matters. A false positive silently stops crawling a supplier who
        # is merely erroring; a false negative just leaves today's behaviour. So a bare 403 —
        # even from a Cloudflare-fronted origin — is NOT enough on its own.
        from stonescan import challenge
        self.assertFalse(challenge.detect(403, {}))
        self.assertFalse(challenge.detect(403, {"server": "cloudflare"}))
        self.assertFalse(challenge.detect(403, {"server": "Microsoft-IIS/10.0"}))
        self.assertFalse(challenge.detect(200, {"cf-mitigated": "challenge"}) == "")  # 200+header still counts

    def test_the_body_rule_needs_both_halves(self):
        from stonescan import challenge
        self.assertTrue(challenge.detect(403, {"server": "cloudflare"},
                                         "<title>Just a moment...</title>"))
        self.assertFalse(challenge.detect(403, {"server": "cloudflare"}, "Access denied"))
        self.assertFalse(challenge.detect(403, {"server": "nginx"},
                                          "<title>Just a moment...</title>"))

    def test_the_marker_survives_the_stored_exception_form(self):
        # Providers store f"{type(e).__name__}: {e}", so the marker lands mid-string. The old
        # is_block_error used startswith and therefore never matched a stored robots block —
        # a live latent bug, unexercised only because 0 of 393 hosts publish a Disallow.
        from stonescan import robots
        from stonescan.challenge import Challenged
        c = Challenged("https://x/y", "bot-protection challenge")
        stored = f"{type(c).__name__}: {c}"
        self.assertTrue(robots.is_challenge_error(stored))
        self.assertTrue(robots.is_declined(stored))
        self.assertFalse(robots.is_block_error(stored), "a challenge is not a robots block")

        d = robots.Disallowed("https://x/y", robots.Decision(False, robots.BLOCKED, "Disallow: /"))
        stored_block = f"{type(d).__name__}: {d}"
        self.assertTrue(robots.is_block_error(stored_block),
                        "the stored form of a real robots block must match")
        self.assertTrue(robots.is_declined(stored_block))

    def test_the_response_hook_raises_without_reading_the_body(self):
        # httpx fires response hooks on an UNREAD response; touching .text there would force
        # a read and break client.stream() for every provider.
        import asyncio

        import httpx

        from stonescan import robots
        from stonescan.challenge import Challenged

        async def go():
            def handler(request):
                if request.url.path == "/robots.txt":
                    return httpx.Response(404, text="nope")
                return httpx.Response(403, headers={"cf-mitigated": "challenge",
                                                    "server": "cloudflare"},
                                      text="<title>Just a moment...</title>")
            transport = httpx.MockTransport(handler)
            async with robots.PoliteClient(transport=transport) as c:
                with self.assertRaises(Challenged) as ctx:
                    await c.get("https://tenant.example.com/FullInventory.aspx")
                return ctx.exception

        exc = asyncio.run(go())
        self.assertIn("challenge-blocked:", str(exc))
        self.assertTrue(isinstance(exc, httpx.HTTPError),
                        "must subclass HTTPError so providers' existing handlers catch it")

    def test_a_normal_response_passes_through_untouched(self):
        import asyncio

        import httpx

        from stonescan import robots

        async def go():
            def handler(request):
                if request.url.path == "/robots.txt":
                    return httpx.Response(404, text="nope")
                return httpx.Response(200, headers={"server": "Microsoft-IIS/10.0"},
                                      text="ok")
            async with robots.PoliteClient(transport=httpx.MockTransport(handler)) as c:
                r = await c.get("https://good.example.com/FullInventory.aspx")
                return r.status_code, r.text

        self.assertEqual(asyncio.run(go()), (200, "ok"))

    def test_nothing_tries_to_get_past_a_challenge(self):
        # A standing guard, not a one-off assertion. The whole point of this feature is that
        # it recognises and stops; a future change that routes a challenged host through a
        # browser, replays a clearance cookie, or retries until it passes would defeat the
        # supplier's decision. Keep the module free of the machinery that would enable it.
        # Checks EXECUTABLE code, not prose. A first cut grepped the raw text and failed on
        # this module's own docstring, which says Playwright must not be used here — a
        # prohibition is not machinery, and a guard that cannot tell them apart would push
        # the next author to delete the warning rather than obey it.
        import ast
        from pathlib import Path
        tree = ast.parse(Path("stonescan/challenge.py").read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                d = ast.get_docstring(node, clean=False)
                if d:
                    docstrings.add(d)
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                used.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                used.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value not in docstrings:
                    used.add(node.value)
        blob = " ".join(used).lower()
        for forbidden in ("playwright", "cf_clearance", "clearance", "solver",
                          "webdriver", "selenium", "undetected", "sleep"):
            self.assertNotIn(forbidden, blob,
                             f"challenge.py must not USE {forbidden!r} — it recognises a "
                             f"challenge and stops; it never tries to get past one")


if __name__ == "__main__":
    unittest.main(verbosity=2)
