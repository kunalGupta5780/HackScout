import asyncio
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import app.main as main
from app import resume as R

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    import pypdf  # noqa: F401
    HAVE_PDF_TOOLS = True
except ImportError:  # the PDF fixtures need reportlab; everything else still runs
    HAVE_PDF_TOOLS = False

RESUME_TEXT = [
    "Asha Verma  asha.verma@example.com  +91 90000 00000",
    "B.Tech Computer Science, Delhi Technological University, CGPA 8.7, Expected graduation 2027",
    "Skills: Python, FastAPI, React, TypeScript, Machine Learning, PyTorch, scikit-learn, Docker, Git, SQL, C++",
    "Built an NLP chatbot using LangChain and LLMs. Won a hackathon with a healthcare triage app.",
    "Medical imaging project. Open source contributor on GitHub. Node.js REST APIs with PostgreSQL.",
]
PROFILE = types.SimpleNamespace(skills=["AI/ML", "Python", "Web Development"], interests=["Artificial Intelligence", "Open Innovation"],
                                level="Student", location="India", prefer_online=True)


def make_pdf(lines, encrypt=False):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 20
    c.save()
    data = buf.getvalue()
    if encrypt:
        from pypdf import PdfReader, PdfWriter
        w = PdfWriter()
        for page in PdfReader(io.BytesIO(data)).pages:
            w.add_page(page)
        w.encrypt("secret")
        out = io.BytesIO()
        w.write(out)
        data = out.getvalue()
    return data


def make_image_only_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.rect(50, 50, 300, 300, fill=1)  # a shape, no text: looks like a scanned page to a text extractor
    c.save()
    return buf.getvalue()


class AnalyzeTests(unittest.TestCase):
    def test_skills_interests_and_level_from_a_student_resume(self):
        out = R.analyze_resume("\n".join(RESUME_TEXT))
        self.assertEqual(out["skills"][:4], ["Python", "FastAPI", "React", "TypeScript"])  # order of appearance on ties
        for skill in ("Machine Learning", "PyTorch", "Docker", "C++", "LangChain", "Node.js", "PostgreSQL"):
            self.assertIn(skill, out["skills"])
        self.assertTrue({"AI/ML", "Web Development", "Health"} <= set(out["interests"]))
        self.assertEqual(out["level"], "Student")

    def test_professional_level_from_years_of_experience(self):
        out = R.analyze_resume("Software Engineer at Acme. 5+ years of experience in Java, Spring Boot, AWS, Kubernetes and Terraform.")
        self.assertEqual(out["level"], "Professional")
        self.assertIn("Cloud / DevOps", out["interests"])

    def test_no_false_positives_from_substrings_or_common_words(self):
        out = R.analyze_resume("I enjoy JavaScript and I like to go running. Learning to program in Go is on my list. Class of 2028.")
        self.assertIn("JavaScript", out["skills"])
        self.assertNotIn("Java", out["skills"])   # 'Java' must not match inside 'JavaScript'
        self.assertNotIn("Go", out["skills"])     # the plain word 'go' is not the language

    def test_contact_details_are_never_extracted(self):
        out = R.analyze_resume("\n".join(RESUME_TEXT))
        blob = repr(out).lower()
        for secret in ("asha", "verma", "example.com", "90000"):
            self.assertNotIn(secret, blob)


@unittest.skipUnless(HAVE_PDF_TOOLS, "reportlab/pypdf not installed")
class PdfReadingTests(unittest.TestCase):
    def test_reads_text_from_a_real_pdf(self):
        text, pages = R.extract_pdf_text(make_pdf(RESUME_TEXT))
        self.assertEqual(pages, 1)
        self.assertIn("FastAPI", text)

    def test_rejects_non_pdf_oversized_scanned_and_locked_files(self):
        with self.assertRaises(R.ResumeError) as e:
            R.extract_pdf_text(b"hello, I am not a pdf")
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(R.ResumeError) as e:
            R.extract_pdf_text(b"%PDF-1.4 " + b"0" * (R.MAX_PDF_BYTES + 1))
        self.assertEqual(e.exception.status, 413)
        with self.assertRaises(R.ResumeError) as e:
            R.extract_pdf_text(make_image_only_pdf())
        self.assertIn("no selectable text", str(e.exception))
        with self.assertRaises(R.ResumeError) as e:
            R.extract_pdf_text(make_pdf(RESUME_TEXT, encrypt=True))
        self.assertIn("password", str(e.exception))


