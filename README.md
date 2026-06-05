<img width="1024" height="1024" alt="image" src="https://github.com/user-attachments/assets/71e93dc1-685b-4c16-8b91-8780c2345b28" />


**Clerk** is an AI-assisted task organization and scheduling system designed to convert unstructured user input into structured, actionable tasks. Instead of acting like a traditional chatbot, Clerk utilizes a multi-stage processing pipeline to transform text, emails, and voice notes into organized tasks and scheduling recommendations.

The goal of Clerk is simple: **reduce manual task entry and make scheduling effortless.**

---

## Features
* **Multi-Modal Ingestion:** Support for raw text, `.txt` file uploads, and audio transcriptions.
* **Intelligent Extraction:** Automated identification of deadlines, durations, and priorities.
* **Confidence Scoring:** Ambiguous tasks are flagged for review rather than "guessed" by the AI.
* **Heuristic Scheduling:** An adaptive engine that learns user work-hour preferences and optimizes the daily timeline.

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
   - `OPENAI_API_KEY`: required for AI extraction.
   - `DATABASE_URL`: recommended for real user data. Use a managed PostgreSQL URL.
   - `GOOGLE_CREDENTIALS_JSON`: required only for Google sync. Paste the full Google OAuth JSON.
5. Deploy. Render will give you a public URL automatically.

For Google sync, add the Render callback URL to your Google OAuth client after Render gives you the URL. It will look like `https://your-service.onrender.com/auth/google/callback`.

Without `DATABASE_URL`, Clerk falls back to SQLite. That is fine for a quick demo, but hosted SQLite data may not survive redeploys or restarts.

---
## Languages and Software  

| Component | Technology |
| :--- | :--- |
| **Backend** | FastAPI, Python, Pydantic |
| **Database** | SQLite |
| **AI / ML** | GPT-5, OpenAI Whisper |
| **Frontend** | React, HTML5, CSS3 |
| **APIs** | Gmail API, Google Calendar API, Media Recorder API |
| **Deployment** | Render |

---

## Team

* **Michael Thomas:** mt49932820@gmail.com
* **John Sedoriosa:** sedoriosajohn@gmail.com
* **Chris Nolan:** chrisjnolan30@gmail.com

---
