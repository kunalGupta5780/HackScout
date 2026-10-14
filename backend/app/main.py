from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse, urljoin

import httpx
from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .resume import ResumeError, analyze_resume, extract_pdf_text

BASE_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BASE_DIR / ".env"
# override=True: the .env file wins over an empty TINYFISH_API_KEY already set in your terminal.
# utf-8-sig: Windows Notepad can save a hidden marker at the start of the file that would hide the first key.
load_dotenv(ENV_FILE, override=True, encoding="utf-8-sig")
DB_PATH = BASE_DIR / "hackscout.db"
def _clean_key(value: str | None) -> str:
    """Treat blanks and copied-from-the-example placeholders (your_..._here) as 'no key'."""
    value = (value or "").strip()
    return "" if value.lower().startswith("your_") or value.lower().endswith("_here") else value


API_KEY = _clean_key(os.getenv("TINYFISH_API_KEY"))
LOCATION = os.getenv("TINYFISH_LOCATION", "IN").strip() or "IN"
# TinyFish Search + Fetch (free) discover candidates and retrieve the real page text.
# Event facts are then extracted by Gemini (free tier) or by Python rules, and every value is
# checked against the fetched page text. TinyFish Agent is PAID per step, so it now runs only
# when EXTRACTION_PROVIDER=tinyfish_agent is set explicitly. It is never used by default.
# When it is enabled, the app tracks a conservative local step budget and reserves the
# provider's documented default maximum (150 steps) before each run.
TINYFISH_AGENT_MAX_PAGES_PER_SCAN = max(0, min(2, int(os.getenv("TINYFISH_AGENT_MAX_PAGES_PER_SCAN", "2"))))
TINYFISH_AGENT_MAX_DURATION_SECONDS = max(15, min(60, int(os.getenv("TINYFISH_AGENT_MAX_DURATION_SECONDS", "35"))))
TINYFISH_AGENT_STEP_BUDGET = max(150, min(1500, int(os.getenv("TINYFISH_AGENT_STEP_BUDGET", "300"))))
TINYFISH_AGENT_STEP_PRICE_USD = 0.016
TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN = 150  # Conservative reservation when a run's usage is unknown.
TINYFISH_AGENT_OBSERVED_STEPS_KEY = "tinyfish_agent_observed_steps"
TINYFISH_AGENT_RESERVED_STEPS_KEY = "tinyfish_agent_reserved_steps"
# Set this to the cumulative number of steps confirmed in TinyFish Billing History when
# migrating from the old app-local estimate. Search/Fetch are free and are not counted here.
TINYFISH_AGENT_INITIAL_OBSERVED_STEPS = max(0, int(os.getenv("TINYFISH_AGENT_INITIAL_OBSERVED_STEPS", "0")))
# Backwards-compatible alias for existing tests/tools; now means observed actual steps only.
TINYFISH_AGENT_STEPS_KEY = TINYFISH_AGENT_OBSERVED_STEPS_KEY

# ---- Gemini (free-tier friendly extraction) -------------------------------------------------
# auto (default): Gemini if GEMINI_API_KEY is set, otherwise rules.  gemini | rules | tinyfish_agent (paid).
EXTRACTION_PROVIDER_SETTING = os.getenv("EXTRACTION_PROVIDER", "auto").strip().lower()
GEMINI_API_KEY = _clean_key(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


def env_diagnostics() -> dict[str, Any]:
    """Explain, in plain words, why the TinyFish key is (not) being picked up."""
    info: dict[str, Any] = {"file": str(ENV_FILE), "file_exists": ENV_FILE.is_file(), "tinyfish_key_set": bool(API_KEY),
                            "key_length": len(API_KEY), "key_tail": API_KEY[-4:] if len(API_KEY) >= 12 else ""}
    if API_KEY:
        info["status"] = "TinyFish key found"
        return info
    if not ENV_FILE.is_file():
        status = "there is no file named .env at that path"
        if (BASE_DIR / ".env.txt").is_file():
            status += ", but .env.txt exists: rename it to .env (Notepad adds .txt)"
    else:
        try:
            values = dotenv_values(ENV_FILE, encoding="utf-8-sig")
        except Exception:
            values = {}
        raw = str(values.get("TINYFISH_API_KEY") or "").strip()
        if "TINYFISH_API_KEY" not in values:
            status = "the file has no TINYFISH_API_KEY= line"
        elif not raw:
            status = "the TINYFISH_API_KEY= line is empty"
        else:
            status = "the TINYFISH_API_KEY value is a placeholder, paste your real key"
        try:
            example = str(dotenv_values(BASE_DIR / ".env.example", encoding="utf-8-sig").get("TINYFISH_API_KEY") or "").strip()
        except Exception:
            example = ""
        if example and not _clean_key(example) == "" and not raw:
            status += "; a key is in .env.example, but the app only reads .env"
    info["status"] = status
    return info


def missing_key_message() -> str:
    d = env_diagnostics()
    return (f"TinyFish API key is not configured. HackScout looked in {d['file']} and {d['status']}. "
            "Add TINYFISH_API_KEY=your_key there, save the file, then stop the backend (Ctrl+C) and start it again.")


print(f"HackScout: TinyFish key {'FOUND' if API_KEY else 'MISSING'} ({env_diagnostics()['status']}); "
      f"Gemini key {'found' if GEMINI_API_KEY else 'missing'}; reading {ENV_FILE}")
# Tried in order; a model that is retired (404) or out of free quota (429) is skipped for the next one.
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODEL", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite").split(",") if m.strip()]
GEMINI_MAX_PAGES_PER_SCAN = max(0, min(20, int(os.getenv("GEMINI_MAX_PAGES_PER_SCAN", "8"))))
# Free tier is limited per minute, so calls are spaced out. Set to 0 if your project has billing enabled.
GEMINI_MIN_INTERVAL_SECONDS = max(0.0, min(30.0, float(os.getenv("GEMINI_MIN_INTERVAL_SECONDS", "4"))))
GEMINI_TIMEOUT_SECONDS = 40
_GEMINI_STATE: dict[str, Any] = {"last_call": 0.0, "good_model": None}


def resolve_extraction_provider() -> str:
    """Which extractor a scan will use. TinyFish Agent (paid) is opt-in only."""
    choice = EXTRACTION_PROVIDER_SETTING
    if choice == "tinyfish_agent":
        return "tinyfish_agent" if API_KEY else "rules"
    if choice in {"gemini", "auto"}:
        return "gemini" if GEMINI_API_KEY else "rules"
    return "rules"


