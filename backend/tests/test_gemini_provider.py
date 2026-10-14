import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import app.main as main
from app.main import normalize_results


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload, self.status_code, self.text = payload or {}, status_code, text

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def gemini_reply(obj):
    return FakeResponse({"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}, "finishReason": "STOP"}]})


PAGE = ("Sample Hackathon 2026 is an online hackathon. Registration deadline: 30 October 2026. "
        "Students are eligible. Prizes and team registration are on this page.")
URL = "https://unstop.com/hackathons/sample-hackathon-2026-555"
METADATA = {
    "title": "Sample Hackathon 2026", "title_evidence": "Sample Hackathon 2026", "event_kind": "hackathon",
    "is_individual_event": True, "registration_deadline": "2026-10-30",
    "registration_deadline_evidence": "Registration deadline: 30 October 2026",
    "submission_deadline": "", "submission_deadline_evidence": "", "event_start_date": "", "event_end_date": "",
    "event_dates_evidence": "", "format": "Online", "format_evidence": "is an online hackathon",
    "eligibility": "Students are eligible", "eligibility_evidence": "Students are eligible",
    "registration_status_claim": "open", "status_evidence": "", "tags": ["AI/ML"],
}


class GeminiProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(main, "DB_PATH", Path(self.tmp.name) / "t.db"),
            patch.object(main, "GEMINI_API_KEY", "test-gemini-key"),
            patch.object(main, "GEMINI_MIN_INTERVAL_SECONDS", 0.0),
            patch.object(main, "GEMINI_MODELS", ["model-a", "model-b"]),
            patch.dict(main._GEMINI_STATE, {"last_call": 0.0, "good_model": None}),
        ]
        for p in self.patches:
            p.start()
        main.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # ---- which extractor runs -----------------------------------------------------------
    def test_default_never_selects_paid_tinyfish_agent(self):
        with patch.object(main, "API_KEY", "tf-key"):
            for setting in ("auto", "gemini", "rules", "", "something-else"):
                with patch.object(main, "EXTRACTION_PROVIDER_SETTING", setting):
                    self.assertNotEqual(main.resolve_extraction_provider(), "tinyfish_agent", setting)

    def test_agent_only_when_explicitly_requested(self):
        with patch.object(main, "API_KEY", "tf-key"), patch.object(main, "EXTRACTION_PROVIDER_SETTING", "tinyfish_agent"):
            self.assertEqual(main.resolve_extraction_provider(), "tinyfish_agent")

    def test_auto_uses_gemini_with_key_and_rules_without(self):
        with patch.object(main, "EXTRACTION_PROVIDER_SETTING", "auto"):
            self.assertEqual(main.resolve_extraction_provider(), "gemini")
            with patch.object(main, "GEMINI_API_KEY", ""):
                self.assertEqual(main.resolve_extraction_provider(), "rules")

    # ---- the Gemini call ----------------------------------------------------------------
    def test_gemini_metadata_is_returned_and_request_is_well_formed(self):
        stats = {}
        with patch.object(main.httpx, "post", return_value=gemini_reply(METADATA)) as post:
            result = main.extract_event_metadata_with_gemini(URL, "Sample Hackathon 2026", PAGE, stats)
        self.assertEqual(result, METADATA)
        self.assertEqual(stats["gemini_succeeded"], 1)
        self.assertIn("model-a:generateContent", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-gemini-key")
        cfg = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(cfg["responseMimeType"], "application/json")
        self.assertIn("responseSchema", cfg)

    def test_retired_model_falls_through_to_next_model(self):
        replies = [FakeResponse({"error": {"message": "models/model-a is not found"}}, 404), gemini_reply(METADATA)]
        stats = {}
        with patch.object(main.httpx, "post", side_effect=replies) as post:
            result = main.extract_event_metadata_with_gemini(URL, "t", PAGE, stats)
        self.assertEqual(result, METADATA)
        self.assertEqual(stats["gemini_model"], "model-b")
        self.assertEqual(post.call_count, 2)

    def test_quota_everywhere_stops_calls_for_the_rest_of_the_scan_and_reports_why(self):
        stats = {}
        with patch.object(main.httpx, "post", return_value=FakeResponse({"error": {"message": "quota"}}, 429)) as post:
            self.assertIsNone(main.extract_event_metadata_with_gemini(URL, "t", PAGE, stats))
            calls_after_first = post.call_count
            self.assertIsNone(main.extract_event_metadata_with_gemini(URL, "t", PAGE, stats))
            self.assertEqual(post.call_count, calls_after_first)  # circuit breaker: no more calls
        self.assertIn("429", stats["gemini_last_error"])

    def test_bad_key_does_not_try_other_models(self):
        with patch.object(main.httpx, "post", return_value=FakeResponse({"error": {"message": "API key not valid"}}, 400)) as post:
            self.assertIsNone(main.extract_event_metadata_with_gemini(URL, "t", PAGE, {}))
        self.assertEqual(post.call_count, 1)

    def test_invalid_json_from_model_falls_back(self):
        bad = FakeResponse({"candidates": [{"content": {"parts": [{"text": "not json at all"}]}}]})
        stats = {}
        with patch.object(main.httpx, "post", return_value=bad):
            self.assertIsNone(main.extract_event_metadata_with_gemini(URL, "t", PAGE, stats))
        self.assertIn("not valid JSON", stats["gemini_last_error"])

    def test_network_failure_is_reported_not_raised(self):
        stats = {}
        with patch.object(main.httpx, "post", side_effect=httpx.ConnectError("down")):
            self.assertIsNone(main.extract_event_metadata_with_gemini(URL, "t", PAGE, stats))
        self.assertIn("Could not reach Gemini", stats["gemini_last_error"])

    # ---- end to end through normalize_results ------------------------------------------
    def test_grounded_gemini_values_are_used_and_labelled(self):
        fetched = {URL: {"title": "Sample Hackathon 2026", "text": PAGE, "final_url": URL}}
        with patch.object(main.httpx, "post", return_value=gemini_reply(METADATA)) as post, \
             patch.object(main, "API_KEY", "tf-key"):
            events = normalize_results([{"url": URL, "title": "Sample Hackathon 2026", "snippet": ""}], fetched, provider="gemini")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["extraction_provider"], "gemini")
        self.assertEqual(events[0]["deadline"], "2026-10-30")
        self.assertEqual(events[0]["format"], "Online")
        self.assertNotIn("agent.tinyfish.ai", " ".join(str(c.args[0]) for c in post.call_args_list))

    def test_invented_date_without_page_evidence_is_rejected(self):
        lie = dict(METADATA, registration_deadline="2026-12-25",
                   registration_deadline_evidence="Registration deadline: 25 December 2026")
        fetched = {URL: {"title": "Sample Hackathon 2026", "text": PAGE, "final_url": URL}}
        with patch.object(main.httpx, "post", return_value=gemini_reply(lie)), patch.object(main, "API_KEY", "tf-key"):
            events = normalize_results([{"url": URL, "title": "Sample Hackathon 2026", "snippet": ""}], fetched, provider="gemini")
        self.assertEqual(events[0]["deadline"], "2026-10-30")  # rule-based date from the real page text wins

    def test_gemini_never_calls_the_paid_agent_endpoint(self):
        fetched = {URL: {"title": "Sample Hackathon 2026", "text": PAGE, "final_url": URL}}
        with patch.object(main.httpx, "post", return_value=gemini_reply(METADATA)) as post, patch.object(main, "API_KEY", "tf-key"):
            normalize_results([{"url": URL, "title": "Sample Hackathon 2026", "snippet": ""}], fetched, provider="gemini")
        for call in post.call_args_list:
            self.assertNotIn("tinyfish", str(call.args[0]))
        with patch.object(main.httpx, "post") as post2:
            normalize_results([{"url": URL, "title": "Sample Hackathon 2026", "snippet": ""}], fetched, provider="rules")
        post2.assert_not_called()

    def test_page_cap_limits_gemini_calls(self):
        urls = [f"https://unstop.com/hackathons/sample-hackathon-{i}-55{i}" for i in range(5)]
        fetched = {u: {"title": "Sample Hackathon 2026", "text": PAGE, "final_url": u} for u in urls}
        raw = [{"url": u, "title": "Sample Hackathon 2026", "snippet": ""} for u in urls]
        with patch.object(main, "GEMINI_MAX_PAGES_PER_SCAN", 2), \
             patch.object(main.httpx, "post", return_value=gemini_reply(METADATA)) as post:
            normalize_results(raw, fetched, provider="gemini")
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