class FakeRequest:
    def __init__(self, data, filename="My Resume.pdf"):
        self._data, self.headers = data, {"x-filename": filename}

    async def body(self):
        return self._data


class ResumeFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.patches = [patch.object(main, "DB_PATH", self.db), patch.object(main, "RESUME", None), patch.object(main, "PROFILE", PROFILE),
                        patch.object(main, "SCAN_EXTRA_PAGES", 0)]
        for p in self.patches:
            p.start()
        main.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _event(self, **kw):
        base = {"title": "Agent Build Challenge", "url": "https://devpost.com/hackathons/agent-build", "source": "devpost.com",
                "snippet": "Build a Python and FastAPI agent that completes a real task.", "deadline": "2099-01-01", "format": "Online",
                "eligibility": "Open to everyone", "tags": ["Web Development"], "verification": "page_fetched",
                "registration_status": "open", "status_reason": "", "extraction_provider": "rules"}
        return {**base, **kw}

    # ---- matching -----------------------------------------------------------------------
    def test_resume_raises_the_score_and_explains_why(self):
        event = self._event()
        no_resume = main.explain_match(event, PROFILE, resume={})
        with_resume = main.explain_match(event, PROFILE, resume={"skills": ["Python", "FastAPI"], "interests": ["Web Development"]})
        self.assertGreater(with_resume[0], no_resume[0])
        self.assertTrue(any(r.startswith("From your resume:") and "FastAPI" in r for r in with_resume[1]), with_resume[1])

    def test_unrelated_event_is_not_boosted_by_the_resume(self):
        event = self._event(title="Knitting Circle Hackathon", snippet="Yarn and needles.", tags=["Craft"], format="In-person")
        score, reasons = main.explain_match(event, PROFILE, resume={"skills": ["Python"], "interests": ["AI/ML"]})
        self.assertLess(score, 20)
        self.assertFalse(any("resume" in r for r in reasons))

    def test_matching_uses_whole_words_and_synonyms(self):
        trap = self._event(title="Maintain the Mainframe Hackathon", snippet="We maintain legacy mainframes.", tags=[], format="In-person")
        self.assertEqual(main.explain_match(trap, types.SimpleNamespace(**{**vars(PROFILE), "skills": ["AI"], "interests": []}), resume={})[0], 0)
        ai_event = self._event(title="Campus Build", snippet="Teams build things.", tags=["AI/ML"], format="In-person")
        score, _ = main.explain_match(ai_event, types.SimpleNamespace(**{**vars(PROFILE), "skills": [], "interests": ["Artificial Intelligence"]}), resume={})
        self.assertGreater(score, 0)

    def test_many_resume_skills_do_not_dilute_the_score(self):
        event = self._event(tags=["AI/ML", "Web Development"], snippet="Python FastAPI React AI agents")
        few = main.explain_match(event, PROFILE, resume={"skills": ["Python"], "interests": []})[0]
        many = main.explain_match(event, PROFILE, resume={"skills": ["Python", "FastAPI", "React", "Rust", "Go", "Swift", "Dart", "Ruby", "PHP", "Kotlin", "C++", "Java"], "interests": ["AI/ML"]})[0]
        self.assertGreaterEqual(many, few)

    # ---- upload / store / remove --------------------------------------------------------
    @unittest.skipUnless(HAVE_PDF_TOOLS, "reportlab/pypdf not installed")
    def test_upload_stores_only_skills_and_rescores_events(self):
        main.upsert_events([self._event()])
        before = main.list_events()[0]["match_score"]
        out = asyncio.run(main.api_upload_resume(FakeRequest(make_pdf(RESUME_TEXT))))
        self.assertTrue(out["present"])
        self.assertEqual(out["filename"], "My Resume.pdf")
        self.assertIn("Python", out["skills"])
        self.assertEqual(out["events_rescored"], 1)
        item = main.list_events()[0]
        self.assertGreaterEqual(item["match_score"], before)
        self.assertTrue(any(r.startswith("From your resume") for r in item["match_reasons"]))
        self.assertEqual(main.load_resume()["skills"], out["skills"])  # survives a restart
        raw = self.db.read_bytes()
        for private in (b"Asha Verma", b"asha.verma@example.com", b"90000"):
            self.assertNotIn(private, raw)  # no name, email, phone or resume text is stored
        self.assertNotIn(b"%PDF-", raw)     # and the PDF itself is not stored

    def test_bad_uploads_become_http_errors(self):
        with self.assertRaises(HTTPException) as e:
            asyncio.run(main.api_upload_resume(FakeRequest(b"not a pdf")))
        self.assertEqual(e.exception.status_code, 400)
        with patch.object(main, "extract_pdf_text", return_value=("Lorem ipsum dolor sit amet, consectetur adipiscing elit sed do.", 1)):
            with self.assertRaises(HTTPException) as e:
                asyncio.run(main.api_upload_resume(FakeRequest(b"%PDF-1.4")))
        self.assertEqual(e.exception.status_code, 422)
        self.assertIn("no recognisable skills", e.exception.detail)
        self.assertIsNone(main.load_resume())

    def test_filename_is_cleaned(self):
        self.assertEqual(main.clean_filename("C%3A%5CUsers%5Casha%5Cresume%20final.pdf"), "resume final.pdf")
        self.assertEqual(main.clean_filename(""), "resume.pdf")
        self.assertEqual(main.clean_filename("../../etc/passwd<script>.pdf"), "passwdscript.pdf")

    def test_delete_removes_resume_and_rescoring_follows(self):
        main.upsert_events([self._event()])
        with patch.object(main, "extract_pdf_text", return_value=("\n".join(RESUME_TEXT), 1)):
            main.handle_resume_upload(b"%PDF-1.4", "r.pdf")
        self.assertIsNotNone(main.RESUME)
        self.assertEqual(main.api_delete_resume()["ok"], True)
        self.assertIsNone(main.RESUME)
        self.assertEqual(main.api_get_resume(), {"present": False})
        self.assertIsNone(main.load_resume())
        self.assertFalse(any("resume" in r for r in main.list_events()[0]["match_reasons"]))

    # ---- resume-driven searching --------------------------------------------------------
    def test_resume_adds_generic_searches_without_personal_details(self):
        with patch.object(main, "RESUME", {"filename": "Asha Verma CV.pdf", "skills": ["Python", "FastAPI"], "interests": ["AI/ML", "Web Development"], "level": "Student"}):
            queries = main.resume_queries(2026, "India")
        self.assertEqual(len(queries), 3)
        self.assertTrue(all("hackathon 2026" in q for q in queries))
        self.assertTrue(any(q.startswith("AI ML hackathon") for q in queries))
        self.assertFalse(any("asha" in q.lower() or "verma" in q.lower() for q in queries))
        self.assertEqual(main.resume_queries(2026, "India"), [])  # nothing without a resume

    def test_scan_searches_include_resume_queries_only_when_a_resume_exists(self):
        seen = []
        def fake(query, page=0):
            seen.append(query)
            return []
        base_jobs = sum(len(s["queries"]) for s in main.ACTIVE_SOURCES)
        with patch.object(main, "fetch_search", side_effect=fake):
            main.search_event_sources(2026)
        self.assertEqual(len(seen), base_jobs)
        seen.clear()
        with patch.object(main, "RESUME", {"filename": "r.pdf", "skills": ["Python"], "interests": ["AI/ML"], "level": None}), \
             patch.object(main, "fetch_search", side_effect=fake):
            main.search_event_sources(2026)
        self.assertEqual(len(seen), base_jobs + 2)
        self.assertTrue(any(q.startswith("AI ML hackathon 2026") for q in seen))

    # ---- profile persistence ------------------------------------------------------------
    def test_profile_is_saved_so_it_survives_a_restart(self):
        main.save_profile(types.SimpleNamespace(skills=["Rust"], interests=["Climate"], level="Professional", location="Pune", prefer_online=False))
        import sqlite3, json
        row = sqlite3.connect(self.db).execute("SELECT value FROM app_state WHERE key='profile'").fetchone()
        self.assertEqual(json.loads(row[0])["skills"], ["Rust"])


if __name__ == "__main__":
    unittest.main()
