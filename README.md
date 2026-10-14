# HackScout

A personal hackathon radar. It searches the live web for hackathons that fit your profile, fetches each page,
extracts the key facts, checks every claim against the page text, and pops up new matches with a link to the
original listing.

## Platforms it searches

Every scan runs a TinyFish Search query for each platform below, so listings from all of them can appear in one feed:

Unstop, Devfolio, Devpost, MLH, HackerEarth, DEV Community (dev.to), Hack2skill, Internshala, Reskilll, DoraHacks,
ETHGlobal, TAIKAI, Luma, Hack Club, Eventbrite, Townscript, HackQuest, Smart India Hackathon, IEEE events, and the open web.

- Platforms take turns when choosing which results to fetch, so one busy site can't crowd out the others.
- DEV Community articles are treated as leads: the app follows their outbound links to the real event page.
- Use the platform chips above the feed to filter by site.
- To search fewer sites, set `SCAN_SOURCES=unstop,devfolio,mlh` in `backend/.env`. The platform list lives in
  `EVENT_SOURCES` in `backend/app/main.py`, so adding a site is one new entry.
- A platform that returns nothing for a scan shows 0 in Scan history. It is not hidden.
- The busiest platforms (Unstop, Devfolio, Devpost, MLH, HackerEarth, open web) get a second page of search results.
  Set `SCAN_EXTRA_PAGES=0` to turn that off.
- Blog posts, guides and explainers about a hackathon (for example a "Registration, eligibility and rules" article) are
  filtered out. They are not the event itself.
- The "New match found" popup appears only when the page proves registration is open. Pages with an unknown or closed
  status can still show in the feed, but they never pop up.

## Resume recommendations

Open **Match profile** and choose your resume as a PDF (up to 5 MB). HackScout then:

- reads the PDF on your computer and finds your skills (Python, React, PyTorch and about 100 others), your interest
  areas (AI/ML, Web Development, Web3, Health and so on) and whether you look like a student or a professional;
- re-ranks every listing against those skills, and adds a line to each card and popup saying why it matched
  (for example "From your resume: Python, FastAPI");
- adds a few searches built from your strongest areas (such as "AI ML hackathon 2026 registration open") so the scan
  looks for events that fit you, not only ranks what it already found.

**Privacy.** The PDF is parsed locally with `pypdf`. It is never sent to TinyFish or Gemini. Only the skill list, the
interest areas, the file name and the page count are saved. No name, email, phone number or resume text is stored, and
the PDF itself is not kept. Search queries use generic words only. "Remove" in the same dialog deletes it all.

**Limits.** It needs a text-based PDF. A scanned image has no selectable text, so you'll be asked to export a text
PDF or add skills by hand. Skill detection is keyword-based: it finds the skills in its list, not every skill you have.
Your profile (skills, interests, level, region) is now saved too, so it survives a restart.

## Scan history

The **Scan history** tab (under Workspace) records every scan: when it ran, how long it took, how many searches and
pages it used, a per-platform breakdown (results found, shortlisted, kept, new, and any search errors), and every
listing it saw, marked **New**, **Updated** or **Seen before**. Click a past scan to inspect it, and filter its listings
by platform or outcome. The last 100 scans are kept. "Clear history" removes only the history, never your feed.

**How TinyFish is used (required for Kritical Hacks 2026)**

| Step | Tool | Cost |
|------|------|------|
| Discover event pages across the web | TinyFish **Search API** | free |
| Retrieve each page's real text and links | TinyFish **Fetch API** | free |
| Read the text and pull out dates, format, eligibility | Gemini (free tier), or Python rules | free |
| Keep a value only if its quote appears on the page | Python evidence check | free |

The paid TinyFish **Agent** is off by default. It only runs if you set `EXTRACTION_PROVIDER=tinyfish_agent`.

## Run it

You need Python 3.10+ and Node 18+.

### 1. Backend

```bash
cd backend
python -m venv .venv
# Windows:  .venv\Scripts\activate        macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt   # includes pypdf for reading resumes
```

Create your settings file by copying the example (Windows: `copy .env.example .env`, macOS/Linux: `cp .env.example .env`).
The zip does not include a `.env`, so unzipping a new version can never overwrite your keys. Open `backend/.env` and paste them:

```
TINYFISH_API_KEY=...      # https://agent.tinyfish.ai/api-keys
GEMINI_API_KEY=...        # https://aistudio.google.com/apikey  (free, no card)
```

Start it:

```bash
python -m uvicorn app.main:app --reload
```

Check it at http://127.0.0.1:8001/api/health and http://127.0.0.1:8001/api/gemini-check.

- `event_extraction_provider` should be `gemini` and `paid_tinyfish_agent_enabled` should be `false`.
- `/api/gemini-check` tests each Gemini model and prints the exact reason if one fails.

With no keys the app still starts in demo mode with clearly labelled sample data.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173 and press **Scan web**. A scan now searches about 20 sources, so expect roughly one to two
minutes (Gemini calls are spaced out for the free tier).

### 3. Tests

```bash
cd backend
python -m unittest discover -s tests -v
```

The resume tests that build real PDFs need `pip install reportlab`. Without it those few tests are skipped.

## Troubleshooting

| Message | Fix |
|---------|-----|
| Gemini HTTP 400, API key not valid | Create a new key in Google AI Studio and paste it again |
| Gemini HTTP 403 | Wrong key type, or the API is not enabled for that Google project |
| Gemini HTTP 404 | Model name retired. Edit `GEMINI_MODEL` (comma-separated list) |
| Gemini HTTP 429 | Free quota used up. Wait, or lower `GEMINI_MAX_PAGES_PER_SCAN` |
| TinyFish rate limit reached | Too many searches at once. Set `SCAN_SEARCH_WORKERS=2`, or limit `SCAN_SOURCES` |
| TinyFish rejected the API key | Check `TINYFISH_API_KEY` in `backend/.env` |
| Backend is not connected | Start the backend first, then refresh the page |
| TinyFish API key is not configured (but you added one) | The message names the exact `.env` path it read and why it found no key. Usual causes: the key is in a different copy of the project, the file is called `.env.txt`, the key is in `.env.example`, or the backend was not restarted. Saving `.env` does not reload it: press Ctrl+C and start the backend again |

If Gemini fails, scans still work: the app falls back to rule-based extraction and says so in the scan message.

## Project layout

```
backend/    FastAPI app (app/main.py), tests, .env, requirements.txt
frontend/   React + Vite UI (src/App.jsx, src/styles.css)
```

Never commit `.env`. The `.gitignore` already excludes it, plus the local database.

## Design notes

- **Fetched is not verified.** Fetching a page proves nothing about a claim. A date, format or eligibility is shown as
  confirmed only when its quoted evidence appears word for word in the fetched text.
- **No invented deadlines.** If nothing on the page supports a date, the UI says "Not verified".
- **No popup spam.** Only genuinely new matches open the popup; saved and dismissed events never reopen it.
- **Applications stay on the official site.** HackScout links to the original listing; it never applies for you.

