import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
import app.main as main


class EnvDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patches = [patch.object(main, "BASE_DIR", self.dir), patch.object(main, "ENV_FILE", self.dir / ".env"),
                        patch.object(main, "API_KEY", "")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def status(self):
        return main.env_diagnostics()["status"]

    def test_no_env_file(self):
        d = main.env_diagnostics()
        self.assertFalse(d["file_exists"])
        self.assertIn("no file named .env", d["status"])

    def test_env_txt_from_notepad_is_called_out(self):
        (self.dir / ".env.txt").write_text("TINYFISH_API_KEY=sk-tinyfish-abc", encoding="utf-8")
        self.assertIn("rename it to .env", self.status())

    def test_line_missing_empty_and_placeholder(self):
        env = self.dir / ".env"
        env.write_text("GEMINI_API_KEY=abc\n", encoding="utf-8")
        self.assertIn("no TINYFISH_API_KEY= line", self.status())
        env.write_text("TINYFISH_API_KEY=\n", encoding="utf-8")
        self.assertIn("line is empty", self.status())
        env.write_text("TINYFISH_API_KEY=your_tinyfish_api_key_here\n", encoding="utf-8")
        self.assertIn("placeholder", self.status())

    def test_key_pasted_into_env_example_instead_of_env(self):
        (self.dir / ".env").write_text("TINYFISH_API_KEY=\n", encoding="utf-8")
        (self.dir / ".env.example").write_text("TINYFISH_API_KEY=sk-tinyfish-realkey1234\n", encoding="utf-8")
        self.assertIn(".env.example", self.status())

    def test_found_key_reports_length_and_tail_never_the_key(self):
        with patch.object(main, "API_KEY", "sk-tinyfish-0123456789abcd"):
            d = main.env_diagnostics()
        self.assertEqual(d["status"], "TinyFish key found")
        self.assertEqual(d["key_tail"], "abcd")
        self.assertNotIn("sk-tinyfish-0123456789abcd", repr(d))

    def test_missing_key_message_names_the_file_and_says_to_restart(self):
        msg = main.missing_key_message()
        self.assertIn(str(self.dir / ".env"), msg)
        self.assertIn("start it again", msg)

    def test_env_file_wins_over_empty_variable_and_survives_a_bom(self):
        env = self.dir / ".env"
        env.write_bytes(b"\xef\xbb\xbfTINYFISH_API_KEY=sk-tinyfish-from-file\n")  # Notepad-style BOM
        with patch.dict(os.environ, {"TINYFISH_API_KEY": ""}):
            load_dotenv(env, override=True, encoding="utf-8-sig")
            self.assertEqual(os.environ["TINYFISH_API_KEY"], "sk-tinyfish-from-file")

    def test_health_reports_env_details(self):
        self.assertIn("env", main.health())
        self.assertFalse(main.health()["tinyfish_configured"])


if __name__ == "__main__":
    unittest.main()