app = FastAPI(title="HackScout API", version="0.1.0", description="Discover and track matching hackathons.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Profile(BaseModel):
    skills: list[str] = Field(default_factory=lambda: ["AI/ML", "Python", "Web Development"])
    interests: list[str] = Field(default_factory=lambda: ["Artificial Intelligence", "Open Innovation"])
    level: str = "Student"
    location: str = "India"
    prefer_online: bool = True

class ScanRequest(BaseModel):
    use_demo: bool = False

PROFILE = Profile()
# Skills and interests read from the user's resume PDF (None until one is uploaded). Never holds the PDF or its text.
RESUME: dict[str, Any] | None = None

# Fixtures are intentionally labelled DEMO. They are not live event listings.
DEMO_EVENTS = [
    {
        "title": "Campus AI Buildathon (Demo)",
        "url": "https://example.com/events/campus-ai-buildathon-demo",
        "source": "Demo source",
        "snippet": "Illustrative student-focused AI and web development hackathon. This is sample data for testing the interface, not a real event.",
        "deadline": None,
        "event_date": None,
        "format": "Unknown",
        "eligibility": "Not verified",
        "tags": ["AI/ML", "Python", "Web Development"],
        "verification": "demo",
        "registration_status": "unknown",
        "status_reason": "Demo records have no real registration details.",
        "is_demo": True,
    },
    {
        "title": "Open Innovation Sprint (Demo)",
        "url": "https://example.com/events/open-innovation-sprint-demo",
        "source": "Demo source",
        "snippet": "Illustrative open-innovation challenge with software and AI themes. This is sample data for testing the interface, not a real event.",
        "deadline": None,
        "event_date": None,
        "format": "Unknown",
        "eligibility": "Not verified",
        "tags": ["Open Innovation", "AI/ML", "Software"],
        "verification": "demo",
        "registration_status": "unknown",
        "status_reason": "Demo records have no real registration details.",
        "is_demo": True,
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS scan_meta (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            snippet TEXT NOT NULL DEFAULT '',
            deadline TEXT,
            event_date TEXT,
            event_end_date TEXT,
            submission_deadline TEXT,
            extraction_provider TEXT NOT NULL DEFAULT 'rules',
            format TEXT NOT NULL DEFAULT 'Unknown',
            eligibility TEXT NOT NULL DEFAULT 'Not verified',
            tags_json TEXT NOT NULL DEFAULT '[]',
            verification TEXT NOT NULL DEFAULT 'search_result',
            registration_status TEXT NOT NULL DEFAULT 'unknown',
            status_reason TEXT NOT NULL DEFAULT '',
            is_demo INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_checked TEXT NOT NULL,
            match_score INTEGER NOT NULL DEFAULT 0,
            is_saved INTEGER NOT NULL DEFAULT 0,
            is_dismissed INTEGER NOT NULL DEFAULT 0
        )""")
        # Lightweight migration for databases created by earlier MVP versions.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "registration_status" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN registration_status TEXT NOT NULL DEFAULT 'unknown'")
        if "status_reason" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN status_reason TEXT NOT NULL DEFAULT ''")
        if "event_end_date" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN event_end_date TEXT")
        if "submission_deadline" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN submission_deadline TEXT")
        if "extraction_provider" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN extraction_provider TEXT NOT NULL DEFAULT 'rules'")
        # Separate confirmed provider usage from conservative reservations for timed-out runs.
        # The legacy tinyfish_agent_steps_used key is intentionally left untouched; it mixed
        # confirmed steps and reservations and must not be displayed as actual billing.
        conn.execute(
            "INSERT OR IGNORE INTO scan_meta (key, value) VALUES (?, ?)",
            (TINYFISH_AGENT_OBSERVED_STEPS_KEY, TINYFISH_AGENT_INITIAL_OBSERVED_STEPS),
        )
        conn.execute(
            "INSERT OR IGNORE INTO scan_meta (key, value) VALUES (?, 0)",
            (TINYFISH_AGENT_RESERVED_STEPS_KEY,),
        )
        conn.execute("""CREATE TABLE IF NOT EXISTS scan_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            mode TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'rules',
            gemini_model TEXT,
            duration_seconds REAL NOT NULL DEFAULT 0,
            queries INTEGER NOT NULL DEFAULT 0,
            platforms_json TEXT NOT NULL DEFAULT '[]',
            candidates INTEGER NOT NULL DEFAULT 0,
            fetched INTEGER NOT NULL DEFAULT 0,
            kept INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            errors_json TEXT NOT NULL DEFAULT '[]',
            note TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS scan_history_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            platform_key TEXT NOT NULL DEFAULT '',
            platform_label TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT 'seen',
            deadline TEXT,
            registration_status TEXT NOT NULL DEFAULT 'unknown',
            format TEXT NOT NULL DEFAULT 'Unknown',
            eligibility TEXT NOT NULL DEFAULT 'Not verified',
            match_score INTEGER NOT NULL DEFAULT 0,
            extraction_provider TEXT NOT NULL DEFAULT 'rules',
            verification TEXT NOT NULL DEFAULT 'search_result',
            tags_json TEXT NOT NULL DEFAULT '[]',
            snippet TEXT NOT NULL DEFAULT '',
            is_demo INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_history_events_scan ON scan_history_events(scan_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS resume_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            pages INTEGER NOT NULL DEFAULT 0,
            skills_json TEXT NOT NULL DEFAULT '[]',
            interests_json TEXT NOT NULL DEFAULT '[]',
            level TEXT
        )""")
        conn.commit()

init_db()


def _profile_dict(profile: Any) -> dict[str, Any]:
    return {k: getattr(profile, k) for k in ("skills", "interests", "level", "location", "prefer_online")}


def save_profile(profile: Any) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES ('profile', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps(_profile_dict(profile)),))
        conn.commit()


def load_resume() -> dict[str, Any] | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM resume_profile WHERE id=1").fetchone()
    if not row:
        return None
    return {"filename": row["filename"], "uploaded_at": row["uploaded_at"], "pages": row["pages"],
            "skills": json.loads(row["skills_json"] or "[]"), "interests": json.loads(row["interests_json"] or "[]"),
            "level": row["level"]}


def _load_persisted_state() -> None:
    """Restore the saved profile and resume after a restart (the profile used to reset every time)."""
    global PROFILE, RESUME
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key='profile'").fetchone()
        if row:
            PROFILE = Profile(**json.loads(row[0]))
    except (sqlite3.Error, ValueError, TypeError):
        pass
    RESUME = load_resume()


_load_persisted_state()


def _usage_counter(key: str) -> int:
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
        conn.execute("INSERT OR IGNORE INTO scan_meta (key, value) VALUES (?, 0)", (key,))
        row = conn.execute("SELECT value FROM scan_meta WHERE key=?", (key,)).fetchone()
        conn.commit()
    return max(0, int(row[0] if row else 0))


def tinyfish_agent_steps_used() -> int:
    """Return confirmed Agent steps reported by TinyFish, not estimated reservations."""
    return _usage_counter(TINYFISH_AGENT_OBSERVED_STEPS_KEY)


def tinyfish_agent_steps_reserved() -> int:
    """Return reserved steps for runs whose billable usage has not been confirmed."""
    return _usage_counter(TINYFISH_AGENT_RESERVED_STEPS_KEY)


def tinyfish_agent_budget_consumed() -> int:
    """Budget usage = confirmed steps + safety reservations; not a provider wallet balance."""
    return tinyfish_agent_steps_used() + tinyfish_agent_steps_reserved()


def reserve_tinyfish_agent_run() -> bool:
    """Reserve room for a possible 150-step run without counting it as actual spend."""
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for key, initial in (
            (TINYFISH_AGENT_OBSERVED_STEPS_KEY, TINYFISH_AGENT_INITIAL_OBSERVED_STEPS),
            (TINYFISH_AGENT_RESERVED_STEPS_KEY, 0),
        ):
            conn.execute("INSERT OR IGNORE INTO scan_meta (key, value) VALUES (?, ?)", (key, initial))
        rows = dict(conn.execute(
            "SELECT key, value FROM scan_meta WHERE key IN (?, ?)",
            (TINYFISH_AGENT_OBSERVED_STEPS_KEY, TINYFISH_AGENT_RESERVED_STEPS_KEY),
        ).fetchall())
        observed = max(0, int(rows.get(TINYFISH_AGENT_OBSERVED_STEPS_KEY, 0)))
        reserved = max(0, int(rows.get(TINYFISH_AGENT_RESERVED_STEPS_KEY, 0)))
        if observed + reserved + TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN > TINYFISH_AGENT_STEP_BUDGET:
            conn.commit()
            return False
        conn.execute(
            "UPDATE scan_meta SET value=? WHERE key=?",
            (reserved + TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN, TINYFISH_AGENT_RESERVED_STEPS_KEY),
        )
        conn.commit()
    return True


def finalize_tinyfish_agent_reservation(actual_steps: int | None) -> int:
    """Replace one safety reservation with reported usage; keep it if usage is unknown."""
    if actual_steps is None or actual_steps < 0:
        return 0  # Unknown usage stays in the reserved counter, not observed spend.
    actual = int(actual_steps)
    with closing(sqlite3.connect(DB_PATH, timeout=5)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO scan_meta (key, value) VALUES (?, 0)", (TINYFISH_AGENT_OBSERVED_STEPS_KEY,))
        conn.execute("INSERT OR IGNORE INTO scan_meta (key, value) VALUES (?, 0)", (TINYFISH_AGENT_RESERVED_STEPS_KEY,))
        observed_row = conn.execute("SELECT value FROM scan_meta WHERE key=?", (TINYFISH_AGENT_OBSERVED_STEPS_KEY,)).fetchone()
        reserved_row = conn.execute("SELECT value FROM scan_meta WHERE key=?", (TINYFISH_AGENT_RESERVED_STEPS_KEY,)).fetchone()
        observed = max(0, int(observed_row[0] if observed_row else 0))
        reserved = max(0, int(reserved_row[0] if reserved_row else 0))
        conn.execute("UPDATE scan_meta SET value=? WHERE key=?", (observed + actual, TINYFISH_AGENT_OBSERVED_STEPS_KEY))
        conn.execute(
            "UPDATE scan_meta SET value=? WHERE key=?",
            (max(0, reserved - TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN), TINYFISH_AGENT_RESERVED_STEPS_KEY),
        )
        conn.commit()
    return actual


# Rotate query groups so repeated scans do not ask TinyFish the exact same four queries.
# This broadens discovery over time while keeping API usage bounded per click.
SCAN_QUERY_SETS: list[list[str]] = [
    [
        "site:unstop.com/hackathons 2026 student hackathon registration deadline",
        "site:devfolio.co hackathon 2026 India registration",
        "site:devpost.com/hackathons 2026 student hackathon deadline",
        "site:mlh.io hackathons 2026 student",
    ],
    [
        "site:hackerearth.com/challenges hackathon 2026 registration",
        "site:internshala.com/competitions hackathon 2026 students",
        "site:reskilll.com hackathon 2026 registration",
        "site:hack2skill.com hackathon 2026 students",
    ],
    [
        "site:unstop.com/competitions hackathon 2026 registration open",
        "site:devfolio.co/hackathons student hackathon 2026",
        "site:events.vtools.ieee.org hackathon 2026 students",
        "site:devpost.com/hackathons college hackathon 2026",
    ],
    [
        "site:dev.to hackathon 2026 registration official event link",
        "site:mlh.io/events hackathons 2026",
        "site:hackerearth.com hackathon students 2026 India",
        "site:unstop.com hackathons October 2026 registration",
    ],
]


def next_query_batch() -> tuple[int, list[str]]:
    """Persistently rotate the query batch used by successive live scans."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("INSERT OR IGNORE INTO scan_meta (key, value) VALUES ('query_batch', 0)")
        row = conn.execute("SELECT value FROM scan_meta WHERE key='query_batch'").fetchone()
        counter = int(row[0] if row else 0)
        batch_index = counter % len(SCAN_QUERY_SETS)
        conn.execute("UPDATE scan_meta SET value=? WHERE key='query_batch'", (counter + 1,))
        conn.commit()
    return batch_index, SCAN_QUERY_SETS[batch_index]


def canonical_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Source URL must be an absolute HTTP(S) URL")
    # Drop tracking parameters and fragments to avoid easy duplicates.
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", "", ""))


def source_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host or "Unknown source"


# Social posts may announce events, but they are not authoritative event listings.
# Keep them out of Discover and require any candidate to resolve to a dedicated page.
SOCIAL_MEDIA_DOMAINS = {
    "instagram.com", "facebook.com", "fb.watch", "tiktok.com", "x.com",
    "twitter.com", "threads.net", "linkedin.com", "youtube.com", "youtu.be",
}


def is_social_media_url(url: str) -> bool:
    try:
        host = urlparse(url.strip()).netloc.lower().split(":", 1)[0].removeprefix("www.")
    except (AttributeError, ValueError):
        return False
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL_MEDIA_DOMAINS)

# Platforms prioritized during search. This is a preference list, not an assertion
# that every result on these sites is active or verified.
PREFERRED_EVENT_SOURCE_PRIORITY = {
    "unstop.com": 0,
    "devfolio.co": 1,
    "devpost.com": 2,
    "mlh.io": 3,
    "hackerearth.com": 4,
    "internshala.com": 5,
    "reskilll.com": 6,
    "hack2skill.com": 7,
    "events.vtools.ieee.org": 8,
}
# Blog/community pages are discovery leads only; follow an outbound event link
# rather than presenting the article itself as a hackathon.
DISCOVERY_ONLY_DOMAINS = {"dev.to", "medium.com", "hashnode.com"}


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_discovery_only_url(url: str) -> bool:
    try:
        host = urlparse(url.strip()).netloc.lower().split(":", 1)[0].removeprefix("www.")
    except (AttributeError, ValueError):
        return False
    return any(_host_matches(host, domain) for domain in DISCOVERY_ONLY_DOMAINS)


def source_priority_for_url(url: str) -> int:
    """Return preferred platform priority; unknown sources remain eligible but rank lower."""
    try:
        host = urlparse(url.strip()).netloc.lower().split(":", 1)[0].removeprefix("www.")
    except (AttributeError, ValueError):
        return 100
    for domain, priority in PREFERRED_EVENT_SOURCE_PRIORITY.items():
        if _host_matches(host, domain):
            return priority
    if any(_host_matches(host, domain) for domain in DISCOVERY_ONLY_DOMAINS):
        return 30
    if is_social_media_url(url):
        return 80
    return 50


def search_result_sort_key(item: dict[str, Any]) -> tuple[int, int, int, int]:
    """Prefer known event platforms and specific event URLs over generic pages."""
    url = str(item.get("url") or "")
    title = str(item.get("title") or "")
    path = urlparse(url).path.lower()
    title_and_path = f"{title} {path.replace('-', ' ')}"
    directory_penalty = 1 if DIRECTORY_TITLE_RE.search(title_and_path) else 0
    event_specificity = 0 if re.search(r"/(?:hackathons?|events?|challenges?|competitions?)/[^/?]+", path) else 1
    position = item.get("position", 99)
    try:
        position = int(position)
    except (TypeError, ValueError):
        position = 99
    return (source_priority_for_url(url), directory_penalty, event_specificity, position)


# ---------------------------------------------------------------------------------------------
# Hackathon platforms searched on EVERY scan (one or two TinyFish Search queries each).
# Search and Fetch are free. A platform that returns nothing simply shows 0 in Scan history.
# Limit them with SCAN_SOURCES=unstop,devfolio,mlh  (comma-separated keys); default is all.
# ---------------------------------------------------------------------------------------------
EVENT_SOURCES: list[dict[str, Any]] = [
    {"key": "unstop", "label": "Unstop", "domain": "unstop.com", "queries": ["site:unstop.com hackathons {year} registration open"]},
    {"key": "devfolio", "label": "Devfolio", "domain": "devfolio.co", "queries": ["site:devfolio.co hackathon {year} apply"]},
    {"key": "devpost", "label": "Devpost", "domain": "devpost.com", "queries": ["site:devpost.com hackathons {year} open for registration"]},
    {"key": "mlh", "label": "MLH", "domain": "mlh.io", "queries": ["site:mlh.io hackathon {year} students"]},
    {"key": "hackerearth", "label": "HackerEarth", "domain": "hackerearth.com", "queries": ["site:hackerearth.com hackathon {year} register"]},
    {"key": "devto", "label": "DEV Community", "domain": "dev.to", "queries": ["site:dev.to hackathon {year} register now"]},
    {"key": "hack2skill", "label": "Hack2skill", "domain": "hack2skill.com", "queries": ["site:hack2skill.com hackathon {year}"]},
    {"key": "internshala", "label": "Internshala", "domain": "internshala.com", "queries": ["site:internshala.com hackathon {year} register"]},
    {"key": "reskilll", "label": "Reskilll", "domain": "reskilll.com", "queries": ["site:reskilll.com hackathon {year}"]},
    {"key": "dorahacks", "label": "DoraHacks", "domain": "dorahacks.io", "queries": ["site:dorahacks.io hackathon {year}"]},
    {"key": "ethglobal", "label": "ETHGlobal", "domain": "ethglobal.com", "queries": ["site:ethglobal.com hackathon {year}"]},
    {"key": "taikai", "label": "TAIKAI", "domain": "taikai.network", "queries": ["site:taikai.network hackathon {year}"]},
    {"key": "luma", "label": "Luma", "domain": "lu.ma", "queries": ["site:lu.ma hackathon {year}"]},
    {"key": "hackclub", "label": "Hack Club", "domain": "hackclub.com", "queries": ["site:hackclub.com hackathon {year}"]},
    {"key": "eventbrite", "label": "Eventbrite", "domain": "eventbrite.com", "queries": ["site:eventbrite.com hackathon {year} {location}"]},
    {"key": "townscript", "label": "Townscript", "domain": "townscript.com", "queries": ["site:townscript.com hackathon {year}"]},
    {"key": "hackquest", "label": "HackQuest", "domain": "hackquest.io", "queries": ["site:hackquest.io hackathon {year}"]},
    {"key": "sih", "label": "Smart India Hackathon", "domain": "sih.gov.in", "queries": ["Smart India Hackathon {year} registration official"]},
    {"key": "ieee", "label": "IEEE events", "domain": "events.vtools.ieee.org", "queries": ["site:events.vtools.ieee.org hackathon {year}"]},
    {"key": "web", "label": "Open web", "domain": "", "queries": [
        "hackathon {year} students registration open {location}",
        "online hackathon {year} apply now",
    ]},
]
_SELECTED_SOURCE_KEYS = {k.strip().lower() for k in os.getenv("SCAN_SOURCES", "").split(",") if k.strip()}
ACTIVE_SOURCES = [src for src in EVENT_SOURCES if not _SELECTED_SOURCE_KEYS or src["key"] in _SELECTED_SOURCE_KEYS]
SCAN_MAX_CANDIDATES = max(8, min(60, int(os.getenv("SCAN_MAX_CANDIDATES", "36"))))
SCAN_DETAIL_LINK_LIMIT = 12
# The busiest platforms get a second page of search results (TinyFish `page` parameter). 0 disables it.
SCAN_EXTRA_PAGES = max(0, min(2, int(os.getenv("SCAN_EXTRA_PAGES", "1"))))
BUSY_SOURCE_KEYS = {"unstop", "devfolio", "devpost", "mlh", "hackerearth", "web"}
SCAN_SEARCH_WORKERS = max(1, min(8, int(os.getenv("SCAN_SEARCH_WORKERS", "5"))))
SCAN_HISTORY_KEEP = 100
PREFERRED_EVENT_SOURCE_PRIORITY.update({
    "dorahacks.io": 9, "ethglobal.com": 10, "taikai.network": 11, "lu.ma": 12, "hackclub.com": 13,
    "eventbrite.com": 14, "townscript.com": 15, "hackquest.io": 16, "sih.gov.in": 17,
})


def platform_info(url: str, group_unknown: bool = False) -> tuple[str, str]:
    """(key, label) of the platform a URL belongs to. Unknown sites use their hostname (or 'other' when grouping)."""
    try:
        host = urlparse(url.strip()).netloc.lower().split(":", 1)[0].removeprefix("www.")
    except (AttributeError, ValueError):
        host = ""
    for src in EVENT_SOURCES:
        if src["domain"] and _host_matches(host, src["domain"]):
            return src["key"], src["label"]
    if group_unknown:
        return "other", "Other websites"
    return host or "unknown", host or "Unknown source"


def search_event_sources(year: int) -> tuple[list[dict[str, Any]], list[str], dict[str, dict[str, Any]]]:
    """Run every active platform's queries concurrently. Returns (results, errors, per-source query stats)."""
    location = (getattr(PROFILE, "location", "") or "India").strip()
    sources = list(ACTIVE_SOURCES)
    resume_q = resume_queries(year, location)
    if resume_q:  # searches shaped by the resume, so it changes what gets found, not only how results are ranked
        sources.append({"key": "resume", "label": "Your resume", "domain": "", "queries": [q.replace("{", "").replace("}", "") for q in resume_q]})
    jobs = [(src, q.format(year=year, location=location) if src["key"] != "resume" else q, page)
            for src in sources for q in src["queries"]
            for page in range(1 + (SCAN_EXTRA_PAGES if src["key"] in BUSY_SOURCE_KEYS else 0))]
    stats = {src["key"]: {"queries": 0, "errors": []} for src in sources}
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(SCAN_SEARCH_WORKERS, len(jobs)))) as executor:
        futures = {(executor.submit(fetch_search, query, page) if page else executor.submit(fetch_search, query)): (src, query)
                   for src, query, page in jobs}
        for future in as_completed(futures):
            src, query = futures[future]
            stats[src["key"]]["queries"] += 1
            try:
                results.extend(row for row in future.result() if isinstance(row, dict))
            except HTTPException as exc:
                stats[src["key"]]["errors"].append(str(exc.detail))
                errors.append(str(exc.detail))
            except Exception as exc:
                message = f"Search failed ({type(exc).__name__})."
                stats[src["key"]]["errors"].append(message)
                errors.append(message)
    unique_errors = sorted(set(errors))
    if not results and unique_errors:
        raise HTTPException(status_code=502, detail=f"All TinyFish searches failed. {unique_errors[0]}")
    return results, unique_errors, stats


