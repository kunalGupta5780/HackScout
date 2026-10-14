"""Read a resume PDF on this computer and turn it into skills, interests and a rough level.

Privacy by design:
  * The PDF is parsed locally with pypdf. It is never sent to TinyFish, Gemini or anyone else.
  * Only the extracted skill and interest lists (plus the file name and page count) are stored.
    The PDF itself and its full text are not saved, and contact details are never extracted.
  * Hackathon searches built from a resume use generic words such as "python hackathon 2026".
"""
from __future__ import annotations

import io
import re
from datetime import date

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGES = 6
MIN_TEXT_CHARS = 40


class ResumeError(ValueError):
    """A resume problem the user can fix. `status` is the HTTP status to report."""
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------------------------------ PDF
def extract_pdf_text(data: bytes) -> tuple[str, int]:
    """Return (text, page_count) for a text-based PDF."""
    if not data.startswith(b"%PDF-"):
        raise ResumeError("That file is not a PDF.", 400)
    if len(data) > MAX_PDF_BYTES:
        raise ResumeError("That PDF is larger than 5 MB. Export a smaller version of your resume.", 413)
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ResumeError("PDF reading needs the pypdf package. Run: pip install -r requirements.txt", 500) from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ResumeError("This PDF is password-protected. Export an unlocked copy.", 422)
        pages = len(reader.pages)
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PAGES])
    except ResumeError:
        raise
    except Exception:
        raise ResumeError("Could not read this PDF. Try exporting it again as a text PDF.", 422) from None
    if len(text.strip()) < MIN_TEXT_CHARS:
        raise ResumeError(
            "This PDF has no selectable text (it may be a scanned image). Export a text PDF, or add your skills by hand.", 422)
    return text, pages


