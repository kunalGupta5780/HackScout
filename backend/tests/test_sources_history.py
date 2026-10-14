import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import app.main as main

PAGE = ("{name} is an online hackathon for students. Registration deadline: 30 December 2026. "
        "Students are eligible. Prizes and team registration are on this page.")

URLS = {
    "unstop": "https://unstop.com/hackathons/nimbus-ai-build-sprint-2026-901",
    "devfolio": "https://devfolio.co/hackathons/chainforge-web3-hack-2026",
    "mlh": "https://mlh.io/events/prompt-relay-24h-hackathon-2026",
}


BLOG_URL = "https://whereuelevate.com/blogs/smart-india-hackathon-2026"
BLOG_TITLE = "Smart India Hackathon 2026: Registration, Eligibility & Internal Hackathon Rules Most People Skip"


def fake_rows(query: str, page: int = 0):
    if page:
        return []
    if "Smart India" in query:
        return [{"url": BLOG_URL, "title": BLOG_TITLE, "snippet": "Hackathon registration eligibility guide", "position": 1}]
    for key, url in URLS.items():
        if key in query:
            return [{"url": url, "title": f"{key.title()} Sample Hackathon 2026", "snippet": "Hackathon registration open", "position": 1}]
    return []


def fake_fetch_pages(urls):
    return {main.canonical_url(u): {"text": (PAGE.format(name=u.rsplit("/", 1)[1].replace("-", " ").title())
                                             if u != BLOG_URL else "Smart India Hackathon 2026 guide. Registration, eligibility and rules. 8 min read. Published on 1 Oct. Written by an editor."),
                                    "title": u.rsplit("/", 1)[1].replace("-", " ").title(), "final_url": u, "links": []}
            for u in urls}


class SourcesAndHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(main, "DB_PATH", Path(self.tmp.name) / "t.db")]
        for p in self.patches:
            p.start()
        main.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # ---- registry / platform detection --------------------------------------------------
    def test_registry_includes_the_requested_platforms(self):
        keys = {s["key"] for s in main.EVENT_SOURCES}
        self.assertTrue({"unstop", "devfolio", "mlh", "devto", "devpost", "hackerearth"} <= keys)
        self.assertGreaterEqual(len(main.EVENT_SOURCES), 15)
        for src in main.EVENT_SOURCES:
            self.assertTrue(all("{year}" in q for q in src["queries"]), src["key"])

    def test_platform_info(self):
        self.assertEqual(main.platform_info("https://unstop.com/hackathons/x"), ("unstop", "Unstop"))
        self.assertEqual(main.platform_info("https://www.devfolio.co/x")[1], "Devfolio")
        self.assertEqual(main.platform_info("https://dev.to/a/b")[0], "devto")
        self.assertEqual(main.platform_info("https://college.edu/hack")[1], "college.edu")
        self.assertEqual(main.platform_info("https://college.edu/hack", group_unknown=True), ("other", "Other websites"))

    # ---- search across platforms --------------------------------------------------------
    def test_every_platform_is_queried_and_failures_are_isolated(self):
        def fake_search(query, page=0):
            if "devfolio" in query:
                raise HTTPException(502, "TinyFish rate limit reached. Wait before scanning again.")
            return fake_rows(query, page)
        with patch.object(main, "fetch_search", side_effect=fake_search) as search:
            results, errors, stats = main.search_event_sources(2026)
        expected = sum(len(s["queries"]) * (1 + (main.SCAN_EXTRA_PAGES if s["key"] in main.BUSY_SOURCE_KEYS else 0))
                       for s in main.ACTIVE_SOURCES)
        self.assertEqual(search.call_count, expected)
        self.assertGreater(expected, sum(len(s["queries"]) for s in main.ACTIVE_SOURCES))  # busy sites get a 2nd page
        self.assertEqual({r["url"] for r in results}, {URLS["unstop"], URLS["mlh"], BLOG_URL})  # search returns the blog; filtering happens later
        self.assertEqual(len(errors), 1)
        self.assertTrue(stats["devfolio"]["errors"])
        self.assertFalse(stats["unstop"]["errors"])

    def test_all_searches_failing_raises(self):
        with patch.object(main, "fetch_search", side_effect=HTTPException(502, "down")):
            with self.assertRaises(HTTPException):
                main.search_event_sources(2026)

    def test_shortlist_shares_slots_between_platforms(self):
        raw = ([{"url": f"https://unstop.com/hackathons/event-{i}", "title": "Hackathon", "position": i} for i in range(12)]
               + [{"url": f"https://devfolio.co/hackathons/e-{i}", "title": "Hackathon", "position": i} for i in range(3)]
               + [{"url": "https://mlh.io/events/one", "title": "Hackathon"}, {"url": "https://devpost.com/hackathons/two", "title": "Hackathon"},
                  {"url": "https://unstop.com/hackathons/event-1?utm_source=x", "title": "dup"}])
        picked, keys = main.build_shortlist(raw, 8)
        platforms = [main.platform_info(i["url"])[0] for i in picked]
        self.assertEqual(len(picked), 8)
        self.assertTrue({"unstop", "devfolio", "mlh", "devpost"} <= set(platforms))
        self.assertLessEqual(platforms.count("unstop"), 3)
        self.assertEqual(len(keys), len({main.canonical_url(i["url"]) for i in picked}))

    # ---- upsert outcomes ----------------------------------------------------------------
    def _event(self, **kw):
        base = {"title": "Sample Hackathon", "url": URLS["unstop"], "source": "unstop.com", "snippet": "s", "deadline": "2026-12-30",
                "format": "Online", "eligibility": "Students", "tags": ["AI/ML"], "verification": "page_fetched",
                "registration_status": "open", "status_reason": "", "extraction_provider": "rules"}
        return {**base, **kw}

    def test_upsert_reports_new_seen_updated(self):
        out = {}
        self.assertEqual(main.upsert_events([self._event()], out), (1, 0))
        self.assertEqual(out[URLS["unstop"]], "new")
        out = {}
        self.assertEqual(main.upsert_events([self._event()], out), (0, 0))
        self.assertEqual(out[URLS["unstop"]], "seen")
        out = {}
        self.assertEqual(main.upsert_events([self._event(deadline="2027-01-15")], out), (0, 1))
        self.assertEqual(out[URLS["unstop"]], "updated")

    # ---- history storage ----------------------------------------------------------------
    def _save(self, n):
        rec = {"url": URLS["unstop"], "title": "T", "platform_key": "unstop", "platform_label": "Unstop", "outcome": "new",
               "deadline": "2026-12-30", "match_score": 70, "tags": ["AI/ML"], "snippet": "x"}
        return main.save_scan_history(mode="live", provider="rules", gemini_model=None, duration=1.2, queries=3, platforms=[{"key": "unstop", "label": "Unstop", "results": 2, "kept": 1}],
                                      candidates=2, fetched=2, new_count=n, updated_count=0, errors=[], note="n", records=[rec])

    def test_history_roundtrip_and_retention(self):
        with patch.object(main, "SCAN_HISTORY_KEEP", 2):
            ids = [self._save(i) for i in range(3)]
        listed = main.list_scan_history()
        self.assertEqual([s["id"] for s in listed], [ids[2], ids[1]])
        detail = main.get_scan_history(ids[2])
        self.assertEqual(detail["events"][0]["platform_label"], "Unstop")
        self.assertEqual(detail["events"][0]["tags"], ["AI/ML"])
        self.assertIsNone(main.get_scan_history(ids[0]))
        self.assertIsNone(main.get_scan_history(9999))

    # ---- blog posts and guides are not events -------------------------------------------
    def test_blog_article_about_a_hackathon_is_not_an_event(self):
        self.assertTrue(main.is_article_like(BLOG_URL, BLOG_TITLE))
        self.assertTrue(main.is_non_event_landing_page(BLOG_URL, BLOG_TITLE, "Registration and eligibility"))
        self.assertTrue(main.is_article_like("https://example.com/post/x", "A guide to winning hackathons"))
        self.assertTrue(main.is_article_like("https://example.com/x", "Plain title",
                                             "A long page. " * 30 + "8 min read. Published on 1 Oct 2026. Written by Sam."))

    def test_real_event_pages_are_not_mistaken_for_articles(self):
        for url, title in [(URLS["unstop"], "Nimbus AI Build Sprint 2026"), (URLS["devfolio"], "Chainforge Web3 Hack"),
                           (URLS["mlh"], "Prompt Relay 24h AI Hackathon"), ("https://devpost.com/hackathons/foo", "Foo Hackathon")]:
            self.assertFalse(main.is_article_like(url, title), url)
            self.assertFalse(main.is_non_event_landing_page(url, title, PAGE.format(name=title)), url)

    def test_shortlist_drops_articles_but_keeps_dev_to_leads(self):
        raw = [{"url": BLOG_URL, "title": BLOG_TITLE},
               {"url": "https://dev.to/someone/join-the-agent-build-challenge-1a2b", "title": "Join the Agent Build Challenge hackathon"},
               {"url": URLS["unstop"], "title": "Nimbus AI Build Sprint 2026"}]
        picked, _ = main.build_shortlist(raw, 10)
        urls = {i["url"] for i in picked}
        self.assertNotIn(BLOG_URL, urls)
        self.assertIn(raw[1]["url"], urls)
        self.assertIn(URLS["unstop"], urls)

    def test_old_blog_rows_are_hidden_from_the_feed(self):
        main.upsert_events([self._event(url=BLOG_URL, title=BLOG_TITLE, source="whereuelevate.com", deadline=None,
                                        registration_status="unknown")])
        self.assertEqual(main.list_events(), [])

    # ---- popups only for confirmed-open events ------------------------------------------
    def test_popup_requires_confirmed_open_registration(self):
        base = {"is_saved": False, "is_dismissed": False, "match_score": 80, "is_demo": False, "first_seen": "2026-01-01"}
        unknown = {**base, "url": "https://a.example/x", "registration_status": "unknown"}
        opened = {**base, "url": "https://b.example/y", "registration_status": "open", "match_score": 40}
        self.assertIsNone(main.select_next_match([unknown], set()))
        self.assertEqual(main.select_next_match([unknown, opened], set())["url"], opened["url"])
        closed = {**opened, "registration_status": "closed"}
        self.assertIsNone(main.select_next_match([closed], set()))

    # ---- full live scan with the network faked ------------------------------------------
    def test_live_scan_records_every_platform_and_history(self):
        with patch.object(main, "API_KEY", "tf"), patch.object(main, "EXTRACTION_PROVIDER_SETTING", "rules"), \
             patch.object(main, "fetch_search", side_effect=fake_rows), patch.object(main, "fetch_pages", side_effect=fake_fetch_pages):
            first = main.scan(types.SimpleNamespace(use_demo=False))
            second = main.scan(types.SimpleNamespace(use_demo=False))
        self.assertEqual(first["new_count"], 3)
        self.assertNotIn(BLOG_URL, {e["url"] for e in first["events"]})  # the SIH blog post never becomes an event
        self.assertNotIn(BLOG_URL, {e["url"] for e in main.get_scan_history(first["scan_id"])["events"]})
        labels = {p["label"]: p for p in first["platforms"]}
        for name in ("Unstop", "Devfolio", "MLH"):
            self.assertEqual(labels[name]["kept"], 1, name)
            self.assertEqual(labels[name]["new"], 1, name)
        self.assertEqual(labels["Devpost"]["results"], 0)  # searched, nothing found: still listed
        history = main.list_scan_history()
        self.assertEqual(len(history), 2)
        detail = main.get_scan_history(first["scan_id"])
        self.assertEqual({e["outcome"] for e in detail["events"]}, {"new"})
        self.assertEqual({e["platform_label"] for e in detail["events"]}, {"Unstop", "Devfolio", "MLH"})
        self.assertEqual(second["new_count"], 0)
        self.assertEqual({e["outcome"] for e in main.get_scan_history(second["scan_id"])["events"]}, {"seen"})

    def test_demo_scan_is_recorded_too(self):
        result = main.scan(types.SimpleNamespace(use_demo=True))
        detail = main.get_scan_history(result["scan_id"])
        self.assertEqual(detail["mode"], "demo")
        self.assertTrue(detail["events"])
        self.assertTrue(all(e["is_demo"] for e in detail["events"]))

    def test_feed_events_carry_platform_label(self):
        main.upsert_events([self._event()])
        self.assertEqual(main.list_events()[0]["platform_label"], "Unstop")


if __name__ == "__main__":
    unittest.main()
