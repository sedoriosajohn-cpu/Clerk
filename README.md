Clerk — AI Task Organizer & Scheduler

Clerk is an AI-assisted task organization and scheduling system that converts unstructured user input into structured, actionable tasks. Instead of acting like a traditional chatbot, Clerk uses a multi-stage processing pipeline to transform text, emails, and voice notes into organized tasks and scheduling recommendations.

The goal of Clerk is simple: reduce manual task entry and make scheduling effortless.

Features:
-Multi-input task capture
-Direct text input
-Email ingestion (permission-based integrations)
-Audio notes → speech-to-text transcription
-Intelligent task extraction
-Detects tasks, deadlines, and context
-Assigns confidence scores (confirmed vs inferred tasks)
-Smart scheduling assistance
-Suggests time slots based on urgency and availability
-Learns from user habits over time
-Calendar integration
-Syncs with Google Calendar
-Two-way availability management
-Adaptive behavior
-Learns preferred work hours
-Tracks task completion patterns
-Generates automated reminders

Clerk uses a multi-stage AI pipeline with modular feedback loops so each stage can be improved independently.

Pipeline Overview:
-Input Ingestion
-Email via Gmail API
-Audio via browser recording + Whisper STT
-Manual text input
-All sources normalized into plain text + metadata
-AI Extraction
-LLM converts raw text → structured JSON

Extracts:
-Tasks
-Deadlines
-Context
-Priority signals
-Temporal Resolution
-Converts natural language dates
Example: “next Monday” → ISO timestamp
-Confidence Scoring
-Classifies tasks as:
-Confirmed obligations
-Inferred tasks
-Scheduling Engine
-Orders tasks by urgency
-Finds available time slots
-Generates suggested schedules
-Adaptive Learning
-Aggregates usage patterns
-Improves future recommendations
-User Review Interface
-React dashboard for approving schedules
-Syncs approved tasks with Google Calendar

Tech Stack
Backend:
FastAPI
Python
Pydantic
SQLite
OpenAI Whisper (Speech-to-Text)
GPT-4o Mini (Task extraction)
dateparser (Natural language date resolution)

Frontend:
React
HTML / CSS
Media Recorder API
Integrations
Gmail API
Google Calendar API
Deployment
Render