def build_shortlist(raw: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], set[str]]:
    """Pick up to `limit` unique results, taking turns between platforms so no single site crowds out the rest."""
    groups: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for item in sorted(raw, key=search_result_sort_key):
        url = item.get("url")
        if not isinstance(url, str):
            continue
        try:
            key = canonical_url(url)
        except ValueError:
            continue
        if key in seen:
            continue
        seen.add(key)
        if not is_discovery_only_url(url) and is_article_like(url, str(item.get("title") or "")):
            continue
        groups.setdefault(platform_info(url)[0], []).append(item)
    order = sorted(groups, key=lambda k: (source_priority_for_url(groups[k][0]["url"]), k))
    picked: list[dict[str, Any]] = []
    while len(picked) < limit and any(groups.values()):
        for key in order:
            if groups[key] and len(picked) < limit:
                picked.append(groups[key].pop(0))
    return picked, {canonical_url(item["url"]) for item in picked}


def fetch_pages_many(urls: list[str]) -> dict[str, dict[str, Any]]:
    """TinyFish Fetch accepts 10 URLs per call; fetch larger lists in parallel batches."""
    chunks = [urls[i:i + 10] for i in range(0, len(urls), 10)]
    merged: dict[str, dict[str, Any]] = {}
    if not chunks:
        return merged
    with ThreadPoolExecutor(max_workers=min(3, len(chunks))) as executor:
        for part in executor.map(fetch_pages, chunks):
            merged.update(part)
    return merged