# ------------------------------------------------------------------------------------------------ skills
# (display name, aliases). Matching is whole-word and case-insensitive; "C" and "R" are left out on purpose
# because a single letter matches too much.
SKILLS: list[tuple[str, list[str]]] = [
    ("Python", ["python"]), ("Java", ["java"]), ("JavaScript", ["javascript", "js", "ecmascript"]), ("TypeScript", ["typescript"]),
    ("C++", ["c++"]), ("C#", ["c#"]), ("Go", ["golang"]), ("Rust", ["rust"]), ("Kotlin", ["kotlin"]), ("Swift", ["swift"]),
    ("PHP", ["php"]), ("Ruby", ["ruby"]), ("Dart", ["dart"]),
    ("SQL", ["sql"]), ("MySQL", ["mysql"]), ("PostgreSQL", ["postgresql", "postgres"]), ("MongoDB", ["mongodb", "mongo db"]),
    ("Redis", ["redis"]), ("SQLite", ["sqlite"]), ("Firebase", ["firebase"]), ("Supabase", ["supabase"]),
    ("React", ["react", "reactjs", "react.js"]), ("Next.js", ["next.js", "nextjs"]), ("Vue", ["vue", "vue.js", "vuejs"]),
    ("Angular", ["angular"]), ("Svelte", ["svelte"]), ("Node.js", ["node.js", "nodejs"]), ("Express", ["express.js", "expressjs"]),
    ("Django", ["django"]), ("Flask", ["flask"]), ("FastAPI", ["fastapi"]), ("Spring Boot", ["spring boot", "springboot"]),
    ("Laravel", ["laravel"]), ("HTML", ["html", "html5"]), ("CSS", ["css", "css3"]), ("Tailwind CSS", ["tailwind", "tailwindcss"]),
    ("Bootstrap", ["bootstrap"]), ("REST APIs", ["rest api", "rest apis", "restful"]), ("GraphQL", ["graphql"]),
    ("Machine Learning", ["machine learning", "ml"]), ("Deep Learning", ["deep learning"]),
    ("NLP", ["nlp", "natural language processing"]), ("Computer Vision", ["computer vision"]), ("OpenCV", ["opencv"]),
    ("LLMs", ["llm", "llms", "large language model", "large language models"]),
    ("Generative AI", ["generative ai", "genai", "gen ai"]), ("LangChain", ["langchain"]),
    ("RAG", ["rag", "retrieval augmented generation"]), ("TensorFlow", ["tensorflow"]), ("PyTorch", ["pytorch"]),
    ("Keras", ["keras"]), ("Scikit-learn", ["scikit-learn", "sklearn", "scikit learn"]), ("Pandas", ["pandas"]),
    ("NumPy", ["numpy"]), ("Hugging Face", ["hugging face", "huggingface"]), ("Data Science", ["data science"]),
    ("Data Analysis", ["data analysis", "data analytics"]), ("Power BI", ["power bi", "powerbi"]), ("Tableau", ["tableau"]),
    ("Docker", ["docker"]), ("Kubernetes", ["kubernetes", "k8s"]), ("AWS", ["aws", "amazon web services"]),
    ("Azure", ["azure"]), ("GCP", ["gcp", "google cloud"]), ("Git", ["git"]), ("GitHub", ["github"]), ("Linux", ["linux"]),
    ("CI/CD", ["ci/cd", "cicd"]), ("Terraform", ["terraform"]), ("DevOps", ["devops"]),
    ("Flutter", ["flutter"]), ("Android", ["android"]), ("iOS", ["ios"]), ("React Native", ["react native"]),
    ("Solidity", ["solidity"]), ("Blockchain", ["blockchain"]), ("Web3", ["web3"]), ("Ethereum", ["ethereum"]),
    ("Smart Contracts", ["smart contract", "smart contracts"]), ("Solana", ["solana"]),
    ("Cybersecurity", ["cybersecurity", "cyber security", "infosec"]), ("CTF", ["ctf"]),
    ("Penetration Testing", ["penetration testing", "pentesting", "pen testing"]),
    ("Arduino", ["arduino"]), ("Raspberry Pi", ["raspberry pi"]), ("IoT", ["iot", "internet of things"]),
    ("Embedded Systems", ["embedded systems", "embedded"]), ("Verilog", ["verilog"]), ("ESP32", ["esp32"]),
    ("Robotics", ["robotics"]), ("MATLAB", ["matlab"]),
    ("Figma", ["figma"]), ("UI/UX", ["ui/ux", "ux design", "ui design"]),
    ("Unity", ["unity"]), ("Unreal Engine", ["unreal engine", "unreal"]), ("Godot", ["godot"]),
    ("Open Source", ["open source", "open-source"]),
]

# Interest areas, named so they match how hackathon pages describe themselves.
AREAS: dict[str, set[str]] = {
    "AI/ML": {"Machine Learning", "Deep Learning", "NLP", "Computer Vision", "LLMs", "Generative AI", "LangChain", "RAG",
              "TensorFlow", "PyTorch", "Keras", "Scikit-learn", "Hugging Face", "OpenCV"},
    "Data": {"Data Science", "Data Analysis", "Pandas", "NumPy", "Power BI", "Tableau", "SQL"},
    "Web Development": {"React", "Next.js", "Vue", "Angular", "Svelte", "Node.js", "Express", "Django", "Flask", "FastAPI",
                        "HTML", "CSS", "Tailwind CSS", "JavaScript", "TypeScript", "Laravel", "Spring Boot", "GraphQL", "REST APIs"},
    "Web3": {"Solidity", "Blockchain", "Web3", "Ethereum", "Smart Contracts", "Solana"},
    "Cybersecurity": {"Cybersecurity", "CTF", "Penetration Testing"},
    "IoT / Hardware": {"Arduino", "Raspberry Pi", "IoT", "Embedded Systems", "Verilog", "ESP32", "Robotics", "MATLAB"},
    "Mobile": {"Flutter", "Android", "iOS", "React Native", "Dart", "Kotlin", "Swift"},
    "Cloud / DevOps": {"Docker", "Kubernetes", "AWS", "Azure", "GCP", "Terraform", "CI/CD", "DevOps", "Linux"},
    "Game dev": {"Unity", "Unreal Engine", "Godot"},
    "Open Source": {"Open Source"},
    "Design": {"Figma", "UI/UX"},
}
DOMAINS: dict[str, str] = {
    "Health": r"health(?:care)?|medical|medtech|biotech|clinical",
    "FinTech": r"fintech|banking|payments?|finance",
    "Climate": r"climate|sustainab\w*|renewable|clean energy|carbon",
    "EdTech": r"edtech|e-learning|education(?:al)? platform",
}


