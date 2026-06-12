# --- IMPORTS AND CONFIGURATION ---
# Standard library and third-party imports needed by the entire app.
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from .extractor import extract_task_from_text, extract_work_schedule_from_image, extract_work_schedule_from_text
try:
    from backend.scripts.init_db import SessionLocal, Task, RawInput, User, ensure_database_schema
except ImportError:
    from scripts.init_db import SessionLocal, Task, RawInput, User, ensure_database_schema
from datetime import datetime, timedelta
import asyncio
import base64
from email.message import EmailMessage
import os
import json
import hashlib
import logging
import secrets
import smtplib
import time
import re
import bcrypt as _bcrypt
from urllib.parse import quote
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# Google API permission scopes — these tell Google exactly what data Clerk can access.
# Using the narrowest scopes possible limits risk if a token is ever compromised.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/tasks.readonly',
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me.readonly'
]

# Calendar event types that are not meaningful tasks/reminders
_SKIP_CALENDAR_EVENT_TYPES = {"focusTime", "outOfOffice", "workingLocation"}

# Sync throttle and auto-sync settings can be overridden via environment variables,
# making it easy to tune behavior without touching code.
SYNC_THROTTLE_SECONDS = int(os.environ.get("SYNC_THROTTLE_SECONDS", "30"))
AUTO_SYNC_ENABLED = os.environ.get("AUTO_SYNC_ENABLED", "1") == "1"
AUTO_SYNC_INTERVAL_SECONDS = int(os.environ.get("AUTO_SYNC_INTERVAL_SECONDS", "900"))
# How far back Google sync looks, in days. Applies to Classroom coursework,
# Gmail messages, Google Tasks due dates, and Calendar events, so a first sync
# doesn't flood the task list with months-old assignments.
GOOGLE_SYNC_PAST_DAYS = int(os.environ.get("GOOGLE_SYNC_PAST_DAYS", "14"))
sync_request_log: dict = {}
auto_sync_task = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_PATH = os.path.join(BASE_DIR, "..", "..", "credentials.json")
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "..", "frontend", "clerk_website")
DEFAULT_GOOGLE_REDIRECT_URI = "http://localhost:8000/auth/google/callback"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:8000"

# In-memory store for transient OAuth states (survives the round-trip; no disk needed).
# Using a dict keyed by a SHA-256 hash of the state string keeps the actual state value secret.
_oauth_states: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: initialise schema and start the background sync loop. Shutdown: cancel it."""
    global auto_sync_task
    ensure_database_schema()
    if AUTO_SYNC_ENABLED and auto_sync_task is None:
        auto_sync_task = asyncio.create_task(auto_sync_loop())
    yield
    if auto_sync_task:
        auto_sync_task.cancel()
        auto_sync_task = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
_log = logging.getLogger("clerk")

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Parse CORS_ALLOWED_ORIGINS from env ("https://myapp.com,https://www.myapp.com").
# Falls back to wildcard so local dev works without any configuration.
_cors_origins_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
_allowed_origins = (
    [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    if _cors_origins_raw
    else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    # Credentials must never be combined with a wildcard origin — browsers reject it
    # and it would weaken the same-origin protections if a real origin list is set.
    allow_credentials=bool(_cors_origins_raw),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    _log.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected server error occurred. Please try again."},
    )

# --- MIDDLEWARE ---
@app.middleware("http")
async def throttle_expensive_sync_routes(request: Request, call_next):
    """Rate-limit sync endpoints so a user can't hammer the Google API repeatedly.
    Returns HTTP 429 with a Retry-After header if the user syncs too frequently."""
    if request.url.path not in {"/sync-gmail", "/sync-classroom", "/sync-all"}:
        return await call_next(request)

    # Use user_id from query string when available; fall back to IP so unauthenticated
    # requests are still throttled.
    user_id = request.query_params.get("user_id") or request.client.host
    key = (request.url.path, user_id)
    now = time.monotonic()
    last_seen = sync_request_log.get(key, 0)
    retry_after = SYNC_THROTTLE_SECONDS - (now - last_seen)

    if retry_after > 0:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Please wait {int(retry_after) + 1} seconds before syncing again."},
            headers={"Retry-After": str(int(retry_after) + 1)}
        )

    sync_request_log[key] = now
    # Prune stale entries to prevent unbounded growth
    if len(sync_request_log) > 500:
        cutoff = now - SYNC_THROTTLE_SECONDS * 10
        stale = [k for k, t in sync_request_log.items() if t < cutoff]
        for k in stale:
            sync_request_log.pop(k, None)
    return await call_next(request)

# --- DATABASE HELPERS ---
def get_db():
    """FastAPI dependency that opens a DB session and guarantees it is closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- UTILITY / SECURITY HELPERS ---
def is_valid_email(value: Optional[str]) -> bool:
    """Return True only if value looks like a real email address (basic regex check)."""
    return bool(value and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))

def _hash_password(password: str, salt: Optional[str] = None) -> str:
    """Hash a password with bcrypt (work factor 12).

    The returned string is the full bcrypt hash and is stored directly in
    the DB — bcrypt embeds its own salt so no separate salt column is needed.
    The `salt` parameter is accepted but ignored to keep callers compatible
    with the old SHA-256 path.
    """
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")

def _verify_password(password: str, stored: Optional[str]) -> bool:
    """Verify a password against a bcrypt hash, a legacy SHA-256 salted hash, or plaintext.

    Migration path (newest → oldest):
      1. bcrypt strings start with '$2b$' or '$2a$' — verified with bcrypt.checkpw.
      2. SHA-256 salted hashes have the form '<32-hex-chars>:<64-hex-chars>' — verified by
         re-hashing the password with the stored salt and comparing digests.
      3. Everything else is treated as a legacy plaintext value.
    On successful login the endpoint re-hashes to bcrypt so old formats are
    upgraded automatically without forcing a password reset.
    """
    if not stored or not password:
        return False
    if stored.startswith(("$2b$", "$2a$", "$2y$")):
        try:
            return _bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            return False
    if ":" in stored:
        # Legacy SHA-256 salted hash — re-derive and compare.
        salt, _ = stored.split(":", 1)
        digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
        return secrets.compare_digest(f"{salt}:{digest}", stored)
    # Legacy plaintext fallback.
    return secrets.compare_digest(password, stored)

def hash_two_factor_code(code: str) -> str:
    """Store a hashed version of the 6-digit code so the plaintext never lives in the DB."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()

def hash_reset_token(token: str) -> str:
    """Hash the password-reset token before storing it, so a DB leak can't be used to reset accounts."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

# --- SESSION TOKEN AUTH ---
# Every data endpoint requires a bearer token issued at login. Without this, anyone
# who guessed a numeric user_id could read or delete another user's tasks.

def issue_session_token(user: User) -> str:
    """Generate a fresh session token for a user and store only its hash.
    The raw token is returned once to the client and never persisted server-side."""
    token = secrets.token_urlsafe(32)
    user.api_token_hash = hash_reset_token(token)
    return token

def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: resolve the Authorization: Bearer header to a User row.
    Raises 401 when the header is missing or the token doesn't match any user."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not signed in. Please sign in again.")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not signed in. Please sign in again.")
    user = db.query(User).filter(User.api_token_hash == hash_reset_token(token)).first()
    if not user:
        raise HTTPException(status_code=401, detail="Your session expired. Please sign in again.")
    return user

def require_same_user(current_user: User, user_id: int):
    """Reject requests where the authenticated user is acting on another user's data."""
    if current_user.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only access your own data.")

# --- LOGIN ATTEMPT THROTTLING ---
# In-memory failed-login tracker: 5 failures per username within 15 minutes → temporary lockout.
_failed_logins: dict = {}
_LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
_LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))

def check_login_throttle(username: str):
    """Raise HTTP 429 if this username has too many recent failed login attempts."""
    now = time.monotonic()
    attempts = [t for t in _failed_logins.get(username, []) if now - t < _LOGIN_LOCKOUT_SECONDS]
    _failed_logins[username] = attempts
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        wait_minutes = int((_LOGIN_LOCKOUT_SECONDS - (now - attempts[0])) / 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed sign-in attempts. Try again in about {wait_minutes} minute(s)."
        )

def record_failed_login(username: str):
    _failed_logins.setdefault(username, []).append(time.monotonic())
    # Keep the tracker bounded.
    if len(_failed_logins) > 1000:
        stale = [k for k, v in _failed_logins.items() if not v or time.monotonic() - v[-1] > _LOGIN_LOCKOUT_SECONDS]
        for k in stale:
            _failed_logins.pop(k, None)

def clear_failed_logins(username: str):
    _failed_logins.pop(username, None)

def send_email_message(to_email: str, subject: str, body: str):
    """Send a plain-text email via SMTP. Falls back to a console print if SMTP is unconfigured."""
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_timeout = float(os.environ.get("SMTP_TIMEOUT_SECONDS", "15"))
    smtp_username = os.environ.get("SMTP_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if smtp_password:
        smtp_password = re.sub(r"\s+", "", smtp_password)
    smtp_from = os.environ.get("SMTP_FROM") or smtp_username

    if not smtp_host or not smtp_from:
        print(f"[email fallback] To: {to_email} | Subject: {subject} | {body}")
        return {"sent": False, "reason": "smtp_not_configured"}

    message = EmailMessage()
    message["To"] = to_email
    message["From"] = smtp_from
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=smtp_timeout) as server:
            server.starttls()
            if smtp_username and smtp_password:
                server.login(smtp_username, smtp_password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        print(f"[email send failed] SMTP authentication failed for {smtp_username}. "
              "If using Gmail, generate a fresh App Password at myaccount.google.com/apppasswords "
              "and update SMTP_PASSWORD in .env")
        return {"sent": False, "reason": "smtp_auth_error"}
    except Exception as exc:
        print(f"[email send failed] To: {to_email} | Subject: {subject} | Reason: {exc}")
        return {"sent": False, "reason": "smtp_error"}

    return {"sent": True}

def send_two_factor_code(user: User):
    """Generate a cryptographically random 6-digit code, hash it for storage, and email it to the user."""
    if not is_valid_email(user.email):
        raise HTTPException(status_code=400, detail="Add a valid email address before enabling 2FA.")

    # secrets.randbelow is cryptographically secure unlike random.randint.
    code = f"{secrets.randbelow(1000000):06d}"
    user.two_factor_code_hash = hash_two_factor_code(code)
    user.two_factor_expires_at = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
    result = send_email_message(
        user.email,
        "Your Clerk verification code",
        f"Your Clerk verification code is {code}. It expires in 10 minutes."
    )
    # Only reveal the code in the API response when SMTP was never configured
    # (local development). Echoing it on transient send failures would let an
    # attacker with a stolen password bypass 2FA whenever email delivery hiccups.
    if not result.get("sent") and result.get("reason") == "smtp_not_configured":
        result["dev_code"] = code
    return result

def verify_two_factor_code(user: User, code: Optional[str]) -> bool:
    """Return True if the submitted code matches the stored hash and hasn't expired yet."""
    if not code or not user.two_factor_code_hash or not user.two_factor_expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(user.two_factor_expires_at)
    except ValueError:
        return False
    if datetime.utcnow() > expires_at:
        return False
    return hash_two_factor_code(code.strip()) == user.two_factor_code_hash

def has_google_token(user_id: int) -> bool:
    """Open a short-lived DB session to check whether the user has a stored Google token."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        return bool(user and user.google_token_json)
    finally:
        db.close()

def user_settings_payload(user: User) -> dict:
    """Serialise all user preference fields into a flat dict safe to send to the frontend."""
    return {
        "preferred_name": user.preferred_name or user.username,
        "schedule_match_name": user.schedule_match_name or "",
        "email": user.email or "",
        "preferred_work_start_hour": user.preferred_work_start_hour if user.preferred_work_start_hour is not None else 9,
        "preferred_work_end_hour": user.preferred_work_end_hour if user.preferred_work_end_hour is not None else 17,
        "dark_mode": bool(user.dark_mode),
        "notifications_enabled": bool(user.notifications_enabled),
        "two_factor_enabled": bool(user.two_factor_enabled),
        "google_connected": has_google_token(user.user_id),
        "google_login": bool(user.google_sub),
    }

def split_name_candidate(value: Optional[str]) -> List[str]:
    """Return multiple name variants for a single string (e.g. 'Jane Smith' → also 'Smith, Jane').
    Work schedules often list names in different orders, so we try all common formats."""
    if not value:
        return []
    text = re.sub(r'[_\-.]+', ' ', str(value)).strip()
    text = re.sub(r'\s+', ' ', text)
    if not text:
        return []

    candidates = [text]
    parts = text.split()
    if len(parts) >= 2:
        candidates.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
        candidates.append(f"{' '.join(parts[:-1])} {parts[-1]}")
    return candidates

def get_user_name_candidates(user: User) -> List[str]:
    """Build a deduplicated list of name strings to match against a work schedule.
    Tries schedule_match_name first, then falls back to preferred_name, username, and email prefix."""
    if user.schedule_match_name:
        return split_name_candidate(user.schedule_match_name)

    candidates = []
    for value in (
        user.preferred_name,
        user.username,
        user.email.split("@", 1)[0] if user.email else None,
    ):
        candidates.extend(split_name_candidate(value))

    seen = set()
    unique = []
    for candidate in candidates:
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique

def ensure_user_exists(user_id: int, db: Session):
    """Raise a 404 if the user doesn't exist; otherwise return the User object."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

def get_password_rule_results(password: str, username: Optional[str] = None) -> dict:
    """Check each password rule individually and return a dict of rule → passed/failed.
    Returning per-rule results lets the frontend show live feedback as the user types."""
    username_text = str(username or "").lower().strip()
    password_text = password or ""
    lowered_password = password_text.lower()
    return {
        "length": len(password_text) >= 12,
        "uppercase": bool(re.search(r"[A-Z]", password_text)),
        "lowercase": bool(re.search(r"[a-z]", password_text)),
        "number": bool(re.search(r"\d", password_text)),
        "special": bool(re.search(r"[^A-Za-z0-9]", password_text)),
        "no_username": not username_text or username_text not in lowered_password,
        "no_spaces": not bool(re.search(r"\s", password_text)),
    }

def validate_strong_password(password: str, username: Optional[str] = None):
    """Raise HTTP 400 listing every failing rule if the password doesn't meet strength requirements."""
    rules = get_password_rule_results(password, username)
    missing_labels = {
        "length": "at least 12 characters",
        "uppercase": "one uppercase letter",
        "lowercase": "one lowercase letter",
        "number": "one number",
        "special": "one symbol",
        "no_username": "cannot include your username",
        "no_spaces": "no spaces",
    }
    missing = [missing_labels[key] for key, passed in rules.items() if not passed]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Password must include {', '.join(missing)}."
        )

# --- PYDANTIC SCHEMAS ---
# Pydantic models validate request bodies automatically; FastAPI rejects malformed requests
# before they even reach the endpoint function.
class LoginRequest(BaseModel):
    username: str
    password: str
    two_factor_code: Optional[str] = None

class UserInput(BaseModel):
    content: str
    source_type: Optional[str] = "text"
    source_id: Optional[str] = None
    local_time: Optional[str] = None
    user_id: int

class TaskUpdate(BaseModel):
    due_date: Optional[str] = None
    end_date: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    item_type: Optional[str] = None
    status: Optional[str] = None
    is_all_day: Optional[bool] = None

class BulkTaskAction(BaseModel):
    task_ids: List[int]