def summarize_platforms(raw: list[dict[str, Any]], shortlist: list[dict[str, Any]], events: list[dict[str, Any]],
                        outcomes: dict[str, str], query_stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-platform funnel for one scan: found by search -> shortlisted -> kept as an event -> new."""
    rows: dict[str, dict[str, Any]] = {}

    def row(key: str, label: str) -> dict[str, Any]:
        return rows.setdefault(key, {"key": key, "label": label, "queries": 0, "results": 0, "shortlisted": 0,
                                     "kept": 0, "new": 0, "errors": []})

    for src in ACTIVE_SOURCES:
        if src["domain"]:
            row(src["key"], src["label"])
    for item in raw:
        if item.get("url"):
            key, label = platform_info(str(item["url"]), group_unknown=True)
            row(key, label)["results"] += 1
    for item in shortlist:
        key, label = platform_info(str(item["url"]), group_unknown=True)
        row(key, label)["shortlisted"] += 1
    for event in events:
        key, label = platform_info(str(event["url"]), group_unknown=True)
        entry = row(key, label)
        entry["kept"] += 1
        if outcomes.get(event["url"]) == "new":
            entry["new"] += 1
    for key, info in query_stats.items():
        if key in rows:
            rows[key]["queries"] = info["queries"]
            rows[key]["errors"] = sorted(set(info["errors"]))
    order = {src["key"]: i for i, src in enumerate(EVENT_SOURCES)}
    return sorted(rows.values(), key=lambda r: (order.get(r["key"], 999), r["label"]))


# ---------------------------------------------------------------------------------------------
# Scan history: every scan is recorded with the listings it saw.
# ---------------------------------------------------------------------------------------------
def save_scan_history(*, mode: str, provider: str, gemini_model: str | None, duration: float, queries: int,
                      platforms: list[dict[str, Any]], candidates: int, fetched: int, new_count: int,
                      updated_count: int, errors: list[str], note: str, records: list[dict[str, Any]]) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.execute(
            """INSERT INTO scan_history (scanned_at, mode, provider, gemini_model, duration_seconds, queries,
               platforms_json, candidates, fetched, kept, new_count, updated_count, errors_json, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (utc_now(), mode, provider, gemini_model, round(duration, 1), queries, json.dumps(platforms),
             candidates, fetched, len(records), new_count, updated_count, json.dumps(errors[:20]), note[:2000]),
        )
        scan_id = int(cursor.lastrowid)
        conn.executemany(
            """INSERT INTO scan_history_events (scan_id, url, title, platform_key, platform_label, outcome, deadline,
               registration_status, format, eligibility, match_score, extraction_provider, verification, tags_json,
               snippet, is_demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(scan_id, r["url"], r["title"], r["platform_key"], r["platform_label"], r["outcome"], r.get("deadline"),
              r.get("registration_status", "unknown"), r.get("format", "Unknown"), r.get("eligibility", "Not verified"),
              int(r.get("match_score") or 0), r.get("extraction_provider", "rules"), r.get("verification", "search_result"),
              json.dumps(r.get("tags", [])), str(r.get("snippet", ""))[:240], int(r.get("is_demo", False))) for r in records],
        )
        conn.execute("DELETE FROM scan_history_events WHERE scan_id NOT IN (SELECT id FROM scan_history ORDER BY id DESC LIMIT ?)", (SCAN_HISTORY_KEEP,))
        conn.execute("DELETE FROM scan_history WHERE id NOT IN (SELECT id FROM scan_history ORDER BY id DESC LIMIT ?)", (SCAN_HISTORY_KEEP,))
        conn.commit()
    return scan_id


def _scan_summary(row: sqlite3.Row) -> dict[str, Any]:
    platforms = json.loads(row["platforms_json"] or "[]")
    return {
        "id": row["id"], "scanned_at": row["scanned_at"], "mode": row["mode"], "provider": row["provider"],
        "gemini_model": row["gemini_model"], "duration_seconds": row["duration_seconds"], "queries": row["queries"],
        "candidates": row["candidates"], "fetched": row["fetched"], "kept": row["kept"], "new_count": row["new_count"],
        "updated_count": row["updated_count"], "errors": json.loads(row["errors_json"] or "[]"), "note": row["note"],
        "platforms": platforms, "platform_count": sum(1 for p in platforms if p.get("results") or p.get("kept")),
    }


def list_scan_history(limit: int = 30) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM scan_history ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
    return [_scan_summary(r) for r in rows]


def get_scan_history(scan_id: int) -> dict[str, Any] | None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scan_history WHERE id=?", (scan_id,)).fetchone()
        if not row:
            return None
        events = conn.execute("SELECT * FROM scan_history_events WHERE scan_id=? ORDER BY match_score DESC, id", (scan_id,)).fetchall()
    out = _scan_summary(row)
    out["events"] = [{**{k: e[k] for k in e.keys() if k != "tags_json"}, "tags": json.loads(e["tags_json"] or "[]"),
                      "is_demo": bool(e["is_demo"])} for e in events]
    return out


_AI_TERMS = ["ai", "artificial intelligence", "machine learning", "ml", "llm", "llms", "genai", "generative ai",
             "deep learning", "nlp", "computer vision", "neural network"]
_WEB3_TERMS = ["web3", "blockchain", "crypto", "smart contract", "smart contracts", "defi", "solidity", "ethereum", "solana"]
_WEBDEV_TERMS = ["web development", "web dev", "full stack", "full-stack", "frontend", "front-end", "backend", "web app", "website"]
_MOBILE_TERMS = ["mobile", "android", "ios", "flutter", "app development", "react native"]
_CLOUD_TERMS = ["cloud", "devops", "aws", "azure", "gcp", "kubernetes", "docker"]
_SEC_TERMS = ["cybersecurity", "cyber security", "infosec", "ctf", "security"]
_IOT_TERMS = ["iot", "internet of things", "embedded", "hardware", "robotics", "arduino"]
_DATA_TERMS = ["data science", "data analysis", "analytics", "big data", "data engineering"]
SYNONYMS: dict[str, list[str]] = {
    "ai": _AI_TERMS, "ai/ml": _AI_TERMS, "ai / ml": _AI_TERMS, "ml": _AI_TERMS, "machine learning": _AI_TERMS,
    "artificial intelligence": _AI_TERMS, "deep learning": _AI_TERMS, "nlp": _AI_TERMS, "llms": _AI_TERMS, "llm": _AI_TERMS,
    "generative ai": _AI_TERMS, "computer vision": _AI_TERMS,
    "web3": _WEB3_TERMS, "blockchain": _WEB3_TERMS, "crypto": _WEB3_TERMS, "smart contracts": _WEB3_TERMS,
    "web development": _WEBDEV_TERMS, "web dev": _WEBDEV_TERMS, "web": _WEBDEV_TERMS, "full stack": _WEBDEV_TERMS,
    "mobile": _MOBILE_TERMS, "cloud / devops": _CLOUD_TERMS, "cloud": _CLOUD_TERMS, "devops": _CLOUD_TERMS,
    "cybersecurity": _SEC_TERMS, "security": _SEC_TERMS,
    "iot / hardware": _IOT_TERMS, "iot": _IOT_TERMS, "hardware": _IOT_TERMS, "robotics": _IOT_TERMS,
    "data": _DATA_TERMS, "data science": _DATA_TERMS,
    "game dev": ["game dev", "game development", "gaming", "game jam", "unity", "unreal", "godot"],
    "health": ["health", "healthcare", "medtech", "medical", "biotech"],
    "fintech": ["fintech", "finance", "payments"],
    "climate": ["climate", "sustainability", "clean energy", "carbon"],
    "edtech": ["edtech", "education"],
    "open source": ["open source", "open-source", "hacktoberfest"],
    "design": ["design", "ui/ux", "ux"],
}


def _term_in(text: str, term: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None


def _alternatives(term: str) -> list[str]:
    key = term.strip().lower()
    return SYNONYMS.get(key, [key]) if key else []


def explain_match(event: dict[str, Any], profile: Any, resume: dict[str, Any] | None = None) -> tuple[int, list[str]]:
    """Score 0-100 plus plain-language reasons. Terms come from the profile and, when present, the resume."""
    resume = RESUME if resume is None else resume
    tags = [str(t) for t in (event.get("tags") or [])]
    title_tags = (str(event.get("title", "")) + " " + " ".join(tags)).lower()
    blob = (title_tags + " " + str(event.get("snippet", ""))).lower()

    targets: list[tuple[str, str]] = []  # (display term, source)
    seen: set[str] = set()
    for source, terms in (("profile", list(profile.skills) + list(profile.interests)),
                          ("resume", list((resume or {}).get("skills", [])) + list((resume or {}).get("interests", [])))):
        for term in terms:
            key = str(term).strip().lower()
            if key and key not in seen:
                seen.add(key)
                targets.append((str(term).strip(), source))

    weight, matched = 0.0, []
    for term, source in targets:
        alts = _alternatives(term)
        if any(_term_in(title_tags, a) for a in alts):
            weight += 1.0
            matched.append((term, source))
        elif any(_term_in(blob, a) for a in alts):
            weight += 0.6
            matched.append((term, source))
    denominator = max(3, min(len(targets), 6))
    score = round(100 * min(weight, denominator) / denominator)

    reasons: list[str] = []
    from_resume = [t for t, src in matched if src == "resume"]
    from_profile = [t for t, src in matched if src == "profile"]
    if from_resume:
        reasons.append("From your resume: " + ", ".join(from_resume[:4]))
    if from_profile:
        reasons.append("Matches your profile: " + ", ".join(from_profile[:3]))
    if str(profile.level).lower() == "student" and "student" in blob:
        score = min(100, score + 10)
        reasons.append("Open to students")
    if profile.prefer_online and str(event.get("format", "Unknown")).lower() == "online":
        score = min(100, score + 5)
        reasons.append("Online, as you prefer")
    return min(100, score), reasons


def calculate_match(event: dict[str, Any], profile: Profile) -> int:
    return explain_match(event, profile)[0]


def rescore_events() -> int:
    """Recompute the stored match score of every saved event (after the profile or resume changes)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id,title,snippet,tags_json,format,eligibility,deadline,url,source,verification,is_demo FROM events").fetchall()
        for row in rows:
            event = dict(row)
            event["tags"] = json.loads(event.pop("tags_json") or "[]")
            conn.execute("UPDATE events SET match_score=? WHERE id=?", (calculate_match(event, PROFILE), row["id"]))
        conn.commit()
    return len(rows)


def parse_date(text: str) -> str | None:
    """Parse a few unambiguous date formats commonly used on event pages."""
    if not text:
        return None
    text = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    patterns = [
        (r"\b(20\d{2}-\d{2}-\d{2})\b", "%Y-%m-%d"),
        (r"\b(\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*,?\s+(?:20\d{2}|\d{2}))\b", "%d %b %Y"),
        (r"\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+(?:20\d{2}|\d{2}))\b", "%b %d %Y"),
    ]
    for pattern, fmt in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        raw = re.sub(r"\s+", " ", match.group(1).replace(",", " ")).strip()
        # Many event pages abbreviate years (e.g. "30 Aug 26"). For this
        # event-discovery use case, interpret two-digit years as 20xx rather
        # than allowing datetime's platform-dependent %y pivot to misclassify them.
        year_match = re.search(r"\b(\d{2})$", raw) if fmt != "%Y-%m-%d" else None
        if year_match:
            raw = raw[:year_match.start(1)] + str(2000 + int(year_match.group(1)))
        try:
            if fmt == "%d %b %Y":
                raw = re.sub(r"January|February|March|April|June|July|August|September|October|November|December", lambda m: m.group(0)[:3], raw, flags=re.IGNORECASE)
            elif fmt == "%b %d %Y":
                raw = re.sub(r"January|February|March|April|June|July|August|September|October|November|December", lambda m: m.group(0)[:3], raw, flags=re.IGNORECASE)
            return datetime.strptime(raw.title() if fmt != "%Y-%m-%d" else raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


DATE_TOKEN = r"(?:20\d{2}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+,?\s+(?:20\d{2}|\d{2})|[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(?:20\d{2}|\d{2}))"
REGISTRATION_DEADLINE_PATTERNS = [
    rf"(?:registration|application)\s+(?:deadline|closes?|closing date|end date|end|ends|last date)\s*(?:is|on|by|:|–|-)?\s*({DATE_TOKEN})",
    rf"(?:last date to (?:register|apply)|register by|apply by|registration closes(?: on)?|applications close(?: on)?)\s*[:–-]?\s*({DATE_TOKEN})",
]
SUBMISSION_DEADLINE_PATTERNS = [
    rf"(?:project\s+)?submission\s+(?:deadline|closes?|closing date|end date|end|ends|last date)\s*(?:is|on|by|:|–|-)?\s*({DATE_TOKEN})",
    rf"(?:last date to submit|submit by|final submission(?: deadline)?|project due)\s*[:–-]?\s*({DATE_TOKEN})",
]
DIRECTORY_TITLE_RE = re.compile(
    r"\b(?:find\s*(?:&|and)\s*join.*hackathons?|all\s+(?:upcoming\s+)?hackathons?|"
    r"hackathon directory|hackathons? in (?:india|your area|one place)|upcoming hackathons?|"
    r"browse hackathons?|hackathon calendar|discover hackathons?)\b", re.I
)
EVENT_TITLE_RE = re.compile(
    r"\b(?:hackathons?|buildathons?|codefests?|hackfests?|challenges?|competitions?|sprints?|"
    r"smart india hackathon|sih|[\w-]*athon)\b", re.I
)
CLOSED_TEXT_RE = re.compile(
    r"(?:registrations?|applications?|submissions?|entries)"
    r"(?:\s+(?:for|to)\b[^.\n]{0,70})?\s+"
    r"(?:(?:is|are|has|have)(?:\s+been)?\s+)?(?:now\s+)?(?:closed|ended|over)\b|"
    r"(?:registration|application|submission) period (?:is |has been )?(?:closed|over|ended)|"
    r"deadline\s+(?:has\s+)?passed|"
    r"no longer accepting (?:applications?|registrations?|submissions?)",
    re.I,
)
OPEN_TEXT_RE = re.compile(
    r"(?:registration|applications?)\s+(?:is\s+|are\s+)?(?:now\s+)?open|"
    r"registration open|applications open|apply now|register now", re.I
)
EVENT_ENDED_TEXT_RE = re.compile(
    r"(?:this\s+)?(?:hackathon|event|competition|challenge|buildathon)\s+"
    r"(?:has\s+(?:ended|concluded|finished)|ended|concluded|is over|finished)|"
    r"event is completed|event has concluded|the event is over", re.I,
)


def _extract_labeled_deadline(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed
    return None


def extract_registration_deadline(text: str) -> str | None:
    """Extract only registration/application deadlines, not project submission dates."""
    return _extract_labeled_deadline(text, REGISTRATION_DEADLINE_PATTERNS)


def extract_submission_deadline(text: str) -> str | None:
    """Extract a separately-labelled submission deadline."""
    return _extract_labeled_deadline(text, SUBMISSION_DEADLINE_PATTERNS)


def extract_event_dates(text: str) -> tuple[str | None, str | None]:
    """Extract explicitly labelled event start/end dates, independently of the Agent extraction layer."""
    source = text or ""
    start_patterns = [
        rf"\b(?:event\s+)?(?:starts?|begins?|starting|start date)\s*[:\-–]?\s*({DATE_TOKEN})",
        rf"\bdate and time\s*[:\-–]?\s*({DATE_TOKEN})",
    ]
    end_patterns = [
        rf"\b(?:event\s+)?(?:ends?|ending|end date|concludes?)\s*[:\-–]?\s*({DATE_TOKEN})",
        rf"\b(?:event\s+)?(?:runs? until|through)\s*[:\-–]?\s*({DATE_TOKEN})",
    ]
    start = _extract_labeled_deadline(source, start_patterns)
    end = _extract_labeled_deadline(source, end_patterns)
    return start, end


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def registration_status(
    deadline: str | None,
    text: str,
    event_end_date: str | None = None,
    submission_deadline: str | None = None,
) -> tuple[str, str]:
    """Conservative status rules; ended events and past submission dates are never called open."""
    today = date.today()
    text = text or ""
    if CLOSED_TEXT_RE.search(text):
        return "closed", "The source text explicitly says registration, applications, or submissions are closed."
    if EVENT_ENDED_TEXT_RE.search(text):
        return "closed", "The source text says this event has ended or concluded."

    parsed_deadline = _as_date(deadline)
    parsed_event_end = _as_date(event_end_date)
    parsed_submission = _as_date(submission_deadline)

    if parsed_event_end and parsed_event_end < today:
        return "closed", f"The event end date ({parsed_event_end.isoformat()}) is in the past."
    if parsed_submission and parsed_submission < today:
        return "closed", f"The submission deadline ({parsed_submission.isoformat()}) is in the past."
    if parsed_deadline and parsed_deadline < today:
        return "closed", f"Registration deadline ({parsed_deadline.isoformat()}) is in the past."
    if parsed_deadline and parsed_deadline == today:
        return "closing_today", "The listed registration deadline is today; confirm the cutoff time with the organizer."

    open_signal = bool(OPEN_TEXT_RE.search(text))
    if parsed_deadline and parsed_deadline > today and open_signal:
        return "open", "The source indicates registration is open and its registration deadline is in the future; confirm on the organizer page."
    if parsed_deadline and parsed_deadline > today:
        return "unknown", "The registration deadline is in the future, but current registration status is not explicit."
    if open_signal:
        return "unknown", "Open-registration wording was found, but no future registration deadline was verified; the wording may be stale."
    return "unknown", "No reliable current registration status could be confirmed from the retrieved text."


def is_directory_page(url: str, title: str, text: str = "") -> bool:
    path = urlparse(url).path.lower().rstrip("/") or "/"
    collection_paths = {
        "/allhacks", "/all-hacks", "/hackathons", "/hackathon", "/events",
        "/opportunities", "/discover", "/competitions", "/contests", "/challenges",
    }
    if path in collection_paths:
        return True
    if DIRECTORY_TITLE_RE.search(title or ""):
        return True
    combined = f"{title} {text[:1000]}".lower()
    # A page that clearly advertises itself as a directory should not become an event card.
    if "hackathon" in combined and any(term in combined for term in ["browse all", "all hacks", "explore events", "find and join", "search hackathons"]):
        return True
    return False


# Marketing/product pages can mention hackathons without listing an event.
MARKETING_PATH_RE = re.compile(
    r"/(?:product|products|solutions|pricing|services|for-organizers|for-organisers|organizers|organisers)(?:/|$)",
    re.I,
)
MARKETING_TITLE_RE = re.compile(
    r"\b(?:host public hackathons? to grow|grow your developer ecosystem|hackathon software|"
    r"hackathon management platform|the leading hackathon platform|host (?:a |your )?public hackathons?|"
    r"public hackathons? for companies|hackathon platform for organizers)\b",
    re.I,
)
MARKETING_COPY_RE = re.compile(
    r"\b(?:book a demo|manage your registrations and project submissions|grow your developer community|"
    r"reach developers globally|run high-impact public hackathons)\b",
    re.I,
)

# Blog posts and guides ABOUT a hackathon are not the hackathon itself.
ARTICLE_PATH_RE = re.compile(
    r"/(?:blog|blogs|article|articles|news|post|posts|story|stories|guide|guides|resource|resources|insight|insights|"
    r"magazine|press|how-to|tips|learn|wiki|faq)(?:/|$)", re.I)
ARTICLE_TITLE_RE = re.compile(
    r"\b(?:how to|what is|what are|why you|guide to|a guide|complete guide|ultimate guide|beginner'?s guide|"
    r"everything (?:you|to) (?:need|know)|things to know|tips(?: and tricks)?|explained|step[- ]by[- ]step|"
    r"(?:people|teams|students) (?:skip|miss|ignore)|before you apply|top \d+|best \d+|\d+ (?:best|top|tips|ways|things)|"
    r"checklist|faqs?)\b", re.I)
ARTICLE_COPY_RE = re.compile(
    r"\b(?:min(?:ute)?s? read|published (?:on|by)|posted (?:on|by)|written by|share this (?:article|post)|"
    r"read more articles|related (?:articles|posts)|table of contents)\b", re.I)


def is_article_like(url: str, title: str, text: str = "") -> bool:
    """True for blog posts, guides and explainers that merely talk about a hackathon."""
    try:
        path = urlparse(str(url or "").strip()).path.lower()
    except (AttributeError, ValueError):
        path = ""
    if ARTICLE_PATH_RE.search(path) or ARTICLE_TITLE_RE.search(str(title or "")):
        return True
    body = str(text or "")[:6000]
    return len(body) > 200 and len({m.group(0).lower() for m in ARTICLE_COPY_RE.finditer(body)}) >= 2


def is_non_event_landing_page(url: str, title: str, text: str = "") -> bool:
    """Reject vendor marketing/product pages and blog articles even if they mention hackathons."""
    try:
        path = urlparse(str(url or "").strip()).path.lower()
    except (AttributeError, ValueError):
        path = ""
    title = str(title or "")
    body = str(text or "")[:6000]
    if is_article_like(url, title, body):
        return True
    if MARKETING_PATH_RE.search(path) or MARKETING_TITLE_RE.search(title):
        return True
    combined = f"{title}\n{body}"
    if MARKETING_COPY_RE.search(combined) and re.search(
        r"\b(?:platform|organizers?|organisers?|companies|ecosystem|marketing support)\b", combined, re.I
    ):
        return True
    return False


def _link_items(raw_links: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(raw_links, list):
        for item in raw_links:
            if isinstance(item, str):
                found.append((item, ""))
            elif isinstance(item, dict):
                url = item.get("url") or item.get("href") or item.get("link")
                label = item.get("text") or item.get("title") or item.get("anchor_text") or item.get("name") or ""
                if isinstance(url, str):
                    found.append((url, str(label)))
    return found


def event_links_from_page(page_url: str, info: dict[str, Any], social_source: bool = False) -> list[dict[str, Any]]:
    """Extract likely individual event links from a directory or social announcement.

    Social posts are never returned as events themselves; this helper only considers
    outbound links that plausibly lead to a dedicated event or registration page.
    """
    text = str(info.get("text") or "")
    pairs = _link_items(info.get("links"))
    # Markdown links are a useful fallback if the API response omits structured link metadata.
    pairs.extend((m.group(2), m.group(1)) for m in re.finditer(r"\[([^\]]{1,160})\]\((https?://[^)\s]+)\)", text))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    base_host = urlparse(page_url).netloc.lower().removeprefix("www.")
    for href, label in pairs:
        absolute = urljoin(page_url, href)
        try:
            clean = canonical_url(absolute)
        except ValueError:
            continue
        parsed = urlparse(clean)
        host = parsed.netloc.lower().removeprefix("www.")
        path = parsed.path.lower().rstrip("/")
        label = re.sub(r"\s+", " ", label).strip()
        if clean in seen or clean == canonical_url(page_url):
            continue
        if is_social_media_url(clean):
            continue
        if host != base_host:
            # From social posts, accept only outbound links that look like an event,
            # registration, official organizer, or known opportunity platform link.
            if social_source:
                context = f"{label} {path} {host}".lower()
                outbound_hint = any(term in context for term in [
                    "hackathon", "athon", "event", "competition", "challenge", "register",
                    "registration", "apply", "official", "unstop.com", "devfolio.co",
                    "reskilll.com", "internshala.com", "devpost.com", "hackerearth.com",
                ])
                if not outbound_hint:
                    continue
            # For normal directories, ignore unrelated external navigation.
            elif not (host.endswith("." + base_host) or "hackathon" in label.lower() or "athon" in label.lower()):
                continue
        if any(x in path for x in ["/login", "/signup", "/sign-up", "/privacy", "/terms", "/about", "/contact", "/blog", "/allhacks"]):
            continue
        eventish = (
            EVENT_TITLE_RE.search(label)
            or EVENT_TITLE_RE.search(path.replace("-", " "))
            or EVENT_TITLE_RE.search(host.replace("-", " "))
            or re.search(r"/(?:event|events|hackathon|hackathons|challenge|challenges|competition|competitions|register|registration|apply)/[^/]*$", path)
            or (social_source and any(term in f"{label} {path} {host}".lower() for term in ["hackathon", "hack", "athon", "competition", "challenge", "register", "apply"]))
        )
        if not eventish:
            continue
        seen.add(clean)
        out.append({"url": clean, "title": label or "Hackathon opportunity", "snippet": "Discovered through an event directory; details are pending source-page extraction.", "site_name": host, "position": 99})
    return out


def infer_format(text: str) -> str:
    low = text.lower()
    online = any(term in low for term in ["online", "virtual", "remote", "online-only"])
    in_person = any(term in low for term in ["in-person", "onsite", "on-site", "offline finale", "physical venue"])
    if online and in_person:
        return "Hybrid"
    if online:
        return "Online"
    if in_person:
        return "In-person"
    return "Unknown"


# TinyFish Agent output schema. Only uses keywords supported by TinyFish structured output.
TINYFISH_AGENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "title_evidence": {"type": "string"},
        "event_kind": {"type": "string", "enum": ["hackathon", "competition", "directory", "other", "unknown"]},
        "is_individual_event": {"type": "boolean"},
        "registration_deadline": {"type": "string"},
        "registration_deadline_evidence": {"type": "string"},
        "submission_deadline": {"type": "string"},
        "submission_deadline_evidence": {"type": "string"},
        "event_start_date": {"type": "string"},
        "event_end_date": {"type": "string"},
        "event_dates_evidence": {"type": "string"},
        "format": {"type": "string", "enum": ["Online", "In-person", "Hybrid", "Unknown"]},
        "format_evidence": {"type": "string"},
        "eligibility": {"type": "string"},
        "eligibility_evidence": {"type": "string"},
        "registration_status_claim": {"type": "string", "enum": ["open", "closed", "unknown"]},
        "status_evidence": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": [
        "title", "title_evidence", "event_kind", "is_individual_event",
        "registration_deadline", "registration_deadline_evidence",
        "submission_deadline", "submission_deadline_evidence",
        "event_start_date", "event_end_date", "event_dates_evidence",
        "format", "format_evidence", "eligibility", "eligibility_evidence",
        "registration_status_claim", "status_evidence", "tags",
    ],
    "propertyOrdering": [
        "title", "title_evidence", "event_kind", "is_individual_event",
        "registration_deadline", "registration_deadline_evidence",
        "submission_deadline", "submission_deadline_evidence",
        "event_start_date", "event_end_date", "event_dates_evidence",
        "format", "format_evidence", "eligibility", "eligibility_evidence",
        "registration_status_claim", "status_evidence", "tags",
    ],
}


def _normalized_quote(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def evidence_is_grounded(evidence: str, source_text: str) -> bool:
    """Require model-provided evidence to be a literal substring of fetched source text."""
    if not evidence or len(evidence.strip()) < 4 or len(evidence) > 500:
        return False
    return _normalized_quote(evidence) in _normalized_quote(source_text)


def _explicit_dates_in_text(text: str) -> set[str]:
    found: set[str] = set()
    for match in re.finditer(DATE_TOKEN, text or "", flags=re.I):
        parsed = parse_date(match.group(0))
        if parsed:
            found.add(parsed)
    # Include every ISO date even if a future date-token pattern is not yet covered.
    for match in re.finditer(r"\b20\d{2}-\d{2}-\d{2}\b", text or ""):
        try:
            found.add(date.fromisoformat(match.group(0)).isoformat())
        except ValueError:
            pass
    return found


def _supported_ai_date(value: Any, evidence: Any, full_text: str) -> str | None:
    """Accept a model date only when it matches an explicit date in a source quote."""
    if not isinstance(value, str) or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        normalized_date = date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None
    if not isinstance(evidence, str) or not evidence_is_grounded(evidence, full_text):
        return None
    if normalized_date not in _explicit_dates_in_text(evidence):
        return None
    return normalized_date


def extract_event_metadata_with_tinyfish_agent(
    url: str,
    page_title: str,
    full_text: str,
    extraction_stats: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Use TinyFish Agent to read one specific event page and return structured metadata.

    Search/Fetch remain free discovery steps. Agent is metered, so every run is gated by a
    conservative app-level estimated step budget and only a small number of pages are tried.
    """
    if not API_KEY or not full_text.strip():
        return None
    if not reserve_tinyfish_agent_run():
        if extraction_stats is not None:
            extraction_stats["budget_skipped"] = extraction_stats.get("budget_skipped", 0) + 1
        print(
            "TinyFish Agent skipped: app-level step budget reached "
            f"({tinyfish_agent_budget_consumed()}/{TINYFISH_AGENT_STEP_BUDGET} steps including unconfirmed reservations)."
        )
        return None

    reserved = TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN
    if extraction_stats is not None:
        extraction_stats["attempted"] = extraction_stats.get("attempted", 0) + 1

    goal = (
        "Inspect this exact candidate webpage for HackScout. Use the live page as the source of truth. "
        "Do not register, submit forms, sign in, or change anything. Do not follow unrelated links. "
        "If the page is clearly one specific hackathon or competition, extract its actual event name, "
        "registration/application deadline, submission deadline, event start and end dates, format, eligibility, "
        "registration status, and concise topic tags. Distinguish registration deadline from submission deadline. "
        "Return dates as YYYY-MM-DD only when a full calendar date and year are visible; otherwise return an empty string. "
        "For each title/date/format/eligibility/status, include a short verbatim evidence quote from the page in the matching evidence field. "
        "If a schedule/rules page is linked directly from this exact event page and is necessary to find dates, you may follow at most one such relevant link on the same event site; do not explore other events. "
        "If this URL is a directory, search page, event calendar, marketing/product page, social post, or generic category instead of a single event, "
        "set is_individual_event=false and event_kind to directory or other. Do not promote page headings like 'Find hackathons' into event names. "
        "If the goal cannot be completed, return empty strings and event_kind=unknown rather than guessing. "
        "Return a JSON object matching the supplied output schema.\n\n"
        f"Candidate title from Search/Fetch: {page_title[:180]}\n"
        f"Fetched-page excerpt for context (may be truncated): {full_text[:2200]}"
    )
    request_body = {
        "url": url,
        "goal": goal,
        "output_schema": TINYFISH_AGENT_OUTPUT_SCHEMA,
        "browser_profile": "lite",
        # max_duration_seconds is supported without the beta-only max_steps field.
        "agent_config": {"max_duration_seconds": TINYFISH_AGENT_MAX_DURATION_SECONDS},
    }
    actual_steps: int | None = None
    try:
        response = httpx.post(
            "https://agent.tinyfish.ai/v1/automation/run",
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            json=request_body,
            timeout=httpx.Timeout(TINYFISH_AGENT_MAX_DURATION_SECONDS + 25, connect=8.0),
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Requests rejected before an Agent run starts are released; other failures are
            # conservatively left reserved because the provider may have begun the run.
            if exc.response.status_code in {400, 401, 402, 403, 404, 429}:
                finalize_tinyfish_agent_reservation(0)
                reserved = 0
            print(f"TinyFish Agent HTTP error {exc.response.status_code}: {exc.response.text[:500]}")
            return None

        payload = response.json()
        raw_steps = payload.get("num_of_steps")
        if isinstance(raw_steps, int) and raw_steps >= 0:
            actual_steps = raw_steps
            finalize_tinyfish_agent_reservation(actual_steps)
            reserved = 0
            if extraction_stats is not None:
                extraction_stats["steps_used"] = extraction_stats.get("steps_used", 0) + actual_steps
        else:
            # Keep the reservation if usage was not returned; this avoids assuming a run was free.
            if extraction_stats is not None:
                extraction_stats["steps_unconfirmed"] = extraction_stats.get("steps_unconfirmed", 0) + TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN

        run_status = str(payload.get("status") or "").upper()
        result = payload.get("result")
        if run_status != "COMPLETED" or not isinstance(result, dict):
            error = payload.get("error")
            message = error.get("message", "no structured result") if isinstance(error, dict) else "no structured result"
            print(f"TinyFish Agent did not complete event extraction (status={run_status or 'unknown'}): {str(message)[:300]}")
            return None
        if result.get("status") == "failure" or result.get("error"):
            print(f"TinyFish Agent goal failed: {str(result.get('reason') or result.get('error') or 'goal not achieved')[:300]}")
            return None
        # Some API responses wrap the output one level deeper, especially when a plain-text
        # result is returned; accept only a dictionary after this small normalization.
        nested = result.get("result")
        if isinstance(nested, dict) and not result.get("title"):
            result = nested
        elif isinstance(nested, str) and not result.get("title"):
            try:
                parsed_nested = json.loads(nested)
                if isinstance(parsed_nested, dict):
                    result = parsed_nested
            except ValueError:
                pass
        if not isinstance(result, dict) or not result.get("event_kind"):
            print("TinyFish Agent result did not contain usable event metadata; using rule-based extraction.")
            return None
        if extraction_stats is not None:
            extraction_stats["succeeded"] = extraction_stats.get("succeeded", 0) + 1
        print(f"TinyFish Agent extracted structured metadata ({actual_steps if actual_steps is not None else 'usage unknown'} steps).")
        return result
    except httpx.TimeoutException:
        # The provider may still have executed/billed the run; keep our conservative reservation.
        if extraction_stats is not None:
            extraction_stats["steps_unconfirmed"] = extraction_stats.get("steps_unconfirmed", 0) + TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN
        print("TinyFish Agent timed out; keeping an unconfirmed reservation separate from actual reported usage.")
        return None
    except httpx.HTTPError as exc:
        if extraction_stats is not None:
            extraction_stats["steps_unconfirmed"] = extraction_stats.get("steps_unconfirmed", 0) + TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN
        print(f"TinyFish Agent request failed ({type(exc).__name__}); reservation remains unconfirmed, using rule-based extraction.")
        return None
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        # If response parsing fails after a run may have occurred, retain the reserve.
        if extraction_stats is not None:
            extraction_stats["steps_unconfirmed"] = extraction_stats.get("steps_unconfirmed", 0) + TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN
        print(f"TinyFish Agent response could not be parsed ({type(exc).__name__}); reservation remains unconfirmed, using rule-based extraction.")
        return None


class GeminiError(Exception):
    """A Gemini call failed. `status` is the HTTP status when there was one."""
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _gemini_error_message(response: Any) -> str:
    status = getattr(response, "status_code", 0)
    detail = ""
    try:
        detail = str((response.json().get("error") or {}).get("message") or "")[:200]
    except Exception:
        detail = str(getattr(response, "text", "") or "")[:200]
    hints = {
        400: "request rejected; if it says the API key is not valid, create a new key in Google AI Studio",
        403: "this key is not allowed to use the Gemini API (wrong key type or API not enabled for the project)",
        404: "model name not found or retired; change GEMINI_MODEL in backend/.env",
        429: "free-tier quota or rate limit reached; wait a minute, or use a Flash-Lite model",
    }
    hint = hints.get(status, "temporary Google-side error" if status >= 500 else "unexpected response")
    return f"Gemini HTTP {status}: {hint}. {detail}".strip()


def _gemini_generate_one(model: str, prompt: str, schema: dict[str, Any] | None, max_tokens: int = 2048) -> str:
    """One generateContent call to one model. Returns the response text or raises GeminiError."""
    gen_config: dict[str, Any] = {"temperature": 0, "maxOutputTokens": max_tokens}
    if schema is not None:
        gen_config.update({"responseMimeType": "application/json", "responseSchema": schema})
    wait = GEMINI_MIN_INTERVAL_SECONDS - (time.monotonic() - _GEMINI_STATE["last_call"])
    if wait > 0:
        time.sleep(wait)
    _GEMINI_STATE["last_call"] = time.monotonic()
    try:
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen_config},
            timeout=GEMINI_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        raise GeminiError("Gemini request timed out.") from None
    except httpx.HTTPError as exc:
        raise GeminiError(f"Could not reach Gemini ({type(exc).__name__}). Check your internet connection.") from None
    if response.status_code >= 400:
        raise GeminiError(_gemini_error_message(response), status=response.status_code)
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        block = (data.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError("Gemini returned no answer" + (f" (blocked: {block})." if block else "."))
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and not part.get("thought"))
    if not text.strip():
        raise GeminiError(f"Gemini returned an empty answer (finishReason={candidates[0].get('finishReason')}).")
    return text


def _gemini_generate(prompt: str, schema: dict[str, Any] | None) -> tuple[str, str]:
    """Try the configured models in order, remembering the first one that works. Returns (text, model)."""
    models = list(GEMINI_MODELS)
    good = _GEMINI_STATE.get("good_model")
    if good in models:
        models.remove(good)
        models.insert(0, good)
    last_error: GeminiError | None = None
    for model in models:
        try:
            text = _gemini_generate_one(model, prompt, schema)
            _GEMINI_STATE["good_model"] = model
            return text, model
        except GeminiError as exc:
            last_error = exc
            # A missing/retired model, an exhausted quota on that model, or a busy server: try the next model.
            if exc.status in {404, 429, 500, 502, 503, 504} or exc.status is None:
                continue
            raise  # key problems (400/401/403) will not be fixed by another model
    raise last_error or GeminiError("No Gemini model is configured.")


def extract_event_metadata_with_gemini(
    url: str,
    page_title: str,
    full_text: str,
    extraction_stats: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Read ONE already-fetched event page with Gemini and return the same metadata shape the
    TinyFish Agent used to return. Nothing here is trusted: normalize_results() accepts a value only
    if its quote is literally present in the fetched page text."""
    stats = extraction_stats if extraction_stats is not None else {}
    if not GEMINI_API_KEY or not full_text.strip() or stats.get("gemini_stopped"):
        return None
    stats["gemini_attempted"] = stats.get("gemini_attempted", 0) + 1
    prompt = (
        "You are extracting facts about ONE hackathon or competition from the text of a web page that was already "
        "fetched. Use only the page text below as the source of truth. Do not use outside knowledge and do not guess. "
        "If the page is clearly one specific hackathon or competition, extract its actual event name, registration/"
        "application deadline, submission deadline, event start and end dates, format, eligibility, registration status "
        "and concise topic tags. Distinguish the registration deadline from the submission deadline. Return dates as "
        "YYYY-MM-DD only when a full calendar date and year are visible; otherwise return an empty string. For each "
        "title/date/format/eligibility/status include a SHORT VERBATIM quote copied word for word from the page in the "
        "matching evidence field (empty string if there is none). If the page is a directory, search page, event "
        "calendar, marketing or product page, social post or generic category instead of a single event, set "
        "is_individual_event=false and event_kind to directory or other, and do not turn headings like 'Find "
        "hackathons' into an event name. If you cannot tell, use empty strings and event_kind=unknown.\n\n"
        f"Page URL: {url}\nCandidate title: {page_title[:180]}\n\nPAGE TEXT:\n{full_text[:14000]}"
    )
    try:
        text, model = _gemini_generate(prompt, TINYFISH_AGENT_OUTPUT_SCHEMA)
    except GeminiError as exc:
        stats["gemini_last_error"] = str(exc)
        # Out of quota or a bad key: stop asking for the rest of this scan and fall back to rules.
        if exc.status in {None, 400, 401, 403, 404, 429}:
            stats["gemini_stopped"] = 1
        print(f"Gemini extraction failed: {exc}")
        return None
    try:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        result = json.loads(cleaned)
    except ValueError:
        stats["gemini_last_error"] = "Gemini returned text that was not valid JSON; rule-based extraction was used."
        print(stats["gemini_last_error"])
        return None
    if not isinstance(result, dict) or not result.get("event_kind"):
        stats["gemini_last_error"] = "Gemini's answer had no event_kind; rule-based extraction was used."
        return None
    stats["gemini_succeeded"] = stats.get("gemini_succeeded", 0) + 1
    stats["gemini_model"] = model  # type: ignore[assignment]
    print(f"Gemini ({model}) extracted structured metadata.")
    return result


def _validated_ai_title(metadata: dict[str, Any] | None, full_text: str, page_title: str) -> str | None:
    if not metadata:
        return None
    candidate = str(metadata.get("title") or "").strip()
    evidence = str(metadata.get("title_evidence") or "").strip()
    if not candidate or not evidence_is_grounded(evidence, full_text):
        return None
    # The candidate should use words visible in its evidence, to resist invented titles.
    candidate_words = [w.casefold() for w in re.findall(r"[A-Za-z0-9]+", candidate) if len(w) > 2]
    evidence_words = {w.casefold() for w in re.findall(r"[A-Za-z0-9]+", evidence)}
    if not candidate_words or sum(w in evidence_words for w in candidate_words) / len(candidate_words) < 0.6:
        return None
    if is_directory_page("https://example.invalid/", candidate, ""):
        return None
    return candidate[:180]


def fetch_search(query: str, page: int = 0) -> list[dict[str, Any]]:
    if not API_KEY:
        raise HTTPException(status_code=503, detail=missing_key_message())
    try:
        response = httpx.get(
            "https://api.search.tinyfish.ai",
            params={"query": query, "location": LOCATION, "language": "en", **({"page": page} if page else {})},
            headers={"X-API-Key": API_KEY},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("results", []) if isinstance(payload, dict) else []
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            detail = "TinyFish rejected the API key. Check backend/.env."
        elif exc.response.status_code == 429:
            detail = "TinyFish rate limit reached. Wait before scanning again."
        else:
            detail = f"TinyFish Search returned HTTP {exc.response.status_code}."
        raise HTTPException(status_code=502, detail=detail) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Could not reach TinyFish Search. Check your connection and try again.") from exc


def search_query_batch(queries: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Search a query batch concurrently so four slow queries do not run serially."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    workers = max(1, min(4, len(queries)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        tasks = {executor.submit(fetch_search, query): query for query in queries}
        for future in as_completed(tasks):
            query = tasks[future]
            try:
                rows = future.result()
                results.extend(row for row in rows if isinstance(row, dict))
            except HTTPException as exc:
                errors.append(str(exc.detail))
            except Exception as exc:
                errors.append(f"Search query failed ({type(exc).__name__}).")
    if not results and errors:
        raise HTTPException(status_code=502, detail=f"All TinyFish searches failed. {errors[0]}")
    return results, errors


def fetch_pages(urls: list[str]) -> dict[str, dict[str, Any]]:
    if not API_KEY or not urls:
        return {}
    try:
        response = httpx.post(
            "https://api.fetch.tinyfish.ai",
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            json={"urls": urls[:10], "format": "markdown", "links": True},
            timeout=35,
        )
        response.raise_for_status()
        data = response.json()
        result: dict[str, dict[str, Any]] = {}
        rows = data.get("results", []) if isinstance(data, dict) else []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url = row.get("url") or row.get("final_url") or row.get("source_url")
                content = row.get("text") or row.get("content") or row.get("markdown") or row.get("body")
                if url and isinstance(content, str):
                    try:
                        key = canonical_url(url)
                    except ValueError:
                        continue
                    info = {
                        "text": content[:30000],
                        "title": str(row.get("title") or ""),
                        "final_url": str(row.get("final_url") or url),
                        "links": row.get("links") or [],
                    }
                    result[key] = info
                    try:
                        result[canonical_url(str(row.get("final_url") or url))] = info
                    except ValueError:
                        pass
        return result
    except httpx.HTTPStatusError as exc:
        print(f"TinyFish Fetch returned HTTP {exc.response.status_code}; source-page text is unavailable for this batch.")
        return {}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        print(f"TinyFish Fetch failed ({type(exc).__name__}); source-page text is unavailable for this batch.")
        return {}


def normalize_results(
    raw_results: list[dict[str, Any]],
    fetched: dict[str, dict[str, Any]],
    use_tinyfish_agent: bool = False,
    extraction_stats: dict[str, int] | None = None,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    # `provider` is "gemini", "rules" or "tinyfish_agent". The old use_tinyfish_agent flag still works
    # when provider is not given.
    chosen = provider if provider is not None else ("tinyfish_agent" if use_tinyfish_agent else "rules")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    agent_calls = 0
    gemini_calls = 0
    for raw in raw_results:
        url = raw.get("url")
        raw_title = str(raw.get("title") or "Untitled opportunity").strip()
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        try:
            clean_url = canonical_url(url)
        except ValueError:
            continue
        if clean_url in seen or is_social_media_url(clean_url) or is_discovery_only_url(clean_url):
            continue
        info = fetched.get(clean_url, {})
        # If a short URL redirects to a social post, don't accept the caption as an event.
        if is_social_media_url(str(info.get("final_url") or "")) or is_discovery_only_url(str(info.get("final_url") or "")):
            continue
        full_text = str(info.get("text") or "")
        fetched_title = str(info.get("title") or "").strip()
        if is_non_event_landing_page(clean_url, fetched_title or raw_title, full_text):
            continue
        page_title = fetched_title if fetched_title and not is_directory_page(clean_url, fetched_title, full_text) else raw_title
        if is_directory_page(clean_url, page_title, full_text) or is_non_event_landing_page(clean_url, page_title, full_text):
            continue

        metadata: dict[str, Any] | None = None
        metadata_source = "rules"
        # Directories and marketing pages were already rejected above, so no extractor call is wasted on them.
        # Gemini is the default extractor. TinyFish Agent is paid per step and runs only when explicitly enabled.
        if full_text.strip():
            if chosen == "tinyfish_agent" and agent_calls < TINYFISH_AGENT_MAX_PAGES_PER_SCAN:
                agent_calls += 1
                metadata = extract_event_metadata_with_tinyfish_agent(clean_url, page_title, full_text, extraction_stats)
                metadata_source = "tinyfish_agent" if metadata else "rules"
            elif chosen == "gemini" and gemini_calls < GEMINI_MAX_PAGES_PER_SCAN:
                gemini_calls += 1
                metadata = extract_event_metadata_with_gemini(clean_url, page_title, full_text, extraction_stats)
                metadata_source = "gemini" if metadata else "rules"

        if metadata and (metadata.get("event_kind") == "directory"
                         or (metadata.get("is_individual_event") is False and metadata.get("event_kind") == "other")):
            continue

        supported_title = _validated_ai_title(metadata, full_text, page_title)
        if supported_title:
            page_title = supported_title
        if is_non_event_landing_page(clean_url, page_title, full_text):
            continue
        # Event pages should mention an event or use an event-like URL/title. TinyFish Agent may
        # help name an event when the crawler's title is merely a hostname/SEO suffix.
        path_text = urlparse(clean_url).path.lower().replace("-", " ")
        model_says_event = bool(metadata and metadata.get("is_individual_event") is True
                                and metadata.get("event_kind") in {"hackathon", "competition"})
        if not (EVENT_TITLE_RE.search(page_title) or EVENT_TITLE_RE.search(path_text)
                or re.search(r"/(?:event|events|challenge|competition)/[^/]+$", path_text)
                or model_says_event):
            continue
        seen.add(clean_url)
        snippet = str(raw.get("snippet") or "").strip()
        combined = f"{page_title}\n{snippet}\n{full_text}"

        # Deterministic regex extraction remains the fallback. LLM values are accepted only
        # when the supporting quote is literally present in the fetched page text.
        deadline = extract_registration_deadline(combined)
        if not deadline:
            deadline_match = re.search(r"(?:registration|application|submission|last)\s+(?:deadline|date)\s*[:\-–]?\s*([^\n.;]{3,60})", combined, re.IGNORECASE)
            deadline = parse_date(deadline_match.group(1)) if deadline_match else None
        submission_deadline = extract_submission_deadline(combined)
        # Use explicitly labelled schedule dates as a deterministic fallback. This is
        # important on event portals where Agent extraction may fail or omit the schedule fields.
        event_start_date, event_end_date = extract_event_dates(combined)
        if metadata and full_text:
            reg_from_llm = _supported_ai_date(metadata.get("registration_deadline"), metadata.get("registration_deadline_evidence"), full_text)
            submit_from_llm = _supported_ai_date(metadata.get("submission_deadline"), metadata.get("submission_deadline_evidence"), full_text)
            date_evidence = metadata.get("event_dates_evidence")
            start_from_llm = _supported_ai_date(metadata.get("event_start_date"), date_evidence, full_text)
            end_from_llm = _supported_ai_date(metadata.get("event_end_date"), date_evidence, full_text)
            if reg_from_llm:
                deadline = reg_from_llm
            if submit_from_llm:
                submission_deadline = submit_from_llm
            if start_from_llm:
                event_start_date = start_from_llm
            if end_from_llm:
                event_end_date = end_from_llm

        fmt = infer_format(combined)
        if metadata and metadata.get("format") in {"Online", "In-person", "Hybrid"}:
            if evidence_is_grounded(str(metadata.get("format_evidence") or ""), full_text):
                fmt = str(metadata["format"])
        eligibility = "Student mentioned; verify on original page" if re.search(r"\bstudent|undergraduate|college\b", combined, re.I) else "Not verified"
        if metadata and isinstance(metadata.get("eligibility"), str) and metadata["eligibility"].strip():
            if evidence_is_grounded(str(metadata.get("eligibility_evidence") or ""), full_text):
                eligibility = metadata["eligibility"].strip()[:240]

        tags: list[str] = []
        for tag, terms in {
            "AI/ML": ["ai", "artificial intelligence", "machine learning", "ml"],
            "Web Development": ["web", "frontend", "backend", "full stack"],
            "Python": ["python"],
            "Open Innovation": ["open innovation", "innovation"],
            "Web3": ["web3", "blockchain"],
            "Hardware": ["hardware", "robotics", "iot", "electronics"],
        }.items():
            if any(re.search(r"\b" + re.escape(term) + r"\b", combined, re.I) for term in terms):
                tags.append(tag)
        if metadata and isinstance(metadata.get("tags"), list):
            allowed_tags = {"AI/ML", "Web Development", "Python", "Open Innovation", "Web3", "Hardware"}
            for tag in metadata["tags"]:
                if tag in allowed_tags and tag not in tags and re.search(re.escape(tag), combined, re.I):
                    tags.append(tag)

        status, status_reason = registration_status(
            deadline,
            combined,
            event_end_date=event_end_date,
            submission_deadline=submission_deadline,
        )
        if metadata and full_text:
            claimed_status = str(metadata.get("registration_status_claim") or "").lower()
            status_evidence = str(metadata.get("status_evidence") or "")
            if claimed_status == "closed" and evidence_is_grounded(status_evidence, combined):
                status = "closed"
                status_reason = "The source evidence explicitly indicates registration or the opportunity is closed."
            elif (claimed_status == "open" and status == "unknown"
                  and evidence_is_grounded(status_evidence, combined)
                  and deadline and _as_date(deadline) and _as_date(deadline) > date.today()):
                status = "open"
                status_reason = "The source evidence indicates registration is open and Python confirmed a future registration deadline."
        normalized.append({
            "title": page_title,
            "url": clean_url,
            "source": source_name(clean_url),
            "snippet": snippet or (full_text[:420].replace("\n", " ").strip() if full_text else "No short description was provided by the search result."),
            "deadline": deadline,
            "event_date": event_start_date,
            "event_end_date": event_end_date,
            "submission_deadline": submission_deadline,
            "extraction_provider": metadata_source,
            "format": fmt,
            "eligibility": eligibility,
            "tags": tags,
            "verification": "page_fetched" if full_text else "search_result",
            "registration_status": status,
            "status_reason": status_reason,
            "is_demo": False,
        })
    return normalized

def upsert_events(events: list[dict[str, Any]], outcomes: dict[str, str] | None = None) -> tuple[int, int]:
    new_count = 0
    updated_count = 0
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        for event in events:
            row = conn.execute(
                "SELECT id, title, snippet, deadline, event_date, event_end_date, submission_deadline, extraction_provider, format, eligibility, verification, registration_status, status_reason FROM events WHERE url = ?",
                (event["url"],),
            ).fetchone()
            now = utc_now()
            score = calculate_match(event, PROFILE)
            values = (
                event["title"], event["url"], event["source"], event["snippet"],
                event.get("deadline"), event.get("event_date"), event.get("event_end_date"),
                event.get("submission_deadline"), event.get("extraction_provider", "rules"),
                event.get("format", "Unknown"), event.get("eligibility", "Not verified"),
                json.dumps(event.get("tags", [])), event.get("verification", "search_result"),
                event.get("registration_status", "unknown"), event.get("status_reason", ""),
                int(event.get("is_demo", False)), now, now, score,
            )
            if row:
                changed_fields = [
                    "title", "snippet", "deadline", "event_date", "event_end_date",
                    "submission_deadline", "extraction_provider", "format", "eligibility",
                    "verification", "registration_status", "status_reason",
                ]
                if any(row[k] != event.get(k) for k in changed_fields):
                    updated_count += 1
                    if outcomes is not None:
                        outcomes[event["url"]] = "updated"
                elif outcomes is not None:
                    outcomes[event["url"]] = "seen"
                conn.execute(
                    """UPDATE events SET title=?, source=?, snippet=?, deadline=?, event_date=?,
                       event_end_date=?, submission_deadline=?, extraction_provider=?, format=?,
                       eligibility=?, tags_json=?, verification=?, registration_status=?, status_reason=?,
                       is_demo=?, last_checked=?, match_score=? WHERE url=?""",
                    (
                        event["title"], event["source"], event["snippet"], event.get("deadline"),
                        event.get("event_date"), event.get("event_end_date"), event.get("submission_deadline"),
                        event.get("extraction_provider", "rules"), event.get("format", "Unknown"),
                        event.get("eligibility", "Not verified"), json.dumps(event.get("tags", [])),
                        event.get("verification", "search_result"), event.get("registration_status", "unknown"),
                        event.get("status_reason", ""), int(event.get("is_demo", False)), now, score, event["url"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO events
                       (title,url,source,snippet,deadline,event_date,event_end_date,submission_deadline,
                        extraction_provider,format,eligibility,tags_json,verification,registration_status,
                        status_reason,is_demo,first_seen,last_checked,match_score)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                new_count += 1
                if outcomes is not None:
                    outcomes[event["url"]] = "new"
        conn.commit()
    return new_count, updated_count

def list_events(include_dismissed: bool = False, include_demo: bool = False) -> list[dict[str, Any]]:
    import json
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        filters = []
        if not include_dismissed:
            filters.append("is_dismissed=0")
        if not include_demo:
            filters.append("is_demo=0")
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        rows = conn.execute(f"SELECT * FROM events {where} ORDER BY is_saved DESC, match_score DESC, first_seen DESC").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json") or "[]")
        item["is_demo"] = bool(item["is_demo"])
        item["platform_label"] = platform_info(item.get("url", ""))[1] if not item["is_demo"] else item.get("source", "")
        item["match_reasons"] = explain_match(item, PROFILE)[1]
        item["is_saved"] = bool(item["is_saved"])
        item["is_dismissed"] = bool(item["is_dismissed"])
        # Re-evaluate old saved rows too, so an earlier scan cannot permanently leave stale status behind.
        extracted_deadline = extract_registration_deadline(f"{item.get('title', '')}\n{item.get('snippet', '')}")
        if not item.get("deadline") and extracted_deadline:
            item["deadline"] = extracted_deadline
        computed_status, computed_reason = registration_status(
            item.get("deadline"),
            f"{item.get('title', '')}\n{item.get('snippet', '')}",
            event_end_date=item.get("event_end_date"),
            submission_deadline=item.get("submission_deadline"),
        )
        stored_status = item.get("registration_status") or "unknown"
        # Never keep a previously stored "open" label if the durable fields don't
        # support it. Full fetched page text isn't persisted, so open must be backed
        # by a future deadline and an open signal retained in the event snippet.
        if (computed_status == "closed" or stored_status == "unknown"
                or (stored_status == "open" and computed_status != "open")):
            item["registration_status"] = computed_status
            item["status_reason"] = computed_reason
        if is_directory_page(item.get("url", ""), item.get("title", ""), item.get("snippet", "")):
            # Do not present a category/landing page as an opportunity.
            continue
        if is_non_event_landing_page(item.get("url", ""), item.get("title", ""), item.get("snippet", "")):
            # Hide older vendor/marketing pages collected before this filter existed.
            continue
        if is_social_media_url(item.get("url", "")):
            # Hide older rows discovered before the social-source filter was added.
            continue
        out.append(item)
    return out

@app.get("/api/health")
def health() -> dict[str, Any]:
    provider = resolve_extraction_provider()
    observed_steps = tinyfish_agent_steps_used()
    reserved_steps = tinyfish_agent_steps_reserved()
    budget_consumed = observed_steps + reserved_steps
    actual_cost = round(observed_steps * TINYFISH_AGENT_STEP_PRICE_USD, 3)
    reserved_exposure = round(reserved_steps * TINYFISH_AGENT_STEP_PRICE_USD, 3)
    estimated_budget = round(TINYFISH_AGENT_STEP_BUDGET * TINYFISH_AGENT_STEP_PRICE_USD, 2)
    budget_remaining = max(0, TINYFISH_AGENT_STEP_BUDGET - budget_consumed)
    agent_ready = provider == "tinyfish_agent" and bool(API_KEY) and budget_remaining >= TINYFISH_AGENT_DEFAULT_MAX_STEPS_PER_RUN
    modes = {
        "gemini": "TinyFish Search + Fetch (free) + Gemini extraction + Python evidence validation",
        "rules": "TinyFish Search + Fetch (free) + rule-based extraction (add GEMINI_API_KEY for better extraction)",
        "tinyfish_agent": "TinyFish Search + Fetch + paid TinyFish Agent + Python validation",
    }
    return {
        "ok": True,
        "env": env_diagnostics(),
        "tinyfish_configured": bool(API_KEY),
        "mode": "live" if API_KEY else "demo-only-until-key-added",
        "event_extraction_provider": provider,
        "extraction_mode": modes[provider],
        "paid_tinyfish_agent_enabled": provider == "tinyfish_agent",
        "gemini_configured": bool(GEMINI_API_KEY),
        "gemini_models": GEMINI_MODELS,
        "gemini_model_last_working": _GEMINI_STATE.get("good_model"),
        "gemini_max_pages_per_scan": GEMINI_MAX_PAGES_PER_SCAN,
        "scan_sources": [{"key": src["key"], "label": src["label"]} for src in ACTIVE_SOURCES],
        "scan_max_candidates": SCAN_MAX_CANDIDATES,
        "tinyfish_agent_configured": agent_ready,
        "tinyfish_agent_max_pages_per_scan": TINYFISH_AGENT_MAX_PAGES_PER_SCAN,
        "tinyfish_agent_max_duration_seconds": TINYFISH_AGENT_MAX_DURATION_SECONDS,
        "tinyfish_agent_step_budget": TINYFISH_AGENT_STEP_BUDGET,
        "tinyfish_agent_steps_used": observed_steps,
        "tinyfish_agent_steps_reserved_unconfirmed": reserved_steps,
        "tinyfish_agent_budget_consumed_including_reserve": budget_consumed,
        "tinyfish_agent_steps_remaining": budget_remaining,
        "tinyfish_agent_reported_cost_usd": actual_cost,
        "tinyfish_agent_unconfirmed_reserve_usd": reserved_exposure,
        "tinyfish_agent_estimated_usd_used": actual_cost,
        "tinyfish_agent_estimated_usd_budget": estimated_budget,
    }


@app.get("/api/gemini-check")
def gemini_check() -> dict[str, Any]:
    """Diagnose Gemini: one tiny structured-output request per configured model, with the exact failure reason."""
    if not GEMINI_API_KEY:
        return {"configured": False, "message": "GEMINI_API_KEY is not set in backend/.env. Create a free key at https://aistudio.google.com/apikey"}
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    results = []
    for model in GEMINI_MODELS:
        try:
            text = _gemini_generate_one(model, 'Return {"ok": true} as JSON.', schema, max_tokens=256)
            parsed = json.loads(text)
            results.append({"model": model, "ok": bool(parsed.get("ok")), "message": "Works, including structured JSON output."})
            _GEMINI_STATE["good_model"] = _GEMINI_STATE["good_model"] or model
        except GeminiError as exc:
            results.append({"model": model, "ok": False, "message": str(exc)})
        except ValueError:
            results.append({"model": model, "ok": False, "message": "Model answered, but not with valid JSON."})
    return {"configured": True, "any_working": any(r["ok"] for r in results), "models": results}


@app.get("/api/profile")
def get_profile() -> Profile:
    return PROFILE

@app.put("/api/profile")
def update_profile(profile: Profile) -> Profile:
    global PROFILE
    PROFILE = profile
    save_profile(PROFILE)
    rescore_events()
    return PROFILE


# ---------------------------------------------------------------------------------------------
# Resume: read locally, keep only skills / interests / level.
# ---------------------------------------------------------------------------------------------
def resume_public(resume: dict[str, Any] | None) -> dict[str, Any]:
    if not resume:
        return {"present": False}
    return {"present": True, **resume, "skill_count": len(resume["skills"])}


def clean_filename(name: str) -> str:
    base = re.split(r"[\\/]", unquote(name or ""))[-1]
    base = re.sub(r"[^\w .()\-]", "", base).strip()
    return (base or "resume.pdf")[:80]


def handle_resume_upload(data: bytes, filename: str) -> dict[str, Any]:
    """Parse a resume PDF, store its skills and interests, and re-rank every saved event."""
    global RESUME
    text, pages = extract_pdf_text(data)
    analysis = analyze_resume(text)
    if not analysis["skills"] and not analysis["interests"]:
        raise ResumeError("This PDF has text, but no recognisable skills were found. Add your skills by hand in the profile.", 422)
    record = {"filename": clean_filename(filename), "uploaded_at": utc_now(), "pages": pages,
              "skills": analysis["skills"], "interests": analysis["interests"], "level": analysis["level"]}
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""INSERT INTO resume_profile (id, filename, uploaded_at, pages, skills_json, interests_json, level)
                        VALUES (1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET filename=excluded.filename, uploaded_at=excluded.uploaded_at,
                        pages=excluded.pages, skills_json=excluded.skills_json, interests_json=excluded.interests_json,
                        level=excluded.level""",
                     (record["filename"], record["uploaded_at"], pages, json.dumps(record["skills"]),
                      json.dumps(record["interests"]), record["level"]))
        conn.commit()
    RESUME = record
    rescored = rescore_events()
    return {**resume_public(RESUME), "events_rescored": rescored}


def resume_queries(year: int, location: str) -> list[str]:
    """Generic hackathon searches built from the resume's strongest areas. No personal details go into them."""
    if not RESUME:
        return []
    terms: list[str] = []
    for term in list(RESUME.get("interests", []))[:2] + list(RESUME.get("skills", []))[:2]:
        word = re.sub(r"\s*/\s*", " ", str(term)).strip()
        if word and word.lower() not in {t.lower() for t in terms}:
            terms.append(word)
    return [f"{term} hackathon {year} registration open {location}" for term in terms[:3]]


@app.get("/api/resume")
def api_get_resume() -> dict[str, Any]:
    return resume_public(RESUME)


@app.post("/api/resume")
async def api_upload_resume(request: Request) -> dict[str, Any]:
    """Send the PDF as the raw request body (Content-Type: application/pdf). The file is not stored."""
    data = await request.body()
    try:
        return await asyncio.to_thread(handle_resume_upload, data, request.headers.get("x-filename", "resume.pdf"))
    except ResumeError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None


@app.delete("/api/resume")
def api_delete_resume() -> dict[str, Any]:
    global RESUME
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM resume_profile")
        conn.commit()
    RESUME = None
    return {"ok": True, "events_rescored": rescore_events()}


@app.get("/api/events")
def get_events(include_dismissed: bool = False) -> list[dict[str, Any]]:
    return list_events(include_dismissed)

def select_next_match(events: list[dict[str, Any]], known_urls: set[str], use_demo: bool = False) -> dict[str, Any] | None:
    """Return a popup candidate only for fresh live events; demo mode is opt-in."""
    candidates = [
        event for event in events
        if not event.get("is_saved")
        and not event.get("is_dismissed")
        and int(event.get("match_score") or 0) >= 15
        and event.get("registration_status") != "closed"
    ]
    if use_demo:
        demos = [event for event in candidates if event.get("is_demo")]
        return demos[0] if demos else None
    # A live "new match" popup says "this is open, apply". Only claim that when the page proved it.
    fresh = [event for event in candidates if not event.get("is_demo") and event.get("url") not in known_urls
             and event.get("registration_status") in {"open", "closing_today"}]
    fresh.sort(key=lambda event: (int(event.get("match_score") or 0), event.get("first_seen", "")), reverse=True)
    return fresh[0] if fresh else None


@app.post("/api/scan")
def scan(request: ScanRequest) -> dict[str, Any]:
    scan_started = time.monotonic()
    platforms_summary: list[dict[str, Any]] = []
    outcomes: dict[str, str] = {}
    raw: list[dict[str, Any]] = []
    shortlist: list[dict[str, Any]] = []
    query_stats: dict[str, dict[str, Any]] = {}
    queries_run = 0
    candidates_count = 0
    fetched_count = 0
    known_live_urls: set[str] = set()
    search_errors: list[str] = []
    extraction_stats: dict[str, int] = {"attempted": 0, "succeeded": 0, "steps_used": 0, "steps_unconfirmed": 0, "budget_skipped": 0,
                                        "gemini_attempted": 0, "gemini_succeeded": 0}
    provider = resolve_extraction_provider()
    if request.use_demo:
        events = DEMO_EVENTS
    else:
        if not API_KEY:
            raise HTTPException(status_code=503, detail=missing_key_message())
        raw, search_errors, query_stats = search_event_sources(date.today().year)
        queries_run = sum(info["queries"] for info in query_stats.values())

        # Take turns between platforms so Unstop, Devfolio, MLH, Dev.to and the rest all get a share of the
        # candidate list instead of one site crowding out the others.
        shortlist, seen = build_shortlist(raw, SCAN_MAX_CANDIDATES)
        candidates_count = len(shortlist)

        # Remember the URLs already in the database. A previously discovered result
        # may be refreshed, but must not trigger the "new match" popup again.
        with closing(sqlite3.connect(DB_PATH)) as conn:
            known_live_urls = {row[0] for row in conn.execute("SELECT url FROM events WHERE is_demo=0").fetchall()}

        first_fetch = fetch_pages_many([item["url"] for item in shortlist])
        detail_links: list[dict[str, Any]] = []
        direct_results: list[dict[str, Any]] = []
        for item in shortlist:
            url = item.get("url", "")
            try:
                clean_url = canonical_url(url)
            except (ValueError, TypeError):
                continue
            info = first_fetch.get(clean_url, {})
            title = str(info.get("title") or item.get("title") or "")
            full_text = str(info.get("text") or "")
            if (is_social_media_url(clean_url) or is_social_media_url(str(info.get("final_url") or ""))
                    or is_discovery_only_url(clean_url) or is_discovery_only_url(str(info.get("final_url") or ""))):
                # Social/blog pages are leads only. Follow outbound links to a dedicated event page.
                detail_links.extend(event_links_from_page(clean_url, info, social_source=True))
            elif is_directory_page(clean_url, title, full_text):
                detail_links.extend(event_links_from_page(clean_url, info))
            else:
                direct_results.append(item)

        # Fetch event-specific pages discovered from directories. Do not save the
        # directory itself as an opportunity.
        second_shortlist: list[dict[str, Any]] = []
        known_urls_in_scan = set(seen)
        for item in detail_links:
            try:
                key = canonical_url(item["url"])
            except (ValueError, TypeError, KeyError):
                continue
            if key not in known_urls_in_scan:
                known_urls_in_scan.add(key)
                second_shortlist.append(item)
            if len(second_shortlist) >= SCAN_DETAIL_LINK_LIMIT:
                break
        second_fetch = fetch_pages_many([item["url"] for item in second_shortlist])
        fetched = {**first_fetch, **second_fetch}
        fetched_count = len(fetched)
        events = normalize_results(
            direct_results + second_shortlist, fetched,
            provider=provider, extraction_stats=extraction_stats,
        )

    new_count, updated_count = upsert_events(events, outcomes)
    if request.use_demo:
        # Demo mode displays only demo fixtures; never mix fictional samples into live results.
        all_events = [event for event in list_events(include_demo=True) if event["is_demo"]]
        # Explicit demo button is allowed to demonstrate a sample popup repeatedly.
        next_match = select_next_match(all_events, known_live_urls, use_demo=True)
        note = "DEMO DATA — these sample entries are fictional and appear only when you choose demo mode."
    else:
        all_events = list_events(include_demo=False)
        # Only newly inserted live URLs are eligible for the modal. Do not fall back
        # to some arbitrary old event if the scan found no new opportunities.
        fresh_candidates = [
            e for e in all_events
            if e["url"] not in known_live_urls and not e["is_demo"]
            and not e["is_saved"] and not e["is_dismissed"]
            and e["match_score"] >= 15 and e.get("registration_status") in {"open", "closing_today"}
        ]
        fresh_candidates.sort(key=lambda e: (e["match_score"], e["first_seen"]), reverse=True)
        next_match = select_next_match(all_events, known_live_urls, use_demo=False)
        if not API_KEY:
            extraction_note = " TinyFish is not configured; only demo mode is available."
        elif provider == "gemini":
            if extraction_stats["gemini_succeeded"]:
                extraction_note = (
                    f" TinyFish Search + Fetch found the pages and Gemini read {extraction_stats['gemini_succeeded']} of them; "
                    "Python kept only values whose quoted evidence appears on the page."
                )
            elif extraction_stats.get("gemini_last_error"):
                extraction_note = f" Gemini could not be used ({extraction_stats['gemini_last_error']}) so rule-based extraction ran instead."
            else:
                extraction_note = " No eligible fetched event page was available for Gemini to read; search results may have been directories, marketing pages or social posts."
        elif provider == "tinyfish_agent":
            if extraction_stats["succeeded"]:
                extraction_note = (
                    f" TinyFish Agent extracted structured details from {extraction_stats['succeeded']} page(s); "
                    "Python validated dates/status where evidence was available."
                )
            elif extraction_stats["attempted"]:
                extraction_note = " TinyFish Agent did not return usable metadata for the attempted page(s); conservative rule-based extraction was used. Check the backend terminal for the Agent error."
            elif extraction_stats["budget_skipped"]:
                extraction_note = " TinyFish Agent calls were skipped because the app-level Agent step budget is nearly exhausted; Search/Fetch and rule-based extraction still ran."
            else:
                extraction_note = " No eligible fetched event page was available for TinyFish Agent to inspect."
        else:
            extraction_note = " TinyFish Search + Fetch ran with rule-based extraction. Add GEMINI_API_KEY to backend/.env for better date and eligibility extraction."
        platforms_summary = summarize_platforms(raw, shortlist, events, outcomes, query_stats)
        if search_errors:
            extraction_note += f" {len(search_errors)} kind(s) of search error occurred; remaining results were still processed."
        if next_match:
            note = f"Scan complete: {len(fresh_candidates)} new matching listing(s). Existing listings were refreshed, and this scan searched {len([p for p in platforms_summary if p['results']])} of {len(ACTIVE_SOURCES)} source groups.{extraction_note}"
        elif new_count or updated_count:
            note = f"Scan complete: {new_count} new listing(s), {updated_count} updated listing(s), but no new active match was eligible for the popup. Existing listings remain in your feed.{extraction_note}"
        else:
            note = f"No new matching event pages survived filtering. Existing listings remain in your feed; {len([p for p in platforms_summary if p['results']])} source groups returned results. This can happen when search returns directories, social posts, expired events, or pages without enough event evidence.{extraction_note}"

    records = []
    for e in events:
        key, label = platform_info(str(e["url"]), group_unknown=False)
        records.append({
            "url": e["url"], "title": e["title"], "platform_key": key,
            "platform_label": e.get("source", label) if request.use_demo else label,
            "outcome": outcomes.get(e["url"], "seen"), "deadline": e.get("deadline"),
            "registration_status": e.get("registration_status", "unknown"), "format": e.get("format", "Unknown"),
            "eligibility": e.get("eligibility", "Not verified"), "match_score": calculate_match(e, PROFILE),
            "extraction_provider": e.get("extraction_provider", "rules"), "verification": e.get("verification", "search_result"),
            "tags": e.get("tags", []), "snippet": e.get("snippet", ""), "is_demo": bool(e.get("is_demo", False)),
        })
    scan_id = save_scan_history(
        mode="demo" if request.use_demo else "live", provider="demo" if request.use_demo else provider,
        gemini_model=extraction_stats.get("gemini_model"), duration=time.monotonic() - scan_started,
        queries=queries_run, platforms=platforms_summary, candidates=candidates_count, fetched=fetched_count,
        new_count=new_count, updated_count=updated_count, errors=search_errors, note=note, records=records,
    )

    return {
        "mode": "demo" if request.use_demo else "live",
        "scan_id": scan_id,
        "platforms": platforms_summary,
        "queries_run": queries_run,
        "candidates_checked": candidates_count,
        "pages_fetched": fetched_count,
        "scanned_at": utc_now(),
        "new_count": new_count,
        "updated_count": updated_count,
        "events_found": len(events),
        "events": all_events,
        "next_match": next_match,
        "search_errors": search_errors,
        "shortlisted_sources": sorted({source_name(str(item.get("url") or "")) for item in (shortlist if not request.use_demo else []) if item.get("url")}),
        "preferred_sources_found": sorted({source_name(str(item.get("url") or "")) for item in (shortlist if not request.use_demo else []) if item.get("url") and source_priority_for_url(str(item.get("url"))) < 50}),
        "extraction_provider": "demo" if request.use_demo else provider,
        "gemini_pages_attempted": extraction_stats.get("gemini_attempted", 0),
        "gemini_pages_succeeded": extraction_stats.get("gemini_succeeded", 0),
        "gemini_model_used": extraction_stats.get("gemini_model"),
        "gemini_last_error": extraction_stats.get("gemini_last_error"),
        "tinyfish_agent_pages_attempted": extraction_stats["attempted"],
        "tinyfish_agent_pages_succeeded": extraction_stats["succeeded"],
        "tinyfish_agent_steps_used_this_scan": extraction_stats["steps_used"],
        "tinyfish_agent_steps_unconfirmed_this_scan": extraction_stats.get("steps_unconfirmed", 0),
        "tinyfish_agent_budget_skipped": extraction_stats["budget_skipped"],
        "tinyfish_agent_total_steps_used": tinyfish_agent_steps_used(),
        "tinyfish_agent_total_steps_reserved_unconfirmed": tinyfish_agent_steps_reserved(),
        "tinyfish_agent_estimated_cost_usd": round(tinyfish_agent_steps_used() * TINYFISH_AGENT_STEP_PRICE_USD, 3),
        "tinyfish_agent_unconfirmed_reserve_usd": round(tinyfish_agent_steps_reserved() * TINYFISH_AGENT_STEP_PRICE_USD, 3),
        "note": note,
    }


@app.get("/api/scans")
def api_scans(limit: int = 30) -> list[dict[str, Any]]:
    """Recent scans, newest first, with a per-platform breakdown for each."""
    return list_scan_history(limit)


@app.get("/api/scans/{scan_id}")
def api_scan_detail(scan_id: int) -> dict[str, Any]:
    """One scan with every listing it saw."""
    scan_record = get_scan_history(scan_id)
    if scan_record is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    return scan_record


@app.delete("/api/scans")
def api_clear_scans() -> dict[str, Any]:
    """Clear scan history only. Your feed, saved and dismissed events are untouched."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM scan_history_events")
        conn.execute("DELETE FROM scan_history")
        conn.commit()
    return {"ok": True}


@app.patch("/api/events/{event_id}")
def update_event(event_id: int, action: str) -> dict[str, Any]:
    if action not in {"save", "unsave", "dismiss", "restore"}:
        raise HTTPException(status_code=400, detail="Unsupported action")
    field, value = {
        "save": ("is_saved", 1), "unsave": ("is_saved", 0),
        "dismiss": ("is_dismissed", 1), "restore": ("is_dismissed", 0),
    }[action]
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cursor = conn.execute(f"UPDATE events SET {field}=? WHERE id=?", (value, event_id))
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True, "id": event_id, "action": action}

@app.delete("/api/events")
def clear_demo_events() -> dict[str, Any]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM events WHERE is_demo=1")
        conn.commit()
    return {"ok": True}
