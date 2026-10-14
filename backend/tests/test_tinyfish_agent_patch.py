import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
import app.main as main
from app.main import (
    extract_event_metadata_with_tinyfish_agent,
    is_non_event_landing_page,
    normalize_results,
)


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = text
    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://agent.tinyfish.ai/v1/automation/run")
            response = httpx.Response(self.status_code, request=request, text=self.text or "API error")
            raise httpx.HTTPStatusError("HTTP error", request=request, response=response)
    def json(self):
        return self._payload


class TinyFishAgentPatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.db_patch = patch.object(main, "DB_PATH", self.db_path)
        self.db_patch.start()
        main.init_db()
        # Fresh isolated app-step ledger for each test.
        with closing(main.sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM scan_meta WHERE key IN (?, ?)", (
                main.TINYFISH_AGENT_OBSERVED_STEPS_KEY,
                main.TINYFISH_AGENT_RESERVED_STEPS_KEY,
            ))
            conn.execute("INSERT INTO scan_meta (key, value) VALUES (?, 0)", (main.TINYFISH_AGENT_OBSERVED_STEPS_KEY,))
            conn.execute("INSERT INTO scan_meta (key, value) VALUES (?, 0)", (main.TINYFISH_AGENT_RESERVED_STEPS_KEY,))
            conn.commit()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_agent_response_extracts_structured_metadata(self):
        metadata = {
            "title": "Sample Hackathon 2026",
            "title_evidence": "Sample Hackathon 2026",
            "event_kind": "hackathon",
            "is_individual_event": True,
            "registration_deadline": "2026-10-30",
            "registration_deadline_evidence": "Registration deadline: 30 October 2026",
            "submission_deadline": "",
            "submission_deadline_evidence": "",
            "event_start_date": "2026-11-01",
            "event_end_date": "2026-11-02",
            "event_dates_evidence": "Event dates: 1 November 2026 to 2 November 2026",
            "format": "Online",
            "format_evidence": "This is an online hackathon",
            "eligibility": "Students are eligible",
            "eligibility_evidence": "Students are eligible",
            "registration_status_claim": "open",
            "status_evidence": "Registration is open until 30 October 2026",
            "tags": ["AI/ML"],
        }
        stats = {"attempted": 0, "succeeded": 0, "steps_used": 0, "budget_skipped": 0}
        fake_payload = {"status": "COMPLETED", "num_of_steps": 12, "result": metadata}
        with patch.object(main, "API_KEY", "test-key"), \
             patch.object(main.httpx, "post", return_value=FakeResponse(fake_payload)) as post:
            result = extract_event_metadata_with_tinyfish_agent(
                "https://example.org/hackathons/sample-2026", "Sample Hackathon 2026",
                "Sample Hackathon 2026 is an online hackathon. Registration deadline: 30 October 2026. "
                "Event dates: 1 November 2026 to 2 November 2026. Students are eligible. "
                "Registration is open until 30 October 2026.", stats,
            )
        self.assertEqual(result, metadata)
        self.assertEqual(stats["attempted"], 1)
        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(stats["steps_used"], 12)
        self.assertEqual(post.call_args.args[0], "https://agent.tinyfish.ai/v1/automation/run")
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-Key"], "test-key")
        body = post.call_args.kwargs["json"]
        self.assertIn("output_schema", body)
        self.assertEqual(body["agent_config"]["max_duration_seconds"], main.TINYFISH_AGENT_MAX_DURATION_SECONDS)

    def test_failed_run_does_not_claim_success(self):
        stats = {"attempted": 0, "succeeded": 0, "steps_used": 0, "budget_skipped": 0}
        with patch.object(main, "API_KEY", "test-key"), \
             patch.object(main.httpx, "post", return_value=FakeResponse({"status": "FAILED", "num_of_steps": 7, "error": {"message": "site blocked"}})):
            result = extract_event_metadata_with_tinyfish_agent(
                "https://example.org/hackathons/sample", "Sample Hackathon",
                "Sample Hackathon with page details.", stats,
            )
        self.assertIsNone(result)
        self.assertEqual(stats["succeeded"], 0)
        self.assertEqual(stats["steps_used"], 7)

    def test_budget_prevents_new_agent_run_without_calling_provider(self):
        with patch.object(main, "TINYFISH_AGENT_STEP_BUDGET", 150), \
             patch.object(main, "API_KEY", "test-key"), \
             patch.object(main.httpx, "post") as post:
            # One unconfirmed reservation exhausts the 150-step ceiling.
            self.assertTrue(main.reserve_tinyfish_agent_run())
            stats = {"attempted": 0, "succeeded": 0, "steps_used": 0, "budget_skipped": 0}
            result = extract_event_metadata_with_tinyfish_agent(
                "https://example.org/hackathons/sample", "Sample Hackathon",
                "Sample Hackathon with page details.", stats,
            )
        self.assertIsNone(result)
        self.assertEqual(stats["budget_skipped"], 1)
        post.assert_not_called()


    def test_confirmed_usage_and_reservations_are_separate(self):
        self.assertEqual(main.tinyfish_agent_steps_used(), 0)
        self.assertEqual(main.tinyfish_agent_steps_reserved(), 0)
        self.assertTrue(main.reserve_tinyfish_agent_run())
        self.assertEqual(main.tinyfish_agent_steps_used(), 0, "a reservation is not actual spend")
        self.assertEqual(main.tinyfish_agent_steps_reserved(), 150)
        self.assertEqual(main.tinyfish_agent_budget_consumed(), 150)
        main.finalize_tinyfish_agent_reservation(7)
        self.assertEqual(main.tinyfish_agent_steps_used(), 7)
        self.assertEqual(main.tinyfish_agent_steps_reserved(), 0)
        self.assertEqual(main.tinyfish_agent_budget_consumed(), 7)

    def test_unknown_usage_keeps_reservation_out_of_confirmed_spend(self):
        stats = {"attempted": 0, "succeeded": 0, "steps_used": 0, "steps_unconfirmed": 0, "budget_skipped": 0}
        with patch.object(main, "API_KEY", "test-key"), \
             patch.object(main.httpx, "post", side_effect=httpx.ReadTimeout("simulated timeout")):
            result = extract_event_metadata_with_tinyfish_agent(
                "https://example.org/hackathons/sample", "Sample Hackathon",
                "Sample Hackathon with page details.", stats,
            )
        self.assertIsNone(result)
        self.assertEqual(main.tinyfish_agent_steps_used(), 0)
        self.assertEqual(main.tinyfish_agent_steps_reserved(), 150)
        self.assertEqual(stats["steps_used"], 0)
        self.assertEqual(stats["steps_unconfirmed"], 150)

    def test_marketing_page_rejected_before_agent(self):
        url = "https://info.devpost.com/product/public-hackathons"
        title = "Host public hackathons to grow your developer ecosystem | Devpost"
        text = "Run virtual hackathons. Manage your registrations and project submissions. Book a demo."
        self.assertTrue(is_non_event_landing_page(url, title, text))
        self.assertEqual(
            normalize_results([{"url": url, "title": title, "snippet": text}],
                              {url: {"title": title, "text": text, "final_url": url}},
                              use_tinyfish_agent=True),
            [],
        )

    def test_short_year_deadline_extracted_by_rules_when_agent_does_not_run(self):
        url = "https://unstop.com/hackathons/example-event-123"
        title = "Example Hackathon 2026"
        text = "Example Hackathon 2026. Registration Deadline: 30 Aug 26. Students may register."
        result = normalize_results([{"url": url, "title": title, "snippet": text}],
                                   {url: {"title": title, "text": text, "final_url": url}},
                                   use_tinyfish_agent=False)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["deadline"], "2026-08-30")
        self.assertEqual(result[0]["registration_status"], "closed")


if __name__ == "__main__":
    unittest.main()