class UserSettingsUpdate(BaseModel):
    preferred_name: Optional[str] = None
    schedule_match_name: Optional[str] = None
    email: Optional[str] = None
    preferred_work_start_hour: Optional[int] = None
    preferred_work_end_hour: Optional[int] = None
    dark_mode: Optional[bool] = None
    notifications_enabled: Optional[bool] = None
    two_factor_enabled: Optional[bool] = None

class TwoFactorSendRequest(BaseModel):
    email: Optional[str] = None

class TwoFactorVerifyRequest(BaseModel):
    email: Optional[str] = None
    code: str

class ForgotPasswordRequest(BaseModel):
    email_or_username: str

class ResetPasswordRequest(BaseModel):
    token: str
    password: str

@app.get("/setup-status")
async def setup_status():
    """Return which optional integrations are configured. Used by the frontend to show a setup banner."""
    try:
        from .extractor import client as _openai_client
    except ImportError:
        from extractor import client as _openai_client

    has_openai   = _openai_client is not None
    has_google   = os.path.exists(CREDS_PATH) or bool(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    has_smtp     = bool(os.environ.get("SMTP_HOST"))

    return {
        "openai":  has_openai,
        "google":  has_google,
        "smtp":    has_smtp,
        "ai_model": os.environ.get("OPENAI_MODEL", "gpt-5.4") if has_openai else None,
    }


class GoogleSetPasswordRequest(BaseModel):
    user_id: int
    password: str

# --- FRONTEND ROUTES ---
@app.get("/")
async def read_index(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """Serve the main SPA page, or complete a Google OAuth redirect if query params are present.
    Google redirects back to '/' with 'code' and 'state' after the user approves access."""
    if code or error:
        return complete_google_oauth(code=code, state=state, error=error)
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

@app.get("/config.js")
async def read_config():
    """Serve a blank config when running locally — the frontend auto-detects the backend from window.location.origin."""
    from fastapi.responses import Response
    return Response(
        content='window.CLERK_API_BASE = window.CLERK_API_BASE || "";',
        media_type="application/javascript",
    )

@app.get("/logo.png")
async def read_logo():
    return FileResponse(os.path.join(FRONTEND_DIR, "logo.png"))

@app.get("/privacy.html")
async def read_privacy():
    return FileResponse(os.path.join(FRONTEND_DIR, "privacy.html"))

@app.get("/terms.html")
async def read_terms():
    return FileResponse(os.path.join(FRONTEND_DIR, "terms.html"))

# --- AUDIO TRANSCRIPTION ---
# Audio formats accepted for voice-note uploads, by extension. Used when the browser
# sends a generic content type (m4a files often arrive as application/octet-stream).
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".webm", ".aac", ".flac", ".mp4", ".mpga", ".mpeg"}
MAX_AUDIO_UPLOAD_BYTES = int(os.environ.get("MAX_AUDIO_UPLOAD_BYTES", str(25 * 1024 * 1024)))  # Whisper API limit

def is_audio_upload(filename: Optional[str], content_type: Optional[str]) -> bool:
    """Detect whether an uploaded file is an audio/voice note by MIME type or extension."""
    if content_type and content_type.startswith("audio/"):
        return True
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in AUDIO_EXTENSIONS and (not content_type or content_type in {
        "application/octet-stream", "video/webm", "video/mp4"
    })

def transcribe_audio_bytes(audio_bytes: bytes, filename: str, content_type: Optional[str]) -> str:
    """Send audio bytes to OpenAI Whisper and return the transcript text.
    Shared by the /transcribe endpoint (mic recordings) and /ingest-doc (voice-note uploads)."""
    try:
        from .extractor import client as openai_client
    except ImportError:
        from extractor import client as openai_client

    if not openai_client:
        raise HTTPException(
            status_code=503,
            detail="OpenAI API key not configured. Add OPENAI_API_KEY to your .env file."
        )
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file received.")
    if len(audio_bytes) > MAX_AUDIO_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Audio file is too large (25 MB max).")

    import io
    audio_buf = io.BytesIO(audio_bytes)
    # Whisper needs a filename to detect the format
    audio_buf.name = filename
    try:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, audio_buf, content_type or "audio/webm"),
        )
        return transcript.text or ""
    except HTTPException:
        raise
    except Exception as e:
        print(f"[transcribe] Whisper error: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")

