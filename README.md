<img width="1024" height="1024" alt="image" src="https://github.com/user-attachments/assets/71e93dc1-685b-4c16-8b91-8780c2345b28" />


**Clerk** is an AI-assisted task organization and scheduling system that converts unstructured input into structured, actionable tasks. Instead of acting like a chatbot, Clerk runs a multi-stage processing pipeline that turns text, emails, voice notes, documents, and work-schedule images into organized tasks and scheduling insights.

The goal of Clerk is simple: **reduce manual task entry and make scheduling effortless.**

---

## Features

* **Multi-Modal Ingestion:** Raw text, file uploads (`.pdf`, `.docx`, `.txt`, `.md`, `.csv`), image uploads (e.g. work schedules), live mic recording, and voice-note audio uploads (`.mp3`, `.m4a`, `.wav`, `.ogg`, `.webm`, `.aac`, `.flac`) transcribed via OpenAI Whisper.
* **Intelligent Extraction:** Deadlines, durations, priorities, and assigners identified with GPT-5. Falls back to a local regex/NLP parser when no API key is configured, so the app always works.
* **Confidence Scoring:** Every extracted task gets a 0–100 confidence score built from multiple signals (action verbs, deadline language, date grounding in the source text, ambiguity words, user feedback votes). Locally parsed tasks are capped below AI-verified ones, and dates the AI can't justify from the source are flagged instead of silently trusted.
* **Google Sync:** Imports from Gmail, Google Calendar (all calendars, including birthdays), Google Tasks, and Google Classroom. Sync looks back a configurable window (default **14 days**) so a first sync doesn't flood the list with months-old assignments; auto-sync runs in the background every 15 minutes. Classroom due times are converted from UTC using your real timezone, so DST doesn't shift deadlines.
* **Cross-Source Deduplication:** The same task arriving from different sources (a meeting from Google Calendar and the same meeting in an uploaded PDF, an assignment in both Gmail and Classroom) is merged instead of duplicated — fuzzy title matching, ±1 day tolerance, and authoritative sources (Calendar/Classroom/Tasks) win on dates.
* **Work Schedule Extraction:** Upload a photo or PDF of an employee shift schedule — Clerk locates your row (pixel-level grid detection plus AI vision), extracts your shifts as timed reminders, and validates them against the printed weekly-hours total.
* **Clerk Insights:** A Review page with per-task confidence meters, thumbs-up/down feedback that the scoring learns from, real schedule-conflict detection (only items with actual durations can conflict — five assignments due at 11:59 PM is a busy night, not a clash), and a daily brief that is AI-written when an OpenAI key is configured and rules-based otherwise.
* **User Accounts:** Register/login with username + password, optional email 2FA, "Sign in with Google" (OAuth with PKCE), and a forgot-password flow with emailed reset links.
* **Secured API:** Every data endpoint requires a per-session bearer token issued at sign-in; users can only read or modify their own data. Passwords are bcrypt-hashed, login attempts are rate-limited (5 failures → 15-minute lockout), and sessions are invalidated on logout and password reset.
* **Dark Mode & Preferences:** Per-user preferred name, schedule-match name, work hours, dark mode, and notification settings.
* **Legal Pages:** Terms of Service and Privacy Policy are served at `/terms.html` and `/privacy.html` and linked from the app footer and login screen.

---

## How Extraction Works

1. **Ingest** — input arrives via the text box (`/ingest`), file upload (`/ingest-doc`), mic recording (`/transcribe`), or Google sync. Audio is transcribed first; PDFs/Word docs/text files are converted to plain text; schedule images go through the vision pipeline.
2. **Extract** — large documents are compacted to task-relevant lines and split into overlapping chunks processed in parallel by the AI. Without an API key, a regex-based local parser handles dates ("tomorrow at 4", "next Friday", "07/15", "in 3 days", "tonight"), priorities, and reminder detection.
3. **Verify & Score** — extracted due dates are cross-checked against the source text (including relative phrases like "tomorrow"), and the confidence score is computed from grounding, clarity, and date-quality signals.
4. **Deduplicate & Save** — each task is checked against the user's existing tasks (same/adjacent day + fuzzy title) before saving, so re-submitting, re-syncing, or uploading the same content doesn't create duplicates.

---

## Running Clerk

On Windows, double-click **`Start Clerk.bat`** — on first run it finds Python, creates a virtual environment, and installs dependencies; after that it just starts Clerk and opens your browser.

From a terminal on any platform:

```bash
python run_clerk.py
```

The launcher installs missing dependencies, writes a starter `.env` if none exists, prints which optional features are configured, picks a free port (8000–8003), and serves the website at `http://127.0.0.1:8000` — no separate frontend server needed.

Clerk works out of the box with no configuration: tasks are stored in a local SQLite file (`clerk.db`) and the local parser handles extraction. Add keys to `.env` to unlock the rest.

---

## Configuration (`.env`)

