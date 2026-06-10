<img width="1024" height="1024" alt="image" src="https://github.com/user-attachments/assets/71e93dc1-685b-4c16-8b91-8780c2345b28" />


**Clerk** is an AI-assisted task organization and scheduling system designed to convert unstructured user input into structured, actionable tasks. Instead of acting like a traditional chatbot, Clerk utilizes a multi-stage processing pipeline to transform text, emails, voice notes, PDFs, and work schedule images into organized tasks and scheduling recommendations.

The goal of Clerk is simple: **reduce manual task entry and make scheduling effortless.**

---

## Features
* **Multi-Modal Ingestion:** Raw text, `.txt`/`.pdf` file uploads, image uploads (e.g. work schedules), and audio transcription via OpenAI Whisper.
* **Intelligent Extraction:** Automated identification of deadlines, durations, priorities, and assigners using GPT-5. Falls back to local NLP parsing when no API key is configured.
* **Confidence Scoring:** Each extracted task receives a confidence score. Ambiguous tasks are flagged rather than silently guessed.
* **Google Integration:** Sync tasks and events from Gmail, Google Calendar (all calendars, including birthdays), Google Tasks, and Google Classroom. Auto-sync runs in the background every 15 minutes.
* **Work Schedule Extraction:** Upload a photo or PDF of an employee shift schedule — Clerk finds your row and extracts your shifts as timed reminders.
* **User Accounts:** Register/login with username and password. Supports optional two-factor authentication via email and "Sign in with Google" (OAuth).
* **Password Recovery:** Forgot-password flow sends a reset link to the user's security email.
* **Dark Mode & Preferences:** Per-user settings for preferred name, work hours, dark mode, and notification preferences.

---

## Running Clerk

On Windows, double-click `Start Clerk.bat`. Clerk will start the backend, open the website in your browser, and show the local URL.

You can also run it from a terminal:

```bash
python run_clerk.py
```

The website is served by the app at `http://127.0.0.1:8000`, so users do not need to run a separate frontend server or type a `uvicorn` command.

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
| **AI / ML** | GPT-5 (extraction + vision), OpenAI Whisper (audio) |
| **Frontend** | Vanilla HTML5, CSS3, JavaScript |
| **APIs** | Gmail API, Google Calendar API, Google Tasks API, Google Classroom API, Media Recorder API |
| **Deployment** | Render |

---

## Team

* **Michael Thomas:** mt49932820@gmail.com
* **John Sedoriosa:** sedoriosajohn@gmail.com
* **Chris Nolan:** chrisjnolan30@gmail.com

---