@app.post("/transcribe")
async def transcribe_audio(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Accept an audio file and return a transcript via OpenAI Whisper.
    Requires a signed-in user so anonymous visitors can't spend the API budget."""
    audio_bytes = await file.read()
    text = await asyncio.to_thread(
        transcribe_audio_bytes, audio_bytes, file.filename or "recording.webm", file.content_type
    )
    return {"text": text}

# --- AUTH ENDPOINTS ---
@app.post("/login")
async def login_user(data: LoginRequest, db: Session = Depends(get_db)):
    """Log a user in, triggering a 2FA challenge first if they have it enabled.
    Returns either a 2FA prompt or the full user object with settings on success."""
    check_login_throttle(data.username)
    user = db.query(User).filter(User.username == data.username).first()
    if not user or not _verify_password(data.password, user.password_hash):
        record_failed_login(data.username)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Silently upgrade legacy (plaintext / SHA-256) hashes to bcrypt on first login.
    if user.password_hash and not user.password_hash.startswith(("$2a$", "$2b$", "$2y$")):
        user.password_hash = _hash_password(data.password)
        db.commit()

    if user.two_factor_enabled:
        if data.two_factor_code:
            if not verify_two_factor_code(user, data.two_factor_code):
                record_failed_login(data.username)
                raise HTTPException(status_code=401, detail="Invalid or expired verification code")
            user.two_factor_code_hash = None
            user.two_factor_expires_at = None
            db.commit()
        else:
            send_result = send_two_factor_code(user)
            db.commit()
            detail = "Enter the verification code sent to your email."
            if not send_result.get("sent"):
                detail = "Enter the verification code below."
            response = {"requires_2fa": True, "message": detail, "email": user.email}
            if send_result.get("dev_code"):
                response["dev_code"] = send_result["dev_code"]
            return response

    clear_failed_logins(data.username)
    token = issue_session_token(user)
    db.commit()
    return {
        "user_id": user.user_id,
        "username": user.username,
        "token": token,
        "settings": user_settings_payload(user),
    }

@app.post("/logout")
async def logout_user(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Invalidate the current session token server-side so it can't be replayed."""
    current_user.api_token_hash = None
    db.commit()
    return {"status": "success", "message": "Signed out."}

@app.post("/register")
async def register_user(data: LoginRequest, db: Session = Depends(get_db)):
    """Create a new account after checking the username is unique and the password is strong."""
    existing_user = db.query(User).filter(User.username == data.username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already taken")
    validate_strong_password(data.password, data.username)
    new_user = User(username=data.username, password_hash=_hash_password(data.password))
    db.add(new_user)
    db.flush()
    token = issue_session_token(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "User created", "user_id": new_user.user_id, "token": token}

@app.post("/forgot-password")
async def forgot_password(data: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    """Send a time-limited password-reset link to the user's registered security email.
    Always returns the same success message so attackers can't tell whether an account exists."""
    lookup = data.email_or_username.strip()
    if not lookup:
        raise HTTPException(status_code=400, detail="Enter your username or security email.")

    user = db.query(User).filter(
        (User.username == lookup) | (User.email == lookup)
    ).first()

    # Avoid account enumeration: respond the same way even if no user/email exists.
    generic_response = {"status": "success", "message": "If that account has a security email, a reset link was sent."}
    if not user:
        print(f"[password reset skipped] No account found for lookup: {lookup}")
        return generic_response
    if not is_valid_email(user.email):
        print(f"[password reset skipped] User {user.user_id} does not have a valid security email.")
        return generic_response

    token = secrets.token_urlsafe(32)
    user.reset_password_token_hash = hash_reset_token(token)
    user.reset_password_expires_at = (datetime.utcnow() + timedelta(minutes=30)).isoformat()
    db.commit()

    frontend_url = get_frontend_url()
    reset_url = f"{frontend_url}?reset_token={quote(token, safe='')}"
    send_result = send_email_message(
        user.email,
        "Reset your Clerk password",
        f"Use this link to reset your Clerk password. It expires in 30 minutes:\n\n{reset_url}"
    )

    if not send_result.get("sent"):
        print(f"[password reset fallback] {reset_url}")
    return generic_response

@app.post("/reset-password")
async def reset_password(data: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Validate a password-reset token (by hash) and, if valid and unexpired, save the new password.
    The token is stored only as a hash so that a DB read alone can't reset accounts."""
    token_hash = hash_reset_token(data.token.strip())
    user = db.query(User).filter(User.reset_password_token_hash == token_hash).first()
    if not user or not user.reset_password_expires_at:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")
    try:
        expires_at = datetime.fromisoformat(user.reset_password_expires_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")
    if datetime.utcnow() > expires_at:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

    validate_strong_password(data.password, user.username)
    user.password_hash = _hash_password(data.password)
    user.reset_password_token_hash = None
    user.reset_password_expires_at = None
    user.two_factor_code_hash = None
    user.two_factor_expires_at = None
    # Invalidate any existing session so a stolen token dies with the old password.
    user.api_token_hash = None
    db.commit()
    return {"status": "success", "message": "Password reset. You can sign in with your new password."}

# --- TASK INGESTION (TEXT) ---
@app.post("/ingest")
async def ingest_task(
    data: UserInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept a block of free-form text from the user, extract tasks via AI, and save them."""
    require_same_user(current_user, data.user_id)
    return await process_and_save_tasks(data.content, data.user_id, data.source_type, db, data.local_time)

# --- TASK INGESTION (DOCUMENTS) ---
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".log", ".text"}

def extract_docx_text(docx_bytes: bytes) -> str:
    """Pull plain text out of a .docx file using only the standard library.

    A .docx is a zip archive; the document body lives in word/document.xml.
    Paragraph tags are converted to newlines and remaining XML tags stripped,
    which is enough for task extraction (no formatting needed).
    """
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
            xml_text = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, KeyError):
        raise HTTPException(status_code=400, detail="Could not read this Word document. Try saving it as PDF or .docx again.")

    xml_text = re.sub(r"</w:p>", "\n", xml_text)
    xml_text = re.sub(r"<w:tab[^>]*/>", "\t", xml_text)
    text = re.sub(r"<[^>]+>", "", xml_text)
    # Unescape the handful of XML entities Word actually emits.
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(entity, char)
    return text.strip()

@app.post("/ingest-doc")
async def ingest_doc(
    user_id: int = Form(...),
    local_time: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept an uploaded file (PDF, Word doc, text, image, or voice note) and extract tasks.
    Image uploads use the AI vision model; audio is transcribed first; documents use the
    standard extractor. The MIME type is checked first, falling back to the file extension
    because browsers often send generic types like application/octet-stream."""
    require_same_user(current_user, user_id)
    content = ""
    file_type = file.content_type or ""
    filename = file.filename or "upload"
    extension = os.path.splitext(filename)[1].lower()
    source_info = f"file: {filename}"

    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if is_audio_upload(filename, file_type):
            # Voice note upload: transcribe with Whisper, then run normal task extraction.
            audio_bytes = await file.read()
            transcript = await asyncio.to_thread(transcribe_audio_bytes, audio_bytes, filename, file_type)
            if not transcript.strip():
                return {
                    "status": "success",
                    "task_ids": [],
                    "message": "No speech detected in this voice note."
                }
            result = await process_and_save_tasks(transcript, user_id, source_info, db, local_time)
            result["transcript"] = transcript
            return result
        elif file_type == "application/pdf" or extension == ".pdf":
            try:
                import fitz
            except ModuleNotFoundError:
                raise HTTPException(
                    status_code=500,
                    detail="PDF support is not installed correctly. Reinstall PyMuPDF to upload PDF documents."
                )
            # Read PDF bytes
            pdf_bytes = await file.read()
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            content = "\n".join(page.get_text() for page in doc)
            doc.close()
        elif extension == ".docx" or file_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            docx_bytes = await file.read()
            content = extract_docx_text(docx_bytes)
        elif file_type in ["text/plain", "text/markdown", "text/csv"] or extension in TEXT_EXTENSIONS:
            # Read Text bytes
            text_bytes = await file.read()
            content = text_bytes.decode("utf-8", errors="replace")
        elif file_type and file_type.startswith("image/"):
            # Check for API key before even reading the bytes so we give a helpful message.
            try:
                from .extractor import client as _img_client
            except ImportError:
                from extractor import client as _img_client
            if not _img_client:
                return {
                    "status": "success",
                    "task_ids": [],
                    "message": "Image extraction requires an OpenAI API key. Add OPENAI_API_KEY to your .env file to enable this feature."
                }
            image_bytes = await file.read()
            structured_tasks = await asyncio.to_thread(
                extract_work_schedule_from_image,
                image_bytes,
                file_type,
                get_user_name_candidates(user),
                local_time
            )
            if not structured_tasks:
                return {
                    "status": "success",
                    "task_ids": [],
                    "message": "No work shifts matched your Clerk profile name. Add your full name in Settings and try again."
                }

            return await save_work_schedule_entries(structured_tasks, user, source_info, db)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {file_type or extension or 'unknown'}. "
                       "Supported: PDF, Word (.docx), text (.txt/.md/.csv), images, and audio voice notes."
            )

        if not content.strip():
            raise HTTPException(status_code=400, detail="The uploaded file appears to be empty.")

        if looks_like_work_schedule(content):
            structured_tasks = await asyncio.to_thread(
                extract_work_schedule_from_text,
                content,
                get_user_name_candidates(user),
                local_time
            )
            if structured_tasks:
                return await save_work_schedule_entries(structured_tasks, user, source_info, db)
            # Detected as a work schedule but couldn't match any shifts to this user.
            # Return a clear message rather than falling through to generic extraction.
            return {
                "status": "success",
                "task_ids": [],
                "message": "This looks like a work schedule but no shifts matched your Clerk profile name. Make sure your full name in Settings matches how it appears on the schedule (e.g. 'Last, First' or 'First Last')."
            }

        # Generic document — extract tasks normally
        return await process_and_save_tasks(content, user_id, source_info, db, local_time)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File processing error: {str(e)}")

# --- GOOGLE OAUTH HELPERS ---

def get_google_credentials_config():
    """Load the Google OAuth client config from an env variable or the credentials.json file.
    The env variable takes priority so deployments don't need a file on disk."""
    credentials_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        try:
            return json.loads(credentials_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="GOOGLE_CREDENTIALS_JSON is not valid JSON.")

    if not os.path.exists(CREDS_PATH):
        raise HTTPException(status_code=500, detail=f"Google credentials are missing. Set GOOGLE_CREDENTIALS_JSON or add credentials.json at {CREDS_PATH}.")

    with open(CREDS_PATH, "r", encoding="utf-8-sig") as creds_file:
        return json.load(creds_file)

def get_google_client_config():
    """Extract the inner 'web' or 'installed' key from the credentials file, whichever exists."""
    config = get_google_credentials_config()
    return config.get("web") or config.get("installed") or config

def get_google_redirect_uri():
    """Determine the OAuth callback URL, preferring explicit config over auto-detection.
    Auto-detects the Render.com hostname so cloud deployments don't need manual config."""
    configured_uri = os.environ.get("GOOGLE_REDIRECT_URI")
    if configured_uri:
        return configured_uri

    render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if render_hostname:
        return f"https://{render_hostname}/auth/google/callback"

    client_config = get_google_client_config()
    redirect_uris = client_config.get("redirect_uris") or []
    if DEFAULT_GOOGLE_REDIRECT_URI in redirect_uris:
        return DEFAULT_GOOGLE_REDIRECT_URI

    return DEFAULT_GOOGLE_REDIRECT_URI

def build_google_flow(state: Optional[str] = None, code_verifier: Optional[str] = None):
    """Create a google_auth_oauthlib Flow object used to generate auth URLs and exchange codes.
    PKCE (code_verifier) is threaded through here to prevent authorization code interception attacks."""
    kwargs = {"redirect_uri": get_google_redirect_uri()}
    if state:
        kwargs["state"] = state
    if code_verifier:
        kwargs["code_verifier"] = code_verifier

    return Flow.from_client_config(
        get_google_credentials_config(),
        scopes=SCOPES,
        **kwargs
    )

def _state_key(state: str) -> str:
    """Hash the OAuth state string so the dict key doesn't expose the raw token."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()

def create_google_auth_url(user_id: int):
    """Generate a Google OAuth authorization URL and save the state so the callback can verify it."""
    flow = build_google_flow()
    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent'
    )
    save_google_oauth_state(state, flow.code_verifier, user_id)
    return auth_url

def get_frontend_url():
    """Return the base URL to redirect users to after OAuth, with environment-based overrides."""
    configured_url = os.environ.get("CLERK_FRONTEND_URL")
    if configured_url:
        return configured_url.rstrip("/")

    render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if render_hostname:
        return f"https://{render_hostname}"

    return DEFAULT_FRONTEND_URL

def save_google_oauth_state(state: str, code_verifier: Optional[str], user_id: int):
    """Store the OAuth state, PKCE verifier, and user_id in memory for the duration of the OAuth round-trip.
    This is keyed by a hash of the state so we can look it up when Google redirects back."""
    _oauth_states[_state_key(state)] = {
        "state": state,
        "code_verifier": code_verifier,
        "user_id": user_id,
        "created_at": datetime.utcnow().isoformat(),
    }
    # Prune states older than 1 hour so the dict doesn't grow unbounded on failed OAuth flows.
    if len(_oauth_states) > 50:
        cutoff = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        stale = [k for k, v in list(_oauth_states.items()) if v.get("created_at", "") < cutoff]
        for k in stale:
            _oauth_states.pop(k, None)

def load_google_oauth_state(state: Optional[str]):
    """Look up a previously saved OAuth state entry by the hashed state string."""
    if not state:
        return {}
    return _oauth_states.get(_state_key(state), {})

def clear_google_oauth_state(state: Optional[str]):
    """Remove a completed OAuth state entry to keep the in-memory dict from growing indefinitely."""
    if not state:
        return
    _oauth_states.pop(_state_key(state), None)

def clear_google_token(user_id: int):
    """Delete the user's stored Google token — called when a token is revoked or invalid."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user:
            user.google_token_json = None
            db.commit()
    finally:
        db.close()

def _save_google_token(user_id: int, token_json: str):
    """Persist the Google OAuth token JSON to the database so it survives server restarts."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user:
            user.google_token_json = token_json
            db.commit()
    finally:
        db.close()

def is_invalid_google_grant(error: Exception) -> bool:
    """Detect whether an exception means the user's Google token has been revoked or expired."""
    message = str(error).lower()
    return "invalid_grant" in message or "expired or revoked" in message

def google_auth_required_response(user_id: int, message: Optional[str] = None):
    """Return a standard dict telling the frontend to redirect the user through Google OAuth."""
    return {
        "auth_url": create_google_auth_url(user_id),
        "message": message or "Google needs to be reconnected. Please sign in again."
    }

# --- GOOGLE OAUTH CALLBACK ---
def complete_google_oauth(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """Process the Google OAuth callback: exchange the code for tokens and save them.
    Handles both the 'connect Google account' flow and the 'log in with Google' flow."""
    frontend_url = get_frontend_url()
    if error:
        return RedirectResponse(url=f"{frontend_url}?google_error={quote(error, safe='')}")
    if not code:
        return RedirectResponse(url=f"{frontend_url}?google_error=missing_authorization_code")

    saved_state = {}
    try:
        saved_state = load_google_oauth_state(state)

        # Recover from in-memory state loss (server restart / new deployment mid-flow).
        # Login flows use a "login_" prefix in the state so we can detect them even when
        # _oauth_states is empty. The code itself is still validated by Google.
        if not saved_state and state and state.startswith("login_"):
            saved_state = {
                "state": state,
                "login_flow": True,
                "login_scopes": [
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile",
                ],
                "user_id": 0,
            }

        if saved_state.get("state") and state and saved_state["state"] != state:
            return RedirectResponse(url=f"{frontend_url}?google_error=oauth_state_mismatch")

        # Login flow: authenticate/register via Google identity
        if saved_state.get("login_flow"):
            return _complete_google_login(code, state, saved_state, frontend_url)

        # Link flow: attach a Google account to an existing Clerk user
        if saved_state.get("link_flow"):
            return _complete_google_link(code, state, saved_state, frontend_url)

        if not saved_state.get("code_verifier"):
            return RedirectResponse(url=f"{frontend_url}?google_error=missing_code_verifier")
        if not saved_state.get("user_id"):
            return RedirectResponse(url=f"{frontend_url}?google_error=missing_google_user")

        flow = build_google_flow(state=state, code_verifier=saved_state["code_verifier"])
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        flow.fetch_token(code=code)
        creds = flow.credentials
        _save_google_token(saved_state["user_id"], creds.to_json())
        clear_google_oauth_state(state)
    except Exception as exc:
        if is_invalid_google_grant(exc):
            if saved_state.get("user_id"):
                clear_google_token(saved_state["user_id"])
            message = "Google access expired. Please connect your Google account again."
        else:
            message = str(exc) or exc.__class__.__name__
        return RedirectResponse(url=f"{frontend_url}?google_error={quote(message, safe='')}")

    return RedirectResponse(url=f"{frontend_url}?google_connected=1&user_id={saved_state['user_id']}")

def _complete_google_login(code: str, state: Optional[str], saved_state: dict, frontend_url: str):
    """Handle the OAuth callback for a login/registration flow."""
    import requests as _requests
    client_config = get_google_client_config()

    # Exchange authorization code for access token directly via HTTP (avoids Flow state issues).
    # The PKCE code_verifier must be included if the library generated one when building the
    # authorization URL — without it Google returns invalid_grant.
    try:
        post_data = {
            "code": code,
            "client_id": client_config["client_id"],
            "client_secret": client_config["client_secret"],
            "redirect_uri": get_google_redirect_uri(),
            "grant_type": "authorization_code",
        }
        code_verifier = saved_state.get("code_verifier")
        if code_verifier:
            post_data["code_verifier"] = code_verifier
        token_resp = _requests.post(
            "https://oauth2.googleapis.com/token",
            data=post_data,
            timeout=10,
        )
        token_data = token_resp.json()
        if "error" in token_data:
            error_msg = f"{token_data['error']}: {token_data.get('error_description', '')}"
            return RedirectResponse(url=f"{frontend_url}?google_error={quote(error_msg, safe='')}")
        access_token = token_data.get("access_token")
        if not access_token:
            return RedirectResponse(url=f"{frontend_url}?google_error={quote('No access token received from Google', safe='')}")
        clear_google_oauth_state(state)
    except Exception as exc:
        return RedirectResponse(url=f"{frontend_url}?google_error={quote(str(exc) or exc.__class__.__name__, safe='')}")

    # Fetch user info from Google
    try:
        userinfo_resp = _requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        userinfo = userinfo_resp.json()
    except Exception as exc:
        return RedirectResponse(url=f"{frontend_url}?google_error={quote('Could not fetch Google profile', safe='')}")

    google_sub = userinfo.get("sub")
    google_email = userinfo.get("email") or ""
    google_name = userinfo.get("name") or ""
    if not google_sub:
        return RedirectResponse(url=f"{frontend_url}?google_error={quote('Google did not return a user identifier', safe='')}")

    # Build a token JSON in the format Credentials.from_authorized_user_info expects,
    # so we can save it immediately and the user is connected for sync after login.
    token_json_to_save = _build_token_json(token_data, client_config)

    db = SessionLocal()
    try:
        # Look for existing user by google_sub
        user = db.query(User).filter(User.google_sub == google_sub).first()

        if user:
            # Existing Google user — update token (scopes may have expanded) and log in.
            if token_json_to_save and token_data.get("refresh_token"):
                user.google_token_json = token_json_to_save
            session_token = issue_session_token(user)
            db.commit()
            return RedirectResponse(
                url=f"{frontend_url}?google_login=1&user_id={user.user_id}"
                    f"&username={quote(user.username, safe='')}&session_token={quote(session_token, safe='')}"
            )

        # New user — auto-generate a username from the Google email.
        base_username = re.sub(r'[^a-z0-9_]', '', google_email.split("@")[0].lower()) or "user"
        username = base_username
        counter = 1
        while db.query(User).filter(User.username == username).first():
            username = f"{base_username}{counter}"
            counter += 1

        new_user = User(
            username=username,
            password_hash=None,
            email=google_email,
            preferred_name=google_name or username,
            google_sub=google_sub,
            google_token_json=token_json_to_save,
        )
        db.add(new_user)
        db.flush()
        session_token = issue_session_token(new_user)
        db.commit()
        db.refresh(new_user)
        # google_new_user=1 tells the frontend this is a first-time registration.
        return RedirectResponse(
            url=f"{frontend_url}?google_new_user=1&user_id={new_user.user_id}"
                f"&username={quote(new_user.username, safe='')}&session_token={quote(session_token, safe='')}"
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(url=f"{frontend_url}?google_error={quote(str(exc), safe='')}")
    finally:
        db.close()


def _build_token_json(token_data: dict, client_config: dict) -> Optional[str]:
    """Convert a raw Google token-exchange response into the JSON string that
    Credentials.from_authorized_user_info() can load.  Returns None if
    the essential fields are missing."""
    access_token = token_data.get("access_token")
    if not access_token:
        return None
    raw_scopes = token_data.get("scope", "")
    scopes = raw_scopes.split() if isinstance(raw_scopes, str) else list(raw_scopes)
    from datetime import timezone as _tz
    expiry = (
        datetime.now(_tz.utc) + timedelta(seconds=int(token_data.get("expires_in", 3600)))
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return json.dumps({
        "token": access_token,
        "refresh_token": token_data.get("refresh_token"),
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": client_config.get("client_id", ""),
        "client_secret": client_config.get("client_secret", ""),
        "scopes": scopes,
        "expiry": expiry,
    })


def _complete_google_link(code: str, state: Optional[str], saved_state: dict, frontend_url: str):
    """Handle the OAuth callback for linking a Google account to an existing Clerk user.

    If the Google account is already linked to a *different* Clerk user, the link
    is refused to prevent account takeover. Otherwise the current user's google_sub
    and google_token_json are updated so they can sign in with Google and sync.
    """
    import requests as _requests
    client_config = get_google_client_config()
    link_user_id = saved_state.get("link_user_id") or saved_state.get("user_id")

    try:
        post_data = {
            "code": code,
            "client_id": client_config["client_id"],
            "client_secret": client_config["client_secret"],
            "redirect_uri": get_google_redirect_uri(),
            "grant_type": "authorization_code",
        }
        code_verifier = saved_state.get("code_verifier")
        if code_verifier:
            post_data["code_verifier"] = code_verifier
        token_resp = _requests.post("https://oauth2.googleapis.com/token", data=post_data, timeout=10)
        token_data = token_resp.json()
        if "error" in token_data:
            error_msg = f"{token_data['error']}: {token_data.get('error_description', '')}"
            return RedirectResponse(url=f"{frontend_url}?google_error={quote(error_msg, safe='')}")
        access_token = token_data.get("access_token")
        if not access_token:
            return RedirectResponse(url=f"{frontend_url}?google_error=no_access_token")
        clear_google_oauth_state(state)
    except Exception as exc:
        return RedirectResponse(url=f"{frontend_url}?google_error={quote(str(exc), safe='')}")

    try:
        userinfo = _requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        ).json()
    except Exception:
        return RedirectResponse(url=f"{frontend_url}?google_error=could_not_fetch_profile")

    google_sub = userinfo.get("sub")
    if not google_sub:
        return RedirectResponse(url=f"{frontend_url}?google_error=no_google_id")

    db = SessionLocal()
    try:
        # Block link if this Google account is already owned by a different user.
        existing = db.query(User).filter(User.google_sub == google_sub).first()
        if existing and existing.user_id != link_user_id:
            return RedirectResponse(url=f"{frontend_url}?google_error=google_account_already_linked")

        user = db.query(User).filter(User.user_id == link_user_id).first()
        if not user:
            return RedirectResponse(url=f"{frontend_url}?google_error=user_not_found")

        user.google_sub = google_sub
        if not user.email:
            user.email = userinfo.get("email") or ""
        token_json = _build_token_json(token_data, client_config)
        if token_json:
            user.google_token_json = token_json
        db.commit()
        return RedirectResponse(url=f"{frontend_url}?google_link=1&user_id={link_user_id}")
    except Exception as exc:
        db.rollback()
        return RedirectResponse(url=f"{frontend_url}?google_error={quote(str(exc), safe='')}")
    finally:
        db.close()


def get_google_creds(user_id: int):
    """Load and, if needed, auto-refresh a user's Google OAuth credentials.
    Returns None (and clears the stored token) if the token is invalid or can't be refreshed."""
    # Read token JSON from the database
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        token_json = user.google_token_json if user else None
    finally:
        db.close()

    creds = None
    if token_json:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
        except Exception:
            clear_google_token(user_id)
            return None

    if not creds or not creds.valid:
        # Google access tokens expire after ~1 hour; use the refresh token to get a new one silently.
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleAuthRequest())
                _save_google_token(user_id, creds.to_json())
                return creds
            except RefreshError:
                clear_google_token(user_id)
                return None
            except Exception as exc:
                if is_invalid_google_grant(exc):
                    clear_google_token(user_id)
                    return None
                raise
        else:
            # No valid creds and no refresh token — caller must trigger OAuth
            clear_google_token(user_id)
            return None
    return creds

@app.get("/auth/google")
async def get_google_auth_url(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Returns the Google OAuth URL for the frontend to redirect to."""
    require_same_user(current_user, user_id)
    ensure_user_exists(user_id, db)
    return {"auth_url": create_google_auth_url(user_id)}

_GOOGLE_IDENTITY_SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
]
# All scopes requested during sign-in so users only go through OAuth once
# and are immediately connected for Gmail/Calendar sync after registration.
_GOOGLE_LOGIN_SCOPES = _GOOGLE_IDENTITY_SCOPES + SCOPES


@app.get("/auth/google/login")
async def get_google_login_url():
    """Starts a Google OAuth flow for login/registration.

    Requests all sync scopes (Gmail, Calendar, Classroom) alongside identity
    so that new and returning users are fully connected in a single OAuth pass.
    """
    try:
        login_state = f"login_{secrets.token_urlsafe(24)}"
        flow = Flow.from_client_config(
            get_google_credentials_config(),
            scopes=_GOOGLE_LOGIN_SCOPES,
            redirect_uri=get_google_redirect_uri()
        )
        auth_url, _ = flow.authorization_url(
            access_type='offline', prompt='select_account', state=login_state
        )
        save_google_oauth_state(login_state, flow.code_verifier, 0)
        key = _state_key(login_state)
        if key in _oauth_states:
            _oauth_states[key]["login_flow"] = True
        return {"auth_url": auth_url}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start Google sign-in: {exc}")


@app.get("/auth/google/link")
async def get_google_link_url(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Generate an OAuth URL to link a Google account to an existing Clerk account.

    Used by password-based users who want Google sign-in and/or sync.
    The callback will update google_sub and save the full token for this user.
    """
    require_same_user(current_user, user_id)
    ensure_user_exists(user_id, db)
    try:
        link_state = f"link_{secrets.token_urlsafe(24)}"
        flow = Flow.from_client_config(
            get_google_credentials_config(),
            scopes=_GOOGLE_LOGIN_SCOPES,
            redirect_uri=get_google_redirect_uri()
        )
        auth_url, _ = flow.authorization_url(
            access_type='offline', prompt='consent', state=link_state
        )
        save_google_oauth_state(link_state, flow.code_verifier, user_id)
        key = _state_key(link_state)
        if key in _oauth_states:
            _oauth_states[key]["link_flow"] = True
            _oauth_states[key]["link_user_id"] = user_id
        return {"auth_url": auth_url}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start Google link: {exc}")

@app.post("/auth/google/set-password")
async def google_set_password(
    data: GoogleSetPasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sets a password for a Google-authenticated user who hasn't set one yet.
    Requires the session token issued during the Google sign-in redirect, so a
    stranger can't claim a passwordless account just by guessing its user_id."""
    require_same_user(current_user, data.user_id)
    user = db.query(User).filter(User.user_id == data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.google_sub:
        raise HTTPException(status_code=400, detail="This endpoint is only for Google-authenticated accounts.")
    validate_strong_password(data.password, user.username)
    user.password_hash = _hash_password(data.password)
    db.commit()
    return {"status": "success", "user_id": user.user_id, "username": user.username, "settings": user_settings_payload(user)}

@app.get("/auth/google/callback")
async def google_callback(code: Optional[str] = None, state: str = None, error: Optional[str] = None):
    """Handles the redirect from Google after the user authenticates."""
    return complete_google_oauth(code=code, state=state, error=error)

# --- GMAIL SYNC ---
def get_email_body(payload):
    """Helper to extract plain text body from Gmail payload."""
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
    elif 'body' in payload and 'data' in payload['body']:
        return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
    return ""

def get_email_sender(payload):
    """Extract the sender's display name from the Gmail message headers.
    Strips the email address portion (e.g. 'John Doe <john@example.com>' → 'John Doe')."""
    headers = payload.get("headers", [])
    sender = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "from"), "")
    if not sender:
        return None
    name_match = re.match(r'\s*"?([^"<]+?)"?\s*(?:<[^>]+>)?\s*$', sender)
    if name_match:
        return name_match.group(1).strip()
    return sender.strip()

# --- TASK ASSIGNER NORMALIZATION ---
def clean_assigner_label(value: Optional[str], source_info: str) -> str:
    """Normalise a raw assigner string into a clean display name, or 'me' if nothing useful is found.
    Strips prefixes like 'Assigned by:' and removes class-name noise from email/doc sources."""
    if not value:
        return "me"

    assigner = re.sub(r'\s+', ' ', str(value)).strip()
    if not assigner:
        return "me"

    source = (source_info or "").lower()
    if source.startswith("classroom"):
        return assigner

    # Email/doc inputs should not keep labels like "US History II: Mr. Teacher".
    parts = [part.strip() for part in re.split(r',|;|\band\b', assigner) if part.strip()]
    cleaned_parts = []
    for part in parts or [assigner]:
        if ":" in part:
            part = part.split(":")[-1].strip()
        part = re.sub(r'^(from|sender|assigned by|assigner|teacher)\s*[:\-]\s*', '', part, flags=re.IGNORECASE).strip()
        if part and part.lower() not in {"none", "null", "me"}:
            cleaned_parts.append(part)

    return ", ".join(cleaned_parts) if cleaned_parts else "me"

def infer_assigner_from_text(text_content: str, source_info: str) -> Optional[str]:
    """Use regex patterns (tailored per source type) to pull an assigner name from raw text.
    Returns None if no match is found so callers can fall back to other strategies."""
    source = (source_info or "").lower()
    patterns = []
    if source.startswith("classroom"):
        patterns = [
            r'Assigned By:\s*([^\n.]+)',
            r'for\s+([^.\n]+?)\.\s*Assigned By:\s*([^\n.]+)',
            r'Classroom Assignment:\s*.*?\s+for\s+([^.\n]+)'
        ]
    elif source.startswith("gmail"):
        patterns = [r'Email From:\s*([^\n]+)']
    else:
        patterns = [
            r'(?:Assigned By|Assigner|Teacher|From):\s*([^\n.]+)',
            r'\b[A-Z][\w &.-]{2,}:\s*([A-Z][^\n.]+)'
        ]

    for pattern in patterns:
        match = re.search(pattern, text_content, re.IGNORECASE)
        if not match:
            continue
        return match.group(match.lastindex or 1).strip()
    return None

def normalize_task_assigner(task_data, text_content: str, source_info: str) -> str:
    """Pick the best available assigner field from the extracted task data, then clean it.
    Tries multiple field names in order of reliability before falling back to text inference."""
    raw_assigner = (
        task_data.get("assigner")
        or task_data.get("assigned_by")
        or task_data.get("teacher")
        or task_data.get("sender")
        or infer_assigner_from_text(text_content, source_info)
        or task_data.get("assignee")
    )
    return clean_assigner_label(raw_assigner, source_info)

# --- TASK SERIALIZATION ---
def task_to_dict(task: Task) -> dict:
    """Convert a SQLAlchemy Task ORM object into a plain dict the frontend can consume as JSON."""
    return {
        "owner_id": task.owner_id,
        "task_id": task.task_id,
        "raw_id": task.raw_id,
        "title": task.title,
        "description": task.description or "",
        "due_date": task.due_date,
        "end_date": task.end_date,
        "due_text": task.due_text,
        "assignee": task.assignee,
        "item_type": task.item_type,
        "priority": task.priority,
        "is_all_day": bool(task.is_all_day),
        "confidence": task.confidence,
        "status": task.status,
        "user_feedback": task.user_feedback,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }

# --- DUPLICATE DETECTION ---
def normalize_title_for_match(title: Optional[str]) -> str:
    """Lowercase and strip punctuation/noise words from a title so different phrasings of
    the same task (e.g. from Gmail vs Classroom) compare as equal."""
    text = re.sub(r'[^a-z0-9\s]', ' ', str(title or "").lower())
    text = re.sub(
        r'\b(google classroom|classroom|calendar|event|assignment|new|posted|assigned|due|please|reminder|notification)\b',
        ' ',
        text
    )
    return re.sub(r'\s+', ' ', text).strip()

def title_tokens_for_match(title: Optional[str]) -> set:
    """Split a normalised title into meaningful tokens, dropping very common verbs and prepositions
    so that only distinctive words drive the similarity check."""
    normalized = normalize_title_for_match(title)
    stop_words = {
        "the", "and", "for", "with", "from", "into", "onto", "task",
        "submit", "finish", "complete", "turn", "read", "write", "review",
        "prepare", "study", "work", "make", "create"
    }
    return {
        token for token in normalized.split()
        if len(token) >= 3 and token not in stop_words
    }

def titles_are_similar(left_title: Optional[str], right_title: Optional[str]) -> bool:
    """Return True if two task titles are similar enough to be considered the same task.
    Uses a 75% token overlap threshold; single-token titles need a substring match to avoid false positives."""
    left_normalized = normalize_title_for_match(left_title)
    right_normalized = normalize_title_for_match(right_title)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True

    left_tokens = title_tokens_for_match(left_normalized)
    right_tokens = title_tokens_for_match(right_normalized)
    if not left_tokens or not right_tokens:
        return False

    intersection = left_tokens & right_tokens
    smaller_count = min(len(left_tokens), len(right_tokens))
    # Single-word titles are too generic for overlap alone — also require one to contain the other.
    if smaller_count == 1:
        shared = next(iter(intersection), "")
        return bool(shared and len(shared) >= 5 and (left_normalized in right_normalized or right_normalized in left_normalized))

    # 75% of the smaller title's tokens must appear in the other title.
    return (len(intersection) / smaller_count) >= 0.75

def due_day_key(due_date: Optional[str]) -> str:
    """Extract just the YYYY-MM-DD portion from any datetime string for day-level comparisons."""
    if not due_date:
        return ""
    match = re.search(r'\d{4}-\d{2}-\d{2}', str(due_date))
    return match.group(0) if match else str(due_date)

def task_match_key(task_data) -> Optional[tuple]:
    """Create a (normalized_title, date) tuple used as a dict key for duplicate detection."""
    title_key = normalize_title_for_match(task_data.get("title"))
    due_key = due_day_key(task_data.get("due_date"))
    if not title_key:
        return None
    return title_key, due_key

def build_duplicate_index(db: Session, user_id: int) -> dict:
    """Load all non-deleted tasks for a user into a lookup dict keyed by (title, date).
    Building this index once per batch avoids N+1 database queries during import."""
    existing_tasks = db.query(Task).filter(
        Task.owner_id == user_id,
        Task.status != "deleted"
    ).all()

    duplicate_index = {"__items__": []}
    for task in existing_tasks:
        key = task_match_key({"title": task.title, "due_date": task.due_date})
        if key:
            duplicate_index[key] = task
            duplicate_index["__items__"].append(task)

    return duplicate_index

def adjacent_day_keys(due_key: str) -> List[str]:
    """Return the day keys for the day before and after a YYYY-MM-DD string.
    Used to catch the same event imported with a ±1 day shift (e.g. a UTC-stored
    calendar event vs. a local-time date read from an uploaded document)."""
    try:
        day = datetime.fromisoformat(due_key)
    except ValueError:
        return []
    return [
        (day - timedelta(days=1)).strftime("%Y-%m-%d"),
        (day + timedelta(days=1)).strftime("%Y-%m-%d"),
    ]

def find_duplicate_task(duplicate_index: dict, task_data) -> Optional[Task]:
    """Search the pre-built index for an existing task that matches the incoming one.

    Match order (strictest first):
      1. Exact normalized title + same day.
      2. Fuzzy title + same day.
      3. Fuzzy title + adjacent day (±1) — catches timezone shifts between sources,
         e.g. 'Team meeting' from Google Calendar vs. the same meeting in a PDF.
      4. Fuzzy title where one side has no date — an undated Gmail mention is
         superseded by the dated Calendar/Classroom version, and vice versa.
    """
    key = task_match_key(task_data)
    if not key:
        return None

    exact_match = duplicate_index.get(key)
    if exact_match:
        return exact_match

    title_key, due_key = key
    if not due_key:
        # New task has no date: fall back to fuzzy title match against any existing task,
        # preferring one that has a date (it's the more authoritative copy).
        dated_match = None
        undated_match = None
        for existing in duplicate_index.get("__items__", []):
            if titles_are_similar(existing.title, task_data.get("title")):
                if existing.due_date and not dated_match:
                    dated_match = existing
                elif not existing.due_date and not undated_match:
                    undated_match = existing
        return dated_match or undated_match

    for existing in duplicate_index.get("__items__", []):
        if due_day_key(existing.due_date) == due_key and titles_are_similar(existing.title, task_data.get("title")):
            return existing

    # Adjacent-day pass: same fuzzy title within ±1 day is almost certainly the
    # same item arriving from two sources with different timezone handling.
    neighbor_keys = set(adjacent_day_keys(due_key))
    if neighbor_keys:
        for existing in duplicate_index.get("__items__", []):
            if due_day_key(existing.due_date) in neighbor_keys and titles_are_similar(existing.title, task_data.get("title")):
                return existing

    # Cross-date pass: new task has a real date, existing task has none.
    # Catches Gmail/announcement tasks that were saved without a due date
    # and are later superseded by the authoritative Classroom entry.
    for existing in duplicate_index.get("__items__", []):
        if not existing.due_date and titles_are_similar(existing.title, task_data.get("title")):
            return existing

    return None

# --- GOOGLE DATE/TIME HELPERS ---
def has_google_due_time(due_time: Optional[dict]) -> bool:
    """Return True if a Google dueTime object contains at least one non-null time component."""
    if not due_time:
        return False
    return any(due_time.get(key) is not None for key in ("hours", "minutes", "seconds", "nanos"))

def google_due_to_iso(
    due: dict,
    due_time: Optional[dict] = None,
    utc_offset_minutes: int = 0,
    tz_name: Optional[str] = None,
) -> Optional[str]:
    """Convert Google Classroom's separate dueDate/dueTime objects into a single ISO 8601 string.

    Classroom stores due times in UTC. Preferred conversion uses the user's IANA
    timezone name so the offset is correct *for that date* — converting a January
    deadline with June's DST offset turned "11:59 PM" into "12:59 AM". Falls back
    to the browser-supplied fixed offset when no timezone name is available.
    """
    if not due:
        return None

    year = due.get("year")
    month = due.get("month")
    day = due.get("day")
    if not year or not month or not day:
        return None

    if not has_google_due_time(due_time):
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    hour = due_time.get("hours", 12)
    minute = due_time.get("minutes", 0)
    second = due_time.get("seconds", 0)

    utc_dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))

    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            from datetime import timezone as _tz
            local_dt = utc_dt.replace(tzinfo=_tz.utc).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
            return local_dt.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            pass  # Unknown tz name or missing tz database — use the fixed offset below.

    # JS getTimezoneOffset convention: positive = west of UTC.
    local_dt = utc_dt - timedelta(minutes=utc_offset_minutes)
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S")

def format_due_for_frontend(due_date: Optional[str], is_all_day: bool = True) -> dict:
    """Format a raw due_date string into a user-friendly display dict for the frontend card view."""
    if not due_date:
        return {"due": "No due date", "time": "All Day" if is_all_day else "No time"}

    try:
        clean_date = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', due_date)
        due_dt = datetime.fromisoformat(clean_date)
    except Exception:
        return {"due": "No due date", "time": "All Day" if is_all_day else "No time"}

    return {
        "due": due_dt.strftime("%m/%d/%Y"),
        "time": "All Day" if is_all_day else due_dt.strftime("%I:%M %p")
    }

def make_structured_task(
    title: str,
    description: str = "",
    due_date: Optional[str] = None,
    end_date: Optional[str] = None,
    assigner: Optional[str] = None,
    item_type: str = "task",
    priority: str = "normal",
    is_all_day: bool = True,
    confidence: int = 96
) -> dict:
    """Build a consistent task dict from structured fields — used by all sync sources (Calendar, Classroom, etc.)
    to produce a uniform shape before saving, regardless of where the data came from."""
    task = {
        "item_type": item_type,
        "title": title,
        "description": description,
        "due_date": due_date,
        "end_date": end_date,
        "assignee": "me",
        "assigner": assigner,
        "is_all_day": is_all_day,
        "priority": priority,
        "confidence": confidence
    }
    task.update(format_due_for_frontend(due_date, is_all_day))
    return task

# --- SOURCE DEDUPLICATION ---
def source_marker(user_id: int, source_info: str) -> str:
    """Create a unique per-user key for a given source (e.g. 'gmail: abc123'), stored in RawInput.source_id."""
    return f"{user_id}:{source_info}"

def source_already_scanned(db: Session, user_id: int, source_info: str) -> bool:
    """Check if this exact source has been imported before so we don't create duplicate tasks on re-sync."""
    marker = source_marker(user_id, source_info)
    if db.query(RawInput).filter(RawInput.source_id == marker).first():
        return True

    # Backward compatibility for rows created before source_id was populated.
    return db.query(Task).join(RawInput, Task.raw_id == RawInput.raw_id).filter(
        Task.owner_id == user_id,
        RawInput.source_type == source_info
    ).first() is not None

def mark_source_scanned(db: Session, user_id: int, source_info: str, content: str = ""):
    """Record that a source has been seen so future syncs skip it even if no tasks were extracted."""
    if source_already_scanned(db, user_id, source_info):
        return

    db.add(RawInput(
        content=(content or f"Scanned {source_info}")[:500],
        source_type=source_info,
        source_id=source_marker(user_id, source_info),
        received_at=datetime.now()
    ))
    db.commit()

# --- WORK SCHEDULE DETECTION ---
def looks_like_work_schedule(text: str) -> bool:
    """Heuristically detect whether uploaded text is an employee work schedule.
    Requires at least a week-header keyword, multiple day names, and shift time ranges."""
    lowered = text.lower()
    has_week_header = any(kw in lowered for kw in ("wkly hrs", "weekly", "schedule", "week of", "week ending"))
    has_day_headers = len(re.findall(r'\b(sun|mon|tue|wed|thu|fri|sat)\b', lowered)) >= 2
    # Match both "9:00 am - 5:00 pm" and "9am - 5pm" and "09:00 - 17:00" (24-hour)
    has_shift_times = bool(re.search(
        r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*[-–]\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b'
        r'|\b\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}\b',
        lowered
    ))
    return has_week_header and has_day_headers and has_shift_times

async def save_work_schedule_entries(structured_tasks, user: User, source_info: str, db: Session):
    """Replace any previously saved shifts from this file with the newly parsed ones.
    Clears old entries first so re-uploading a corrected schedule doesn't leave stale shifts."""
    clear_existing_work_schedule_entries(user.user_id, source_info, db)
    entries = []
    for index, task_data in enumerate(structured_tasks):
        shift_key = f"{task_data.get('due_date', 'no-date')}:{task_data.get('end_date', 'no-end')}:{index}"
        shift_source = f"{source_info}:work-shift:{shift_key}"
        shift_content = f"Uploaded work schedule for {user.preferred_name or user.username}: {task_data.get('title', 'Work shift')}"
        entries.append((task_data, shift_content, shift_source))
    return await save_structured_task_entries(entries, user.user_id, db)

def clear_existing_work_schedule_entries(user_id: int, source_info: str, db: Session):
    """Delete all RawInput rows (and their linked Tasks) for shifts from a specific source file.
    The LIKE query matches the source_id pattern used when saving work shifts."""
    raw_rows = db.query(RawInput).filter(
        RawInput.source_id.like(source_marker(user_id, f"{source_info}:work-shift:%"))
    ).all()
    raw_ids = [row.raw_id for row in raw_rows]
    if not raw_ids:
        return

    db.query(Task).filter(
        Task.owner_id == user_id,
        Task.raw_id.in_(raw_ids)
    ).delete(synchronize_session=False)
    db.query(RawInput).filter(RawInput.raw_id.in_(raw_ids)).delete(synchronize_session=False)
    db.commit()

# --- GOOGLE CALENDAR SYNC ---
def google_calendar_event_to_entry(event: dict, cal_name: str = ""):
    """Convert a raw Google Calendar event dict into the standard (task, content, source_info) tuple.
    An all-day event is identified by a 10-character date string (YYYY-MM-DD) without a time component."""
    start = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
    end = event.get('end', {}).get('dateTime') or event.get('end', {}).get('date')
    if not start:
        return None

    # Google all-day events use date strings (len=10); timed events use datetime strings (len>10).
    is_all_day = len(str(start)) == 10
    due_date = f"{start}T12:00:00Z" if is_all_day else start
    end_date = end
    if is_all_day and end and len(str(end)) == 10:
        try:
            # Google's all-day event end date is exclusive (e.g. a 1-day event on Mon has end=Tue).
            # Subtract one day to get the actual last day the event spans.
            exclusive_end = datetime.fromisoformat(end)
            inclusive_end = exclusive_end - timedelta(days=1)
            end_date = f"{inclusive_end.strftime('%Y-%m-%d')}T13:00:00Z"
        except ValueError:
            end_date = due_date

    title = event.get("summary", "Google Calendar Event")
    description = event.get("description", "")

    # Birthday events arrive with just the person's name as the summary.
    # Tag them so they're recognisable in the task list.
    is_birthday = (
        event.get("eventType") == "birthday"
        or "birthday" in cal_name.lower()
    )
    if is_birthday and "birthday" not in title.lower():
        title = f"{title}'s Birthday"
    if is_birthday and not description:
        description = "Birthday from Google Calendar"

    content = f"Calendar Event: {title} starting {start}. Description: {description}"
    task = make_structured_task(
        title=title,
        description=description,
        due_date=due_date,
        end_date=end_date,
        assigner="Google Calendar",
        item_type="event",
        is_all_day=is_all_day
    )
    return task, content, f"calendar: {event['id']}"

def is_noise_calendar(cal_id: str, cal_name: str) -> bool:
    """True for Google's subscribed informational calendars (Holidays in United States,
    week numbers, etc.) whose events are observances, not personal commitments -
    importing them floods the task list with entries like 'Tax Day 2030'."""
    cid = (cal_id or "").lower()
    name = (cal_name or "").lower()
    return (
        "#holiday@group.v.calendar.google.com" in cid
        or "%23holiday@group.v.calendar.google.com" in cid
        or "#weeknum@group.v.calendar.google.com" in cid
        or "%23weeknum@group.v.calendar.google.com" in cid
        or "holiday" in name
        or "week number" in name
    )

# Holiday-calendar events carry a standard description ("Observance - To hide
# observances, go to Google Calendar Settings > Holidays in United States").
_OBSERVANCE_DESC_RE = re.compile(r"to hide observances|public holiday", re.IGNORECASE)
DEFAULT_GOOGLE_CALENDAR_SYNC_FUTURE_DAYS = 180

def google_calendar_sync_future_days() -> int:
    """Read the Calendar future-sync horizon, falling back safely on blank/bad env values."""
    raw_value = os.environ.get("GOOGLE_CALENDAR_SYNC_FUTURE_DAYS", "").strip()
    if not raw_value:
        return DEFAULT_GOOGLE_CALENDAR_SYNC_FUTURE_DAYS
    try:
        return max(0, int(raw_value))
    except ValueError:
        _log.warning(
            "Invalid GOOGLE_CALENDAR_SYNC_FUTURE_DAYS=%r; using default %s",
            raw_value,
            DEFAULT_GOOGLE_CALENDAR_SYNC_FUTURE_DAYS,
        )
        return DEFAULT_GOOGLE_CALENDAR_SYNC_FUTURE_DAYS

def is_holiday_calendar_event(event: dict, cal_id: str = "", cal_name: str = "") -> bool:
    """Return True for Google holiday/observance events that should not become tasks."""
    if is_noise_calendar(cal_id, cal_name):
        return True

    fields = [
        event.get("description", ""),
        event.get("location", ""),
    ]
    for actor_key in ("creator", "organizer"):
        actor = event.get(actor_key) or {}
        if isinstance(actor, dict):
            fields.extend([actor.get("displayName", ""), actor.get("email", "")])

    return bool(_OBSERVANCE_DESC_RE.search(" ".join(str(value or "") for value in fields)))

def is_holiday_calendar_task(task: Task, raw: Optional[RawInput] = None) -> bool:
    """Return True for previously imported Google holiday events that should be hidden."""
    if task.item_type != "event":
        return False

    source_type = str(getattr(raw, "source_type", "") or "")
    source_id = str(getattr(raw, "source_id", "") or "")
    came_from_google_calendar = (
        task.assignee == "Google Calendar"
        or source_type.startswith("calendar:")
        or ":calendar:" in source_id
    )
    if not came_from_google_calendar:
        return False

    searchable_text = " ".join(
        str(value or "")
        for value in (
            task.description,
            getattr(raw, "content", "") if raw else "",
        )
    )
    return bool(_OBSERVANCE_DESC_RE.search(searchable_text))

def collect_google_calendar_entries(calendar, db: Session, user_id: int, max_results: int = 2500):
    """Fetch events from every Google Calendar the user has (primary, birthdays, shared, etc.)
    and return them as a list of task entries ready to save. Skips already-seen events,
    holiday/observance calendars, and anything beyond the future sync horizon."""
    summary = {"calendar": 0, "calendar_already_scanned": 0, "calendar_skipped": 0}
    sync_entries = []
    past_days = int(os.environ.get("GOOGLE_CALENDAR_SYNC_PAST_DAYS", str(GOOGLE_SYNC_PAST_DAYS)))
    time_min = (datetime.utcnow() - timedelta(days=past_days)).isoformat() + 'Z'
    # Bound the future too; singleEvents=True expands recurring events, so an
    # unbounded query can return weekly-class instances stretching years ahead.
    future_days = google_calendar_sync_future_days()
    time_max = (datetime.utcnow() + timedelta(days=future_days)).isoformat() + 'Z'

    # Discover every calendar the user has (primary, birthdays, shared, etc.)
    # Requires calendar.readonly scope; falls back to primary-only if scope is missing.
    # Build list of (calendar_id, calendar_name) tuples so we can tag birthday events.
    calendar_list: list[tuple[str, str]] = []
    try:
        page_token = None
        while True:
            cal_list_result = calendar.calendarList().list(pageToken=page_token).execute()
            for cal in cal_list_result.get('items', []):
                if is_noise_calendar(cal.get('id', ''), cal.get('summary', '')):
                    summary["calendars_skipped"] = summary.get("calendars_skipped", 0) + 1
                    continue
                calendar_list.append((cal['id'], cal.get('summary', '')))
            page_token = cal_list_result.get('nextPageToken')
            if not page_token:
                break
    except Exception:
        calendar_list = [('primary', '')]

    if not calendar_list:
        calendar_list = [('primary', '')]

    seen_event_ids = set()  # guard against the same event appearing in multiple calendar views

    for cal_id, cal_name in calendar_list:
        page_token = None
        while True:
            try:
                cal_result = calendar.events().list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=min(max_results, 2500),
                    singleEvents=True,
                    orderBy='startTime',
                    pageToken=page_token
                ).execute()
            except Exception as exc:
                print(f"Calendar sync skipped for {cal_id}: {exc}")
                break

            for event in cal_result.get('items', []):
                event_id = event.get('id', '')
                if not event_id or event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)

                if event.get("status") == "cancelled" or not event.get("summary"):
                    summary["calendar_skipped"] += 1
                    continue

                if event.get("eventType") in _SKIP_CALENDAR_EVENT_TYPES:
                    summary["calendar_skipped"] += 1
                    continue

                source_info = f"calendar: {event_id}"
                if source_already_scanned(db, user_id, source_info):
                    summary["calendar_already_scanned"] += 1
                    continue

                # Holiday observances carry a standard boilerplate description even
                # when they reach us through a renamed or shared calendar.
                if is_holiday_calendar_event(event, cal_id, cal_name):
                    summary["calendar_skipped"] += 1
                    mark_source_scanned(db, user_id, source_info, f"Skipped holiday observance: {event.get('summary', '')}")
                    continue

                entry = google_calendar_event_to_entry(event, cal_name)
                if not entry:
                    summary["calendar_skipped"] += 1
                    mark_source_scanned(db, user_id, source_info, f"Skipped Calendar event: {event.get('summary', '')}")
                    continue

                sync_entries.append(entry)
                summary["calendar"] += 1

            page_token = cal_result.get("nextPageToken")
            if not page_token:
                break

    return sync_entries, summary


def collect_google_tasks_entries(tasks_service, db: Session, user_id: int):
    """Fetch all incomplete tasks from every Google Tasks list."""
    summary = {"gtasks": 0, "gtasks_already_scanned": 0, "gtasks_skipped": 0}
    sync_entries = []

    try:
        tasklists_result = tasks_service.tasklists().list(maxResults=20).execute()
    except Exception as exc:
        print(f"Google Tasks list error (scope may be missing): {exc}")
        return sync_entries, summary

    for tasklist in tasklists_result.get('items', []):
        try:
            page_token = None
            while True:
                tasks_result = tasks_service.tasks().list(
                    tasklist=tasklist['id'],
                    showCompleted=False,
                    showHidden=False,
                    maxResults=100,
                    pageToken=page_token
                ).execute()

                for task in tasks_result.get('items', []):
                    if task.get('status') == 'completed':
                        continue

                    title = str(task.get('title') or '').strip()
                    if not title:
                        summary["gtasks_skipped"] += 1
                        continue

                    source_info = f"gtask: {task['id']}"
                    if source_already_scanned(db, user_id, source_info):
                        summary["gtasks_already_scanned"] += 1
                        continue

                    due_raw = task.get('due')  # RFC 3339 — always T00:00:00.000Z when set
                    due_date = None
                    if due_raw:
                        date_only = re.sub(r'T.*', '', due_raw)  # YYYY-MM-DD
                        due_date = f"{date_only}T12:00:00"
                        # Skip long-overdue Google Tasks — outside the sync window
                        # they're stale clutter. Undated tasks are always kept.
                        try:
                            if datetime.fromisoformat(date_only) < datetime.utcnow() - timedelta(days=GOOGLE_SYNC_PAST_DAYS):
                                summary["gtasks_skipped"] += 1
                                mark_source_scanned(db, user_id, source_info, f"Skipped old Google Task: {title}")
                                continue
                        except ValueError:
                            pass

                    entry_task = make_structured_task(
                        title=title,
                        description=task.get('notes') or '',
                        due_date=due_date,
                        assigner="Google Tasks",
                        item_type="task",
                        is_all_day=True,
                        confidence=97
                    )
                    content = f"Google Task: {title}. Due: {due_raw or 'No date'}. Notes: {task.get('notes', '')}"
                    sync_entries.append((entry_task, content, source_info))
                    summary["gtasks"] += 1

                page_token = tasks_result.get('nextPageToken')
                if not page_token:
                    break

        except Exception as exc:
            print(f"Google Tasks sync error for list '{tasklist.get('title')}': {exc}")

    return sync_entries, summary

# --- GOOGLE CLASSROOM FILTERING ---
# Pre-compiled regex patterns are faster than compiling on every function call.
CLASSROOM_TASK_WORD_RE = re.compile(
    r'\b(submit|turn\s+in|complete|finish|answer|write|draft|create|make|'
    r'prepare|read|study|review|upload|attach|respond|do|assignment|homework|'
    r'project|essay|quiz|test|exam|worksheet|reflection|presentation|deadline|due)\b',
    re.IGNORECASE
)

CLASSROOM_REFERENCE_ONLY_RE = re.compile(
    r'\b(example|sample|template|reference|resource|announcement|notes?|slides?|'
    r'materials?|rubric|practice|optional|copy of)\b',
    re.IGNORECASE
)

CLASSROOM_DAY_ONLY_TITLE_RE = re.compile(
    r'^\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*'
    r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|'
    r'dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\s*$',
    re.IGNORECASE
)

CLASSROOM_MINIMAL_DESCRIPTION_RE = re.compile(
    r'^\s*(?:\.|n/?a|none|no description)?\s*$',
    re.IGNORECASE
)

ACTIONABLE_CLASSROOM_WORK_TYPES = {
    "ASSIGNMENT",
    "SHORT_ANSWER_QUESTION",
    "MULTIPLE_CHOICE_QUESTION"
}

def is_actionable_classroom_item(item: dict) -> bool:
    """Decide whether a Classroom post represents a real student task worth adding to Clerk.
    Filters out draft posts, resource-only material, and date-header agenda posts."""
    if item.get("state") and item.get("state") != "PUBLISHED":
        return False

    work_type = item.get("workType")
    if work_type and work_type not in ACTIONABLE_CLASSROOM_WORK_TYPES:
        return False

    title = str(item.get("title") or "")
    description = str(item.get("description") or "")

    # Skip daily agenda posts unconditionally — titles like "Thursday May 28th"
    # or "May 26th" are class-period plans, not student assignments.
    if CLASSROOM_DAY_ONLY_TITLE_RE.match(title):
        return False

    text = f"{title}\n{description}"
    has_due_date = bool(item.get("dueDate"))
    has_action_language = bool(CLASSROOM_TASK_WORD_RE.search(text))
    looks_reference_only = bool(CLASSROOM_REFERENCE_ONLY_RE.search(title)) and not has_due_date

    if looks_reference_only:
        return False

    if not has_due_date and not has_action_language:
        return False

    return True

def classroom_item_is_too_old(item: dict, utc_offset_minutes: int = 0) -> bool:
    """True when a Classroom post falls outside the sync window and shouldn't be imported.

    Items with a due date are judged by that date — an old post whose deadline is
    still in the future (or recent) stays relevant. Items without a due date are
    judged by their creation time instead.
    """
    cutoff = datetime.utcnow() - timedelta(days=GOOGLE_SYNC_PAST_DAYS)
    due_iso = google_due_to_iso(item.get("dueDate", {}), item.get("dueTime"), utc_offset_minutes)
    if due_iso:
        due_dt = _parse_wall_datetime(due_iso)
        return bool(due_dt and due_dt < cutoff)
    creation = item.get("creationTime") or item.get("updateTime")
    if creation:
        try:
            created_dt = datetime.fromisoformat(str(creation).replace("Z", "+00:00")).replace(tzinfo=None)
            return created_dt < cutoff
        except ValueError:
            return False
    return False

def cleanup_classroom_noise_tasks(db: Session, user_id: int) -> int:
    """Soft-delete any previously imported Classroom tasks whose title is just a date/day header.
    These posts are class-period agendas, not student assignments, and should be cleaned up."""
    tasks = db.query(Task).filter(
        Task.owner_id == user_id,
        Task.status != "deleted"
    ).all()

    # Collect raw_ids in one batch query
    raw_ids_needed = [t.raw_id for t in tasks if t.raw_id and CLASSROOM_DAY_ONLY_TITLE_RE.match(t.title or "")]
    raw_map = {}
    if raw_ids_needed:
        raw_map = {r.raw_id: r for r in db.query(RawInput).filter(RawInput.raw_id.in_(raw_ids_needed)).all()}

    cleaned = 0
    for task in tasks:
        if not CLASSROOM_DAY_ONLY_TITLE_RE.match(task.title or ""):
            continue

        raw = raw_map.get(task.raw_id) if task.raw_id else None
        came_from_classroom = raw and str(raw.source_type or "").startswith("classroom")
        assigned_by_class = task.assignee and task.assignee != "me"
        if came_from_classroom or assigned_by_class:
            task.status = "deleted"
            cleaned += 1

    if cleaned:
        db.commit()
    return cleaned

def classroom_item_to_entry(classroom, course: dict, item: dict, utc_offset_minutes: int = 0, tz_name: Optional[str] = None):
    """Convert a single Google Classroom coursework item into the standard (task, content, source_info) tuple.
    Looks up the teacher's profile by their Google user ID so we can show a name instead of an ID."""
    due = item.get('dueDate', {})
    due_str = f"{due.get('month')}/{due.get('day')}/{due.get('year')}" if due else "No date"
    teacher_name = None
    creator_id = item.get("creatorUserId") or course.get("teacherGroupEmail")
    if creator_id:
        try:
            profile = classroom.userProfiles().get(userId=creator_id).execute()
            teacher_name = profile.get("name", {}).get("fullName") or profile.get("emailAddress")
        except Exception:
            teacher_name = None

    assigner = f"{course['name']}: {teacher_name}" if teacher_name else course["name"]
    content = f"Classroom Assignment: {item['title']} for {course['name']}. Assigned By: {assigner}. Due: {due_str}. Instructions: {item.get('description', '')}"
    due_date = google_due_to_iso(item.get("dueDate", {}), item.get("dueTime"), utc_offset_minutes, tz_name)
    is_all_day = not has_google_due_time(item.get("dueTime"))
    task = make_structured_task(
        title=item.get("title", "Classroom Assignment"),
        description=item.get("description", ""),
        due_date=due_date,
        assigner=assigner,
        is_all_day=is_all_day,
        confidence=98 if due_date else 88
    )
    return task, content, f"classroom: {item['id']}"

# --- GOOGLE CLASSROOM SYNC ---
def collect_classroom_entries(classroom, db: Session, user_id: int, utc_offset_minutes: int = 0, tz_name: Optional[str] = None):
    """Fetch coursework from every enrolled Google Classroom course and return actionable assignments.
    Runs a noise-cleanup pass first to remove previously imported date-only titles."""
    summary = {"classroom": 0, "skipped": 0, "already_scanned": 0, "cleaned": cleanup_classroom_noise_tasks(db, user_id)}
    sync_entries = []

    courses_result = classroom.courses().list(pageSize=5).execute()
    for course in courses_result.get('courses', []):
        cw_result = classroom.courses().courseWork().list(courseId=course['id']).execute()
        for item in cw_result.get('courseWork', []):
            source_info = f"classroom: {item['id']}"
            if source_already_scanned(db, user_id, source_info):
                summary["already_scanned"] += 1
                continue

            # Don't import months-old coursework — only the recent sync window.
            if classroom_item_is_too_old(item, utc_offset_minutes):
                summary["skipped"] += 1
                mark_source_scanned(db, user_id, source_info, f"Skipped old Classroom post: {item.get('title', '')}")
                continue

            if not is_actionable_classroom_item(item):
                summary["skipped"] += 1
                mark_source_scanned(db, user_id, source_info, f"Skipped Classroom post: {item.get('title', '')}")
                continue

            try:
                sync_entries.append(classroom_item_to_entry(classroom, course, item, utc_offset_minutes, tz_name))
                summary["classroom"] += 1
            except Exception:
                summary["skipped"] += 1
                mark_source_scanned(db, user_id, source_info, f"Skipped Classroom post: {item.get('title', '')}")

    return sync_entries, summary

@app.get("/sync-gmail")
async def sync_gmail(user_id: int, current_user: User = Depends(get_current_user)):
    """API endpoint to trigger a Gmail sync for the user. Runs in a thread to avoid blocking async."""
    require_same_user(current_user, user_id)
    return await asyncio.to_thread(sync_gmail_blocking, user_id)

def sync_gmail_blocking(user_id: int):
    """Scan the 5 most recent Gmail messages, extract tasks, and save any not yet seen.
    Uses asyncio.run() to call the async save function from within this synchronous thread."""
    db = SessionLocal()
    try:
        ensure_user_exists(user_id, db)
        creds = get_google_creds(user_id)
        if not creds:
            return google_auth_required_response(user_id)
        service = build('gmail', 'v1', credentials=creds)
        # Fetch the 5 most recent emails
        results = service.users().messages().list(
            userId='me',
            maxResults=15,
            q=f"newer_than:{GOOGLE_SYNC_PAST_DAYS}d"
        ).execute()
        messages = results.get('messages', [])

        processed_count = 0
        skipped_count = 0
        for msg in messages:
            source_info = f"gmail: {msg['id']}"
            if source_already_scanned(db, user_id, source_info):
                skipped_count += 1
                continue

            m = service.users().messages().get(userId='me', id=msg['id']).execute()
            # Fetch full body instead of just the snippet
            body = get_email_body(m.get('payload', {})) or m.get('snippet', '')
            sender = get_email_sender(m.get('payload', {}))
            if body:
                try:
                    if sender:
                        body = f"Email From: {sender}\n{body}"
                    result = asyncio.run(process_and_save_tasks(body, user_id, source_info, db))
                    if not result.get("task_ids"):
                        mark_source_scanned(db, user_id, source_info, body)
                    processed_count += 1
                except HTTPException:
                    continue
        return {"status": "success", "message": f"Scanned {processed_count} new emails, skipped {skipped_count} already scanned emails."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

def remember_user_timezone(db: Session, user: User, tz_name: Optional[str]):
    """Persist the browser-reported IANA timezone so background auto-sync can
    convert Classroom due times correctly even without a live request."""
    if tz_name and re.fullmatch(r"[A-Za-z0-9_+\-/]{1,64}", tz_name) and user.timezone_name != tz_name:
        user.timezone_name = tz_name
        db.commit()

@app.get("/sync-classroom")
async def sync_classroom(user_id: int, tz_offset: int = 0, tz_name: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """API endpoint to trigger a Google Classroom sync. tz_name (IANA) gives DST-correct
    due-time conversion; tz_offset is the legacy fixed-offset fallback."""
    require_same_user(current_user, user_id)
    return await asyncio.to_thread(sync_classroom_blocking, user_id, tz_offset, tz_name)

def sync_classroom_blocking(user_id: int, tz_offset: int = 0, tz_name: Optional[str] = None):
    """Fetch and save all actionable Classroom coursework not yet in the database."""
    db = SessionLocal()
    try:
        user = ensure_user_exists(user_id, db)
        remember_user_timezone(db, user, tz_name)
        creds = get_google_creds(user_id)
        if not creds:
            return google_auth_required_response(user_id)

        classroom = build('classroom', 'v1', credentials=creds)
        sync_entries, summary = collect_classroom_entries(classroom, db, user_id, tz_offset, tz_name or user.timezone_name)
        if sync_entries:
            asyncio.run(save_structured_task_entries(sync_entries, user_id, db))

        return {
            "status": "success",
            "processed": summary,
            "message": f"Added {summary['classroom']} Classroom tasks, skipped {summary['skipped']} non-task posts, ignored {summary['already_scanned']} already scanned posts, and cleaned {summary['cleaned']} old date-only tasks."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

def cleanup_stale_synced_tasks(db: Session, user_id: int) -> int:
    """Soft-delete previously imported Google-synced tasks that fall outside the sync window.

    Earlier versions imported the entire Classroom/Tasks history, so long-time users
    have months-old assignments cluttering their dashboard. Only touches still-pending
    tasks whose raw source is a Google sync (classroom/calendar/gtask/gmail) and whose
    schedule ended before the window — soft-deleted, so they remain restorable in History.
    """
    cutoff = datetime.now() - timedelta(days=GOOGLE_SYNC_PAST_DAYS)
    tasks = db.query(Task).filter(
        Task.owner_id == user_id,
        Task.status == "pending",
        Task.due_date.isnot(None)
    ).all()

    raw_ids = [t.raw_id for t in tasks if t.raw_id]
    raw_map = {}
    if raw_ids:
        raw_map = {r.raw_id: r for r in db.query(RawInput).filter(RawInput.raw_id.in_(raw_ids)).all()}

    removed = 0
    for task in tasks:
        due = _parse_wall_datetime(task.due_date)
        if not due or due >= cutoff:
            continue
        # Multi-day items still running (or recently ended) stay.
        end = _parse_wall_datetime(task.end_date)
        if end and end >= cutoff:
            continue
        raw = raw_map.get(task.raw_id) if task.raw_id else None
        source = str(raw.source_type or "") if raw else ""
        if source.startswith(("classroom:", "calendar:", "gtask:", "gmail:")):
            task.status = "deleted"
            removed += 1

    if removed:
        db.commit()
    return removed

def cleanup_unwanted_calendar_imports(db: Session, user_id: int, prune_far_future: bool = True) -> int:
    """Remove calendar noise imported by earlier syncs.

    1. Holiday-calendar observances ("St. Patrick's Day", "Tax Day") are soft-deleted -
       identified by the boilerplate description Google attaches to holiday events.
       Their scanned markers are kept so they are never re-imported.
    2. Far-future event instances beyond the sync horizon are hard-deleted together
       with their scanned markers, so each one re-imports naturally once its date
       comes within the horizon.
    """
    holiday_rows = db.query(Task, RawInput).outerjoin(
        RawInput, Task.raw_id == RawInput.raw_id
    ).filter(
        Task.owner_id == user_id,
        Task.status == "pending",
        Task.item_type == "event"
    ).all()

    removed = 0
    for task, raw in holiday_rows:
        if is_holiday_calendar_task(task, raw):
            task.status = "deleted"
            removed += 1

    if not prune_far_future:
        if removed:
            db.commit()
        return removed

    future_days = google_calendar_sync_future_days()
    horizon = datetime.now() + timedelta(days=future_days)
    candidates = db.query(Task).filter(
        Task.owner_id == user_id,
        Task.status == "pending",
        Task.item_type == "event",
        Task.assignee == "Google Calendar",
        Task.due_date.isnot(None)
    ).all()
    far_future = []
    for task in candidates:
        due = _parse_wall_datetime(task.due_date)
        if due and due > horizon:
            far_future.append(task)
    if far_future:
        task_ids = [t.task_id for t in far_future]
        raw_ids = [t.raw_id for t in far_future if t.raw_id]
        db.query(Task).filter(Task.task_id.in_(task_ids)).delete(synchronize_session=False)
        if raw_ids:
            db.query(RawInput).filter(RawInput.raw_id.in_(raw_ids)).delete(synchronize_session=False)
        removed += len(task_ids)

    if removed:
        db.commit()
    return removed

def cleanup_unwanted_calendar_imports_safely(db: Session, user_id: int, prune_far_future: bool = True) -> int:
    """Best-effort cleanup that must never break task loading or Google sync."""
    try:
        return cleanup_unwanted_calendar_imports(db, user_id, prune_far_future=prune_far_future)
    except Exception:
        db.rollback()
        _log.exception("Calendar cleanup failed for user_id=%s", user_id)
        return 0

def cleanup_duplicate_calendar_events(db: Session, user_id: int) -> int:
    """Soft-delete exact-duplicate Google Calendar events (same title+date). Returns count removed."""
    events = db.query(Task).filter(
        Task.owner_id == user_id,
        Task.item_type == "event",
        Task.assignee == "Google Calendar",
        Task.status != "deleted"
    ).order_by(Task.task_id).all()

    seen: dict = {}
    removed = 0
    for event in events:
        title_key = normalize_title_for_match(event.title)
        date_key = due_day_key(event.due_date)
        if not title_key:
            continue
        key = (title_key, date_key)
        if key in seen:
            event.status = "deleted"
            removed += 1
        else:
            seen[key] = event.task_id

    if removed > 0:
        db.commit()
    return removed

@app.get("/sync-all")
async def sync_all(user_id: int, tz_offset: int = 0, tz_name: Optional[str] = None, current_user: User = Depends(get_current_user)):
    """Trigger a full sync across Gmail, Classroom, Calendar, and Google Tasks in one call."""
    require_same_user(current_user, user_id)
    return await asyncio.to_thread(sync_all_blocking, user_id, tz_offset, tz_name)

def sync_all_blocking(user_id: int, tz_offset: int = 0, tz_name: Optional[str] = None):
    """Collect entries from all Google sources and save them in one batch.
    Runs sequentially (Classroom → Calendar → Tasks) so errors in one source don't block others."""
    db = SessionLocal()
    try:
        user = ensure_user_exists(user_id, db)
        remember_user_timezone(db, user, tz_name)
        creds = get_google_creds(user_id)
        if not creds:
            # If no credentials, return the auth URL for the frontend to redirect
            return google_auth_required_response(user_id)

        cleanup_duplicate_calendar_events(db, user_id)
        stale_cleaned = cleanup_stale_synced_tasks(db, user_id)
        stale_cleaned += cleanup_unwanted_calendar_imports_safely(db, user_id)

        classroom = build('classroom', 'v1', credentials=creds)
        calendar = build('calendar', 'v3', credentials=creds)

        summary = {"classroom": 0, "calendar": 0, "gtasks": 0, "stale_cleaned": stale_cleaned}
        sync_entries = []

        # 1. Classroom assignments
        try:
            classroom_entries, classroom_summary = collect_classroom_entries(classroom, db, user_id, tz_offset, tz_name or user.timezone_name)
            sync_entries.extend(classroom_entries)
            summary["classroom"] = classroom_summary["classroom"]
            summary["classroom_skipped"] = classroom_summary["skipped"]
        except Exception as e:
            print(f"Classroom sync error: {e}")

        # 2. All Google Calendar events (all calendars, including birthdays)
        try:
            calendar_entries, calendar_summary = collect_google_calendar_entries(calendar, db, user_id)
            sync_entries.extend(calendar_entries)
            summary.update(calendar_summary)
        except Exception as e:
            print(f"Calendar sync error: {e}")

        # 3. Google Tasks
        try:
            tasks_service = build('tasks', 'v1', credentials=creds)
            tasks_entries, tasks_summary = collect_google_tasks_entries(tasks_service, db, user_id)
            sync_entries.extend(tasks_entries)
            summary["gtasks"] = tasks_summary["gtasks"]
        except Exception as e:
            print(f"Google Tasks sync error: {e}")

        if sync_entries:
            asyncio.run(save_structured_task_entries(sync_entries, user_id, db))

        return {"status": "success", "processed": summary}
    except HTTPException:
        raise
    finally:
        db.close()

# --- AUTO-SYNC BACKGROUND LOOP ---
async def auto_sync_user(user_id: int, db: Session):
    """Run a full sync for a single user asynchronously — called by the background loop.
    Errors in any one source are caught and logged without stopping the other sources."""
    creds = get_google_creds(user_id)
    if not creds:
        return {"gmail": 0, "classroom": 0, "calendar": 0}

    # Use the timezone remembered from the user's last manual sync for due-time conversion.
    sync_user = db.query(User).filter(User.user_id == user_id).first()
    user_tz = sync_user.timezone_name if sync_user else None

    cleanup_duplicate_calendar_events(db, user_id)
    cleanup_stale_synced_tasks(db, user_id)
    cleanup_unwanted_calendar_imports_safely(db, user_id)

    gmail_count = 0
    try:
        service = build('gmail', 'v1', credentials=creds)
        results = service.users().messages().list(
            userId='me',
            maxResults=15,
            q=f"newer_than:{GOOGLE_SYNC_PAST_DAYS}d"
        ).execute()
        for msg in results.get('messages', []):
            source_info = f"gmail: {msg['id']}"
            if source_already_scanned(db, user_id, source_info):
                continue

            m = service.users().messages().get(userId='me', id=msg['id']).execute()
            body = get_email_body(m.get('payload', {})) or m.get('snippet', '')
            sender = get_email_sender(m.get('payload', {}))
            if not body:
                mark_source_scanned(db, user_id, source_info, "Empty Gmail message")
                continue

            if sender:
                body = f"Email From: {sender}\n{body}"
            result = await process_and_save_tasks(body, user_id, source_info, db)
            if not result.get("task_ids"):
                mark_source_scanned(db, user_id, source_info, body)
            gmail_count += 1
    except Exception as exc:
        print(f"Auto Gmail sync error for user {user_id}: {exc}")

    classroom_count = 0
    try:
        classroom = build('classroom', 'v1', credentials=creds)
        entries, summary = collect_classroom_entries(classroom, db, user_id, tz_name=user_tz)
        if entries:
            await save_structured_task_entries(entries, user_id, db)
        classroom_count = summary.get("classroom", 0)
    except Exception as exc:
        print(f"Auto Classroom sync error for user {user_id}: {exc}")

    calendar_count = 0
    try:
        calendar = build('calendar', 'v3', credentials=creds)
        entries, summary = collect_google_calendar_entries(calendar, db, user_id)
        if entries:
            await save_structured_task_entries(entries, user_id, db)
        calendar_count = summary.get("calendar", 0)
    except Exception as exc:
        print(f"Auto Calendar sync error for user {user_id}: {exc}")

    gtasks_count = 0
    try:
        tasks_service = build('tasks', 'v1', credentials=creds)
        entries, summary = collect_google_tasks_entries(tasks_service, db, user_id)
        if entries:
            await save_structured_task_entries(entries, user_id, db)
        gtasks_count = summary.get("gtasks", 0)
    except Exception as exc:
        print(f"Auto Google Tasks sync error for user {user_id}: {exc}")

    return {"gmail": gmail_count, "classroom": classroom_count, "calendar": calendar_count, "gtasks": gtasks_count}

def get_auto_sync_user_ids(db: Session) -> List[int]:
    """Return the IDs of every user who has a connected Google account, so we know who to auto-sync."""
    # Find all users who have a stored Google token in the database.
    return [
        u.user_id for u in
        db.query(User).filter(User.google_token_json.isnot(None)).all()
    ]

def run_auto_sync_once_blocking():
    """Synchronously iterate all Google-connected users and run their sync.
    Uses asyncio.run() because this function is called from a thread (not an async context)."""
    db = SessionLocal()
    try:
        for user_id in get_auto_sync_user_ids(db):
            asyncio.run(auto_sync_user(user_id, db))
    finally:
        db.close()

async def run_auto_sync_once():
    """Async wrapper that offloads the blocking sync loop to a thread so it doesn't stall the event loop."""
    await asyncio.to_thread(run_auto_sync_once_blocking)

async def auto_sync_loop():
    """Background task that wakes up every AUTO_SYNC_INTERVAL_SECONDS and syncs all users.
    The initial 10-second delay lets the server finish starting up before the first sync."""
    await asyncio.sleep(10)
    while True:
        try:
            await run_auto_sync_once()
        except Exception as exc:
            print(f"Auto sync loop error: {exc}")
        await asyncio.sleep(AUTO_SYNC_INTERVAL_SECONDS)

# --- TASK EXTRACTION AND SAVING ---
async def process_and_save_tasks(text_content, user_id, source_info, db, current_time=None):
    """Run AI task extraction on raw text, then save results. Offloads the extraction to a thread
    because the AI call is CPU/IO-bound and would block the async event loop."""
    structured_tasks = await asyncio.to_thread(extract_task_from_text, text_content, current_time)
    return await save_structured_tasks(structured_tasks, text_content, user_id, source_info, db)

async def save_structured_tasks(structured_tasks, text_content, user_id, source_info, db):
    """Convert a list of extracted task dicts into (task, content, source) tuples and save them."""
    if not structured_tasks:
        # Return a success with 0 tasks instead of a 500 error
        return {"status": "success", "task_ids": [], "message": "No actionable tasks found in input."}

    # Deduplication strategy per source type:
    #   text    — every submission is intentional; use a random tag so the same text
    #             can be re-submitted and all extracted tasks are always saved.
    #   file:*  — same file content → same tasks, so dedup by content hash; but save
    #             ALL tasks from a new file by giving each its own index suffix.
    #   other   — Gmail/Calendar/Tasks sources each already have a unique source id,
    #             so pass through unchanged.
    if source_info == "text":
        tag = secrets.token_hex(6)
        entries = [
            (task_data, text_content, f"text:{tag}:{i}")
            for i, task_data in enumerate(structured_tasks)
        ]
    elif source_info.startswith("file:"):
        content_hash = hashlib.sha256(text_content.encode()).hexdigest()[:12]
        entries = [
            (task_data, text_content, f"{source_info}:{content_hash}:{i}")
            for i, task_data in enumerate(structured_tasks)
        ]
    else:
        entries = [(task_data, text_content, source_info) for task_data in structured_tasks]
    return await save_structured_task_entries(entries, user_id, db)

async def save_structured_task_entries(entries, user_id, db):
    """Core save function: iterate entries, skip already-seen sources, detect duplicates,
    and either create new Task rows or update the matching existing ones with authoritative data."""
    if not entries:
        return {"status": "success", "task_ids": [], "message": "No actionable tasks found in input."}

    try:
        task_ids = []
        duplicate_index = build_duplicate_index(db, user_id)

        # Pre-load all source markers for this batch in a single query rather than one per entry.
        all_markers = {source_marker(user_id, src) for _, _, src in entries}
        scanned_markers = set(
            row[0] for row in db.query(RawInput.source_id).filter(
                RawInput.source_id.in_(all_markers)
            ).all()
        )

        for task_data, text_content, source_info in entries:
            if source_marker(user_id, source_info) in scanned_markers:
                continue

            marker = source_marker(user_id, source_info)
            scanned_markers.add(marker)
            new_raw = RawInput(
                content=text_content[:500],
                source_type=source_info,
                source_id=marker,
                received_at=datetime.now()
            )
            db.add(new_raw)
            db.flush()

            normalized_assigner = normalize_task_assigner(task_data, text_content, source_info)
            duplicate = find_duplicate_task(duplicate_index, task_data)
            if duplicate:
                if normalized_assigner != "me" and (not duplicate.assignee or duplicate.assignee == "me"):
                    duplicate.assignee = normalized_assigner
                # Calendar, Google Tasks, and Classroom are authoritative: update the existing
                # task's dates so these versions always win over Gmail/email guesses.
                is_authoritative = (
                    source_info.startswith("calendar:")
                    or source_info.startswith("gtask:")
                    or source_info.startswith("classroom:")
                )
                if is_authoritative:
                    if task_data.get("due_date"):
                        duplicate.due_date = task_data["due_date"]
                    if task_data.get("end_date"):
                        duplicate.end_date = task_data["end_date"]
                    if task_data.get("is_all_day") is not None:
                        duplicate.is_all_day = 1 if task_data["is_all_day"] else 0
                elif task_data.get("due_date") and not duplicate.due_date:
                    # Non-authoritative source, but the existing copy has no date at all —
                    # a dated duplicate (e.g. from a PDF) is still better than nothing.
                    duplicate.due_date = task_data["due_date"]
                    if task_data.get("end_date"):
                        duplicate.end_date = task_data["end_date"]
                    if task_data.get("is_all_day") is not None:
                        duplicate.is_all_day = 1 if task_data["is_all_day"] else 0
                task_ids.append(duplicate.task_id)
                continue

            new_task = Task(
                owner_id=user_id,
                raw_id=new_raw.raw_id,
                title=task_data.get("title"),
                description=task_data.get("description"),
                due_date=task_data.get("due_date"), # Store None if not provided, not "None" string
                end_date=task_data.get("end_date"),
                due_text=task_data.get("due"),
                assignee=normalized_assigner,
                priority=task_data.get("priority", "normal"),
                is_all_day=1 if task_data.get("is_all_day") else 0,
                item_type=task_data.get("item_type", "task"),
                confidence=task_data.get("confidence"),
                status="pending"
            )
            db.add(new_task)
            db.flush()
            task_ids.append(new_task.task_id)
            key = task_match_key({"title": new_task.title, "due_date": new_task.due_date})
            if key:
                duplicate_index[key] = new_task
                duplicate_index["__items__"].append(new_task)

        db.commit()
        return {"status": "success", "task_ids": task_ids, "message": f"Extracted {len(task_ids)} tasks"}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# --- TASK MANAGEMENT ROUTES ---
@app.post("/tasks/deduplicate")
async def deduplicate_tasks(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Remove duplicate Google Calendar events with the same title and date."""
    require_same_user(current_user, user_id)
    removed = cleanup_duplicate_calendar_events(db, user_id)
    return {"status": "success", "removed": removed, "message": f"Removed {removed} duplicate calendar event(s)."}

@app.get("/tasks")
async def get_tasks(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Return all tasks for a user. Opportunistically backfills missing assignee values from the raw
    source text so older tasks gradually get proper labels without a migration script."""
    require_same_user(current_user, user_id)
    cleanup_unwanted_calendar_imports_safely(db, user_id, prune_far_future=False)
    tasks = db.query(Task).filter(Task.owner_id == user_id).all()

    # Only fetch RawInput rows for tasks that are still missing an assignee label.
    needs_raw = [t for t in tasks if t.raw_id and (not t.assignee or t.assignee == "me")]
    raw_map = {}
    if needs_raw:
        raw_ids = [t.raw_id for t in needs_raw]
        raw_map = {r.raw_id: r for r in db.query(RawInput).filter(RawInput.raw_id.in_(raw_ids)).all()}

    changed = False
    for task in needs_raw:
        raw = raw_map.get(task.raw_id)
        if not raw:
            continue
        inferred_assigner = normalize_task_assigner({}, raw.content, raw.source_type)
        if inferred_assigner != "me":
            task.assignee = inferred_assigner
            changed = True

    if changed:
        db.commit()

    return {"tasks": [task_to_dict(task) for task in tasks]}

# --- USER SETTINGS ENDPOINTS ---
@app.get("/users/{user_id}/settings")
async def get_user_settings(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Return the current settings/preferences for a user as a flat JSON object."""
    require_same_user(current_user, user_id)
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        **user_settings_payload(user)
    }

@app.patch("/users/{user_id}/settings")
async def update_user_settings(user_id: int, settings: UserSettingsUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Apply only the fields provided in the PATCH body — None fields are left unchanged.
    Validates hours, email format, and prevents enabling 2FA without a verified code."""
    require_same_user(current_user, user_id)
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if settings.preferred_name is not None:
        user.preferred_name = settings.preferred_name
    if settings.schedule_match_name is not None:
        user.schedule_match_name = settings.schedule_match_name.strip() or None
    if settings.email is not None:
        email = settings.email.strip()
        if email and not is_valid_email(email):
            raise HTTPException(status_code=400, detail="Enter a valid email address.")
        user.email = email or None
    if settings.preferred_work_start_hour is not None:
        if settings.preferred_work_start_hour < 0 or settings.preferred_work_start_hour > 23:
            raise HTTPException(status_code=400, detail="Work start hour must be between 0 and 23.")
        user.preferred_work_start_hour = settings.preferred_work_start_hour
    if settings.preferred_work_end_hour is not None:
        if settings.preferred_work_end_hour < 1 or settings.preferred_work_end_hour > 24:
            raise HTTPException(status_code=400, detail="Work end hour must be between 1 and 24.")
        user.preferred_work_end_hour = settings.preferred_work_end_hour
    if (
        user.preferred_work_start_hour is not None
        and user.preferred_work_end_hour is not None
        and user.preferred_work_start_hour >= user.preferred_work_end_hour
    ):
        raise HTTPException(status_code=400, detail="Work start must be before work end.")
    if settings.dark_mode is not None:
        user.dark_mode = 1 if settings.dark_mode else 0
    if settings.notifications_enabled is not None:
        user.notifications_enabled = 1 if settings.notifications_enabled else 0
    if settings.two_factor_enabled is not None:
        if settings.two_factor_enabled:
            raise HTTPException(status_code=400, detail="Verify your email code before enabling 2FA.")
        else:
            user.two_factor_enabled = 0
            user.two_factor_code_hash = None
            user.two_factor_expires_at = None

    db.commit()
    return {"message": "Settings updated"}

# --- 2FA SETUP ENDPOINTS ---
@app.post("/users/{user_id}/2fa/send-test")
async def send_two_factor_test(user_id: int, request: TwoFactorSendRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Send a 6-digit test code to the user's email before they fully enable 2FA.
    Returns the code in the response body when SMTP is unavailable, for dev/testing convenience."""
    require_same_user(current_user, user_id)
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if request.email is not None:
        email = request.email.strip()
        if not is_valid_email(email):
            raise HTTPException(status_code=400, detail="Enter a valid email address.")
        user.email = email

    send_result = send_two_factor_code(user)
    db.commit()
    message = "Verification code sent to your email."
    if not send_result.get("sent"):
        if send_result.get("reason") == "smtp_not_configured":
            message = "Code generated — enter it in the field below."
        else:
            message = "Code generated, but email delivery failed. Check SMTP settings."
    result = {"status": "success" if send_result.get("sent") or send_result.get("dev_code") else "warning", "message": message}
    if send_result.get("dev_code"):
        result["dev_code"] = send_result["dev_code"]
    if not send_result.get("sent") and not send_result.get("dev_code"):
        result["smtp_error"] = True
    return result

@app.post("/users/{user_id}/2fa/verify")
async def verify_two_factor_setup(user_id: int, request: TwoFactorVerifyRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Verify the test code and, if correct, officially enable 2FA on the account.
    Clears the code hash after success so the same code can't be replayed."""
    require_same_user(current_user, user_id)
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if request.email is not None:
        email = request.email.strip()
        if not is_valid_email(email):
            raise HTTPException(status_code=400, detail="Enter a valid email address.")
        user.email = email
    if not verify_two_factor_code(user, request.code):
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

    user.two_factor_enabled = 1
    user.two_factor_code_hash = None
    user.two_factor_expires_at = None
    db.commit()
    return {"status": "success", "message": "Two-factor authentication is now enabled."}

@app.get("/tasks/history")
async def get_task_history(user_id: int, db: Session = Depends(get_db), limit: int = 50, current_user: User = Depends(get_current_user)):
    """
    Fetches recently completed or deleted tasks for a user.
    """
    require_same_user(current_user, user_id)
    # Auto-delete tasks older than 7 days from history
    cutoff = datetime.utcnow() - timedelta(days=7)
    db.query(Task).filter(
        Task.owner_id == user_id,
        (Task.status == 'completed') | (Task.status == 'deleted'),
        Task.created_at < cutoff
    ).delete(synchronize_session=False)
    db.commit()

    history_tasks = db.query(Task).filter(
        Task.owner_id == user_id,
        (Task.status == 'completed') | (Task.status == 'deleted')
    ).order_by(Task.created_at.desc()).limit(limit).all()
    return {"tasks": [task_to_dict(task) for task in history_tasks]}

@app.patch("/tasks/{task_id}")
async def update_task(task_id: int, task_update: TaskUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Partially update a task's fields. Date-only strings are padded to full ISO datetimes
    so the database stores a consistent format regardless of how the frontend sends the value."""
    task = db.query(Task).filter(Task.task_id == task_id, Task.owner_id == current_user.user_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task_update.title: task.title = task_update.title
    if task_update.description is not None: task.description = task_update.description
    if task_update.priority: task.priority = task_update.priority
    if task_update.item_type: task.item_type = task_update.item_type
    if task_update.status: task.status = task_update.status # This handles "completed" and "deleted"
    if task_update.is_all_day is not None: task.is_all_day = 1 if task_update.is_all_day else 0
    if task_update.due_date:
        new_date = task_update.due_date
        if len(new_date) == 10: new_date += "T12:00:00Z"
        task.due_date = new_date
    if task_update.end_date:
        new_end = task_update.end_date
        if len(new_end) == 10: new_end += "T13:00:00Z"
        task.end_date = new_end

    db.commit()
    return {"message": "Updated successfully"}

@app.patch("/tasks/bulk/update")
async def bulk_update_tasks(action: BulkTaskAction, status: str = "deleted", db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Set the status of multiple tasks in one DB query. Uses dict.fromkeys to deduplicate IDs."""
    if status not in {"deleted", "completed", "pending"}:
        raise HTTPException(status_code=400, detail="Unsupported bulk status.")

    task_ids = list(dict.fromkeys(action.task_ids or []))
    if not task_ids:
        raise HTTPException(status_code=400, detail="No tasks selected.")

    updated = db.query(Task).filter(
        Task.task_id.in_(task_ids),
        Task.owner_id == current_user.user_id
    ).update(
        {Task.status: status},
        synchronize_session=False
    )
    db.commit()
    return {"message": f"Updated {updated} tasks.", "updated": updated}

@app.delete("/tasks/bulk/permanent")
async def bulk_permanent_delete_tasks(action: BulkTaskAction, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Permanently remove multiple tasks from the database (not just soft-delete). Irreversible."""
    task_ids = list(dict.fromkeys(action.task_ids or []))
    if not task_ids:
        raise HTTPException(status_code=400, detail="No tasks selected.")

    deleted = db.query(Task).filter(
        Task.task_id.in_(task_ids),
        Task.owner_id == current_user.user_id
    ).delete(synchronize_session=False)
    db.commit()
    return {"message": f"Permanently deleted {deleted} tasks.", "deleted": deleted}

@app.delete("/tasks/{task_id}/permanent")
async def permanent_delete_task(task_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Permanently delete a single task row. Distinct from PATCH status='deleted' (soft-delete)."""
    task = db.query(Task).filter(Task.task_id == task_id, Task.owner_id == current_user.user_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete(task)
    db.commit()
    return {"message": "Task permanently deleted"}


# --- FEEDBACK LEARNING ---

class FeedbackRequest(BaseModel):
    user_id: int
    vote: int  # +1 = correct extraction, -1 = incorrect


@app.post("/tasks/{task_id}/feedback")
async def submit_task_feedback(task_id: int, data: FeedbackRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Record whether the user thinks a task was extracted correctly.

    A +1 vote means the extraction was accurate; -1 means it was wrong.
    The vote is stored on the task row and factored into the confidence score
    so Clerk can learn that this user's texts tend to produce reliable or
    unreliable extractions.
    """
    require_same_user(current_user, data.user_id)
    task = db.query(Task).filter(Task.task_id == task_id, Task.owner_id == data.user_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if data.vote not in (-1, 0, 1):
        raise HTTPException(status_code=400, detail="vote must be -1, 0, or +1")

    task.user_feedback = data.vote

    # Adjust confidence to reflect user's judgment, clamped to [0, 100].
    if task.confidence is not None:
        base = float(task.confidence)
        if data.vote == 1:
            task.confidence = min(100.0, base + 10.0)
        elif data.vote == -1:
            task.confidence = max(0.0, base - 20.0)
        else:
            # Neutral reset — leave score unchanged but clear prior vote.
            pass
    db.commit()
    return {"status": "ok", "task_id": task_id, "confidence": task.confidence}


@app.get("/tasks/{task_id}/feedback")
async def get_task_feedback(task_id: int, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Return the current feedback vote for a task."""
    require_same_user(current_user, user_id)
    task = db.query(Task).filter(Task.task_id == task_id, Task.owner_id == user_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "vote": task.user_feedback}


# --- INSIGHTS SUMMARY ---

# Cache AI-generated briefs per user so repeat visits to the Insights page don't
# re-spend tokens. Invalidated when the user's task state changes (hash mismatch).
_insights_cache: dict = {}
INSIGHTS_CACHE_SECONDS = int(os.environ.get("INSIGHTS_CACHE_SECONDS", "600"))

def _parse_wall_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-ish date string as naive wall-clock time (drops Z / offsets)."""
    if not value:
        return None
    try:
        cleaned = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', str(value))
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None

def _build_insights_stats(tasks: List[Task], now: datetime) -> dict:
    """Aggregate the numbers the Insights page shows and the summary text references."""
    today = now.date()
    week_end = today + timedelta(days=7)
    stats = {
        "active": 0, "due_today": 0, "overdue": 0, "due_this_week": 0,
        "high_priority": 0, "low_confidence": 0, "no_due_date": 0,
        "reminders": 0, "events": 0,
    }
    day_load: dict = {}
    next_up = None
    for task in tasks:
        stats["active"] += 1
        if task.item_type == "reminder":
            stats["reminders"] += 1
        elif task.item_type == "event":
            stats["events"] += 1
        if task.priority == "high":
            stats["high_priority"] += 1
        if task.confidence is not None and task.confidence < 70:
            stats["low_confidence"] += 1
        due = _parse_wall_datetime(task.due_date)
        if not due:
            stats["no_due_date"] += 1
            continue
        due_day = due.date()
        if due_day == today:
            stats["due_today"] += 1
        elif due_day < today:
            stats["overdue"] += 1
        elif due_day <= week_end:
            stats["due_this_week"] += 1
        if today <= due_day <= week_end:
            day_load[due_day] = day_load.get(due_day, 0) + 1
        if due >= now and (next_up is None or due < next_up[0]):
            next_up = (due, task.title)

    if day_load:
        busiest_day, busiest_count = max(day_load.items(), key=lambda item: item[1])
        stats["busiest_day"] = busiest_day.strftime("%A")
        stats["busiest_day_count"] = busiest_count
    if next_up:
        stats["next_up_title"] = next_up[1]
        stats["next_up_at"] = next_up[0].strftime("%Y-%m-%dT%H:%M:%S")
    return stats

def _rules_based_summary(stats: dict, preferred_name: str) -> str:
    """Build a readable daily brief without any AI call — always available."""
    parts = []
    if stats["active"] == 0:
        return "You're all caught up — no active tasks right now. Add notes, upload a file, or sync Google to fill your queue."
    opening = f"You have {stats['active']} active item{'s' if stats['active'] != 1 else ''}"
    middles = []
    if stats["due_today"]:
        middles.append(f"{stats['due_today']} due today")
    if stats["overdue"]:
        middles.append(f"{stats['overdue']} overdue")
    if stats["due_this_week"]:
        middles.append(f"{stats['due_this_week']} more due this week")
    parts.append(opening + (" — " + ", ".join(middles) if middles else "") + ".")
    if stats.get("next_up_title"):
        when = _parse_wall_datetime(stats.get("next_up_at"))
        when_text = when.strftime("%a %b %d at %I:%M %p").replace(" 0", " ") if when else "soon"
        parts.append(f"Next up: “{stats['next_up_title']}” on {when_text}.")
    if stats.get("busiest_day") and stats.get("busiest_day_count", 0) > 1:
        parts.append(f"{stats['busiest_day']} is your busiest day with {stats['busiest_day_count']} items.")
    if stats["low_confidence"]:
        parts.append(f"{stats['low_confidence']} extraction{'s' if stats['low_confidence'] != 1 else ''} below 70% confidence could use a quick review.")
    if stats["no_due_date"]:
        parts.append(f"{stats['no_due_date']} item{'s' if stats['no_due_date'] != 1 else ''} still have no due date.")
    return " ".join(parts)

def _ai_insights_summary(stats: dict, task_lines: List[str], preferred_name: str) -> Optional[str]:
    """Ask the AI for a 2–3 sentence brief. Returns None on any failure so the
    caller can fall back to the rules-based summary."""
    try:
        from .extractor import client as _ai_client
    except ImportError:
        from extractor import client as _ai_client
    if not _ai_client:
        return None
    try:
        prompt = (
            "You are Clerk, a friendly task assistant. Write a 2-3 sentence daily brief "
            f"for {preferred_name or 'the user'} based on these stats and upcoming tasks. "
            "Be specific and practical (what to do first, what's at risk). Plain text only, no lists.\n\n"
            f"Stats: {json.dumps(stats)}\n"
            "Upcoming tasks:\n" + "\n".join(task_lines[:15])
        )
        response = _ai_client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.4"),
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=220,
        )
        text = (response.choices[0].message.content or "").strip()
        return text or None
    except Exception as exc:
        print(f"[insights] AI summary failed, using rules-based text: {exc}")
        return None

@app.get("/insights/summary")
async def get_insights_summary(
    user_id: int,
    local_time: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return a short natural-language brief plus aggregate stats for the Insights page.
    Uses the AI model when configured (cached per task-state), otherwise a rules-based text."""
    require_same_user(current_user, user_id)
    now = _parse_wall_datetime(local_time) or datetime.now()
    tasks = db.query(Task).filter(
        Task.owner_id == user_id,
        Task.status.notin_(["deleted", "completed"])
    ).all()

    stats = _build_insights_stats(tasks, now)
    rules_summary = _rules_based_summary(stats, current_user.preferred_name or current_user.username)

    # The cache key covers every field that could change the brief.
    state_fingerprint = hashlib.sha256(json.dumps({
        "stats": stats,
        "day": now.strftime("%Y-%m-%d"),
    }, sort_keys=True, default=str).encode()).hexdigest()

    cached = _insights_cache.get(user_id)
    if cached and cached["hash"] == state_fingerprint and time.monotonic() - cached["at"] < INSIGHTS_CACHE_SECONDS:
        return {"summary": cached["summary"], "stats": stats, "generated_by": cached["generated_by"]}

    upcoming_lines = []
    for task in sorted(tasks, key=lambda t: t.due_date or "9999"):
        due = _parse_wall_datetime(task.due_date)
        upcoming_lines.append(f"- {task.title} (due {due.strftime('%Y-%m-%d %H:%M') if due else 'no date'}, priority {task.priority})")

    ai_summary = await asyncio.to_thread(
        _ai_insights_summary, stats, upcoming_lines, current_user.preferred_name or current_user.username
    )
    summary = ai_summary or rules_summary
    generated_by = "ai" if ai_summary else "rules"
    _insights_cache[user_id] = {
        "hash": state_fingerprint, "summary": summary,
        "generated_by": generated_by, "at": time.monotonic(),
    }
    # Keep the cache bounded.
    if len(_insights_cache) > 500:
        oldest = sorted(_insights_cache.items(), key=lambda item: item[1]["at"])[:100]
        for key, _ in oldest:
            _insights_cache.pop(key, None)
    return {"summary": summary, "stats": stats, "generated_by": generated_by}

# --- ACCOUNT DELETION ---

@app.delete("/users/{user_id}")
async def delete_account(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Permanently delete a user account and all associated data.

    Removes every Task, RawInput, and User row for this user.
    This action is irreversible — the frontend must show a second confirmation
    before calling this endpoint.
    """
    require_same_user(current_user, user_id)
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Delete tasks first (FK dependency on raw_inputs via raw_id).
    task_ids = [t.task_id for t in db.query(Task.task_id).filter(Task.owner_id == user_id).all()]
    if task_ids:
        db.query(Task).filter(Task.task_id.in_(task_ids)).delete(synchronize_session=False)

    # Delete raw inputs created by this user (identified by the source_id prefix).
    db.query(RawInput).filter(
        RawInput.source_id.like(f"{user_id}:%")
    ).delete(synchronize_session=False)

    db.delete(user)
    db.commit()
    _log.info("Account deleted: user_id=%s", user_id)
    return {"status": "deleted", "user_id": user_id}