| Variable | Purpose |
| :--- | :--- |
| `OPENAI_API_KEY` | Enables AI extraction, Whisper transcription, vision schedule reading, and AI insight briefs. Set to `disabled` to force the local parser (useful for testing). |
| `OPENAI_MODEL` | Extraction model override (default `gpt-5.4`). `OPENAI_VISION_MODEL` overrides the vision model. |
| `DATABASE_URL` | PostgreSQL URL for hosted deployments. Unset = local SQLite. |
| `GOOGLE_CREDENTIALS_JSON` | Google OAuth client JSON (or place `credentials.json` in the project root) — required for Google sync and Google sign-in. |
| `GOOGLE_REDIRECT_URI` / `CLERK_FRONTEND_URL` | OAuth callback and redirect base URLs for hosted deployments. |
| `GOOGLE_SYNC_PAST_DAYS` | How far back Google sync looks (default `14`). Older items are skipped, and previously imported stale ones are moved to History. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM` | Outgoing email for 2FA codes and password-reset links. |
| `CORS_ALLOWED_ORIGINS` | Comma-separated allowed origins — tighten this in production. |
| `AUTO_SYNC_ENABLED` / `AUTO_SYNC_INTERVAL_SECONDS` / `SYNC_THROTTLE_SECONDS` | Background sync tuning. |
| `CLERK_PORT` / `CLERK_HOST` / `CLERK_NO_BROWSER` | Local launcher options. |

See `backend/.env.example` for the full annotated list.

---

## Project Structure

```
Clerk/
├── run_clerk.py              # One-command local launcher (deps, .env, browser)
├── Start Clerk.bat           # Windows double-click launcher (creates .venv)
├── render.yaml               # Render.com blueprint for one-click cloud deploy
├── backend/
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py           # FastAPI app: auth, ingestion, Google sync, tasks, insights
│   │   └── extractor.py      # AI + local extraction, confidence scoring, schedule vision
│   ├── scripts/
│   │   └── init_db.py        # SQLAlchemy models + schema migrations (SQLite/PostgreSQL)
│   └── tests/
│       ├── test_extractor.py # Unit tests (pytest)
│       └── e2e_smoke.py      # End-to-end API smoke test against a running server
├── frontend/
│   └── clerk_website/
│       ├── index.html        # Single-file app (dashboard, calendar, insights, settings)
│       ├── terms.html        # Terms of Service
│       └── privacy.html      # Privacy Policy
└── docs/                     # Project pitch, proposal, and scope PDFs
```

---

## Security & Privacy

* Sign-in issues a random session token; only its SHA-256 hash is stored. All task, settings, sync, and upload endpoints require the token and enforce per-user ownership.
* Passwords use bcrypt (legacy hashes are upgraded transparently on login). Password resets and logouts invalidate active sessions.
* Failed logins are rate-limited per username. 2FA codes are stored hashed, expire in 10 minutes, and are never returned in API responses when email is configured.
* Google OAuth uses the narrowest read-only scopes; tokens are stored per-user and cleared automatically when revoked.
* Account deletion removes every task, raw input, and user row. Your inputs are processed only to create your tasks — see the in-app Privacy Policy.

---

## Tests

```bash
# Unit tests (no server or API key needed)
python -m pytest backend/tests/test_extractor.py -v

# End-to-end smoke test (run instructions in the file's docstring)
python backend/tests/e2e_smoke.py http://127.0.0.1:8000
```

---

## Deploying With A Free Render URL

This project includes `render.yaml`, so Render can create a hosted Clerk web service from the repo.

1. Push this project to GitHub.
2. In Render, choose **New** > **Blueprint** and select this repo.
3. Render will read `render.yaml` and create the web service.
4. Add these environment variables:
   - `OPENAI_API_KEY`: required for AI extraction and audio transcription.
   - `DATABASE_URL`: recommended for real user data. Use a managed PostgreSQL URL.
   - `GOOGLE_CREDENTIALS_JSON`: required only for Google sync. Paste the full Google OAuth JSON.
   - `CORS_ALLOWED_ORIGINS`: your site origin(s), to lock down cross-origin access.
5. Deploy. Render will give you a public URL automatically.

Open the Render URL directly when possible, for example `https://your-service.onrender.com`.
If you open the frontend from a local file or a static frontend server while the backend is on Render,
open it once with the backend URL in the query string:

```text
frontend/clerk_website/index.html?api_base=https://your-service.onrender.com
```

The frontend remembers that hosted API URL for later visits in the same browser.

For Google sync, add the Render callback URL to your Google OAuth client after Render gives you the URL. It will look like `https://your-service.onrender.com/auth/google/callback`.

Without `DATABASE_URL`, Clerk falls back to SQLite. That is fine for a quick demo, but hosted SQLite data may not survive redeploys or restarts.

---

## Languages and Software

| Component | Technology |
| :--- | :--- |
| **Backend** | FastAPI, Python, Pydantic, SQLAlchemy |
| **Database** | SQLite (local) / PostgreSQL (cloud) |
| **AI / ML** | GPT-5 (extraction + vision), OpenAI Whisper (audio), local regex/NLP fallback |
| **Frontend** | Vanilla HTML5, CSS3, JavaScript (single-file app) |
| **APIs** | Gmail API, Google Calendar API, Google Tasks API, Google Classroom API, MediaRecorder API |
| **Imaging / Docs** | PyMuPDF (PDF text), Pillow (schedule grid detection & image prep) |
| **Auth & Security** | bcrypt, session bearer tokens, Google OAuth 2.0 + PKCE, email 2FA |
| **Deployment** | Render |

---

## Team

* **Michael Thomas:** mt49932820@gmail.com
* **John Sedoriosa:** sedoriosajohn@gmail.com
* **Chris Nolan:** chrisjnolan30@gmail.com

---