def _alias_pattern(alias: str) -> str:
    words = [re.escape(w) for w in re.split(r"[\s\-]+", alias.strip()) if w]
    return r"[\s\-]+".join(words)


_SKILL_RES = [(name, re.compile(r"(?<![A-Za-z0-9+#])(?:" + "|".join(_alias_pattern(a) for a in aliases) + r")(?![A-Za-z0-9+#])", re.I))
              for name, aliases in SKILLS]
_DOMAIN_RES = {name: re.compile(r"(?<![a-z])(?:" + pat + r")(?![a-z])", re.I) for name, pat in DOMAINS.items()}

# Level hints
_STUDENT_RE = re.compile(
    r"\b(?:b\.?\s?tech|b\.?\s?e\.?|bca|b\.?\s?sc|bachelor(?:'s)?|undergraduate|pursuing|currently studying|"
    r"cgpa|sgpa|expected (?:graduation|date)|class of 20\d\d|first[- ]year|second[- ]year|third[- ]year|final[- ]year|"
    r"semester|college|university|institute of technology)\b", re.I)
_PRO_RE = re.compile(
    r"\b(?:(\d{1,2})\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:professional\s+)?experience|senior|lead|staff engineer|"
    r"full[- ]time|software (?:engineer|developer) at|work experience|professional experience)\b", re.I)


def analyze_resume(text: str, today: date | None = None) -> dict:
    """Skills (most-mentioned first), interest areas and a level guess from resume text."""
    today = today or date.today()
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for name, rx in _SKILL_RES:
        found = list(rx.finditer(text))
        if found:
            counts[name] = len(found)
            first_seen[name] = found[0].start()
    # most-mentioned first; ties keep the order they appear in the resume (a Skills section usually comes first)
    skills = sorted(counts, key=lambda k: (-counts[k], first_seen[k]))[:20]

    area_score: dict[str, int] = {}
    for area, members in AREAS.items():
        distinct = [m for m in members if m in counts]
        weight = sum(counts[m] for m in distinct)
        if len(distinct) >= 2 or (distinct and area in {"Web3", "Cybersecurity", "Game dev", "Open Source", "Design"}):
            area_score[area] = weight
    for domain, rx in _DOMAIN_RES.items():
        if len(rx.findall(text)) >= 2:
            area_score[domain] = 2
    interests = [a for a, _ in sorted(area_score.items(), key=lambda kv: (-kv[1], kv[0]))][:6]

    # level: explicit years of experience wins; otherwise student wording; otherwise unknown
    years = [int(m.group(1)) for m in _PRO_RE.finditer(text) if m.group(1)]
    grad_years = [int(y) for y in re.findall(r"\b(20\d\d)\b", text)]
    student_hits = len(_STUDENT_RE.findall(text))
    expects_future_grad = any(y >= today.year for y in grad_years) and re.search(r"expected|pursuing|present|current", text, re.I)
    level = None
    if years and max(years) >= 2:
        level = "Professional"
    elif student_hits >= 2 or expects_future_grad:
        level = "Student"
    elif _PRO_RE.search(text):
        level = "Professional"
    return {"skills": skills, "interests": interests, "level": level}
