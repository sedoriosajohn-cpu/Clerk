from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request
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
import os.path
import json
import hashlib
import secrets
import smtplib
import time
import re
from urllib.parse import quote
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/tasks.readonly',
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me.readonly'
]

# Calendar event types that are not meaningful tasks/reminders
_SKIP_CALENDAR_EVENT_TYPES = {"focusTime", "outOfOffice", "workingLocation"}

app = FastAPI()
SYNC_THROTTLE_SECONDS = int(os.environ.get("SYNC_THROTTLE_SECONDS", "30"))
AUTO_SYNC_ENABLED = os.environ.get("AUTO_SYNC_ENABLED", "1") == "1"
AUTO_SYNC_INTERVAL_SECONDS = int(os.environ.get("AUTO_SYNC_INTERVAL_SECONDS", "900"))
sync_request_log = {}
auto_sync_task = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_PATH = os.path.join(BASE_DIR, "..", "..", "credentials.json")
DEFAULT_GOOGLE_REDIRECT_URI = "http://localhost:8000/auth/google/callback"
DEFAULT_FRONTEND_URL = "http://127.0.0.1:8000"

# In-memory store for transient OAuth states (survives the round-trip; no disk needed).
_oauth_states: dict = {}
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "..", "..", "frontend", "clerk_website")), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def throttle_expensive_sync_routes(request: Request, call_next):
    if request.url.path not in {"/sync-gmail", "/sync-classroom", "/sync-all"}:
        return await call_next(request)

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

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def is_valid_email(value: Optional[str]) -> bool:
    return bool(value and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value.strip()))

def hash_two_factor_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()

def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def send_email_message(to_email: str, subject: str, body: str):
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
    if not is_valid_email(user.email):
        raise HTTPException(status_code=400, detail="Add a valid email address before enabling 2FA.")

    code = f"{secrets.randbelow(1000000):06d}"
    user.two_factor_code_hash = hash_two_factor_code(code)
    user.two_factor_expires_at = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
    result = send_email_message(
        user.email,
        "Your Clerk verification code",
        f"Your Clerk verification code is {code}. It expires in 10 minutes."
    )
    # Always pass the code back when email delivery fails for any reason,
    # so 2FA stays usable even when SMTP is misconfigured or credentials are wrong.
    if not result.get("sent"):
        result["dev_code"] = code
    return result

def verify_two_factor_code(user: User, code: Optional[str]) -> bool:
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
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        return bool(user and user.google_token_json)
    finally:
        db.close()

def user_settings_payload(user: User) -> dict:
    return {
        "preferred_name": user.preferred_name or user.username,
        "schedule_match_name": user.schedule_match_name or "",
        "email": user.email or "",
        "preferred_work_start_hour": user.preferred_work_start_hour if user.preferred_work_start_hour is not None else 9,
        "preferred_work_end_hour": user.preferred_work_end_hour if user.preferred_work_end_hour is not None else 17,
        "dark_mode": bool(user.dark_mode),
        "notifications_enabled": bool(user.notifications_enabled),
        "two_factor_enabled": bool(user.two_factor_enabled),
        "google_connected": has_google_token(user.user_id)
    }

def split_name_candidate(value: Optional[str]) -> List[str]:
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
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

def get_password_rule_results(password: str, username: Optional[str] = None) -> dict:
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

# --- SCHEMAS ---
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
    has_google   = os.path.exists(os.path.join(BASE_DIR, "..", "..", "credentials.json")) \
                   or bool(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
    has_smtp     = bool(os.environ.get("SMTP_HOST"))

    return {
        "openai":  has_openai,
        "google":  has_google,
        "smtp":    has_smtp,
        "ai_model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini") if has_openai else None,
    }

# --- FRONTEND ROUTES ---
@app.get("/")
async def read_index(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if code or error:
        return complete_google_oauth(code=code, state=state, error=error)
    return FileResponse(os.path.join(BASE_DIR, "..", "..", "frontend", "clerk_website", "index.html"))

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
    return FileResponse(os.path.join(BASE_DIR, "..", "..", "frontend", "clerk_website", "logo.png"))

@app.get("/privacy.html")
async def read_privacy():
    return FileResponse(os.path.join(BASE_DIR, "..", "..", "frontend", "clerk_website", "privacy.html"))

@app.get("/terms.html")
async def read_terms():
    return FileResponse(os.path.join(BASE_DIR, "..", "..", "frontend", "clerk_website", "terms.html"))

# --- AUDIO TRANSCRIPTION ---
@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """Accept an audio file and return a transcript via OpenAI Whisper."""
    try:
        from .extractor import client as openai_client
    except ImportError:
        from extractor import client as openai_client

    if not openai_client:
        raise HTTPException(
            status_code=503,
            detail="OpenAI API key not configured. Add OPENAI_API_KEY to your .env file."
        )

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file received.")

    import io
    audio_buf = io.BytesIO(audio_bytes)
    # Whisper needs a filename to detect the format
    filename = file.filename or "recording.webm"
    audio_buf.name = filename

    try:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, audio_buf, file.content_type or "audio/webm"),
        )
        return {"text": transcript.text}
    except Exception as e:
        print(f"[transcribe] Whisper error: {e}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")

# --- AUTH ROUTES ---
@app.post("/login")
async def login_user(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user or user.password_hash != data.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if user.two_factor_enabled:
        if data.two_factor_code:
            if not verify_two_factor_code(user, data.two_factor_code):
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

    return {"user_id": user.user_id, "username": user.username, "settings": user_settings_payload(user)}

@app.post("/register")
async def register_user(data: LoginRequest, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.username == data.username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already taken")
    validate_strong_password(data.password, data.username)
    new_user = User(username=data.username, password_hash=data.password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "User created", "user_id": new_user.user_id}

@app.post("/forgot-password")
async def forgot_password(data: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
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
    user.password_hash = data.password
    user.reset_password_token_hash = None
    user.reset_password_expires_at = None
    user.two_factor_code_hash = None
    user.two_factor_expires_at = None
    db.commit()
    return {"status": "success", "message": "Password reset. You can sign in with your new password."}

# --- TASK INGESTION (TEXT) ---
@app.post("/ingest")
async def ingest_task(data: UserInput, db: Session = Depends(get_db)):
    return await process_and_save_tasks(data.content, data.user_id, data.source_type, db, data.local_time)

# --- NEW: TASK INGESTION (DOCUMENTS) ---
@app.post("/ingest-doc")
async def ingest_doc(
    user_id: int = Form(...),
    local_time: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    content = ""
    file_type = file.content_type
    source_info = f"file: {file.filename}"

    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if file_type == "application/pdf":
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
        elif file_type in ["text/plain", "text/markdown"]:
            # Read Text bytes
            text_bytes = await file.read()
            content = text_bytes.decode("utf-8")
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
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {file_type}")

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

# --- GMAIL SYNC ---

def get_google_credentials_config():
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
    config = get_google_credentials_config()
    return config.get("web") or config.get("installed") or config

def get_google_redirect_uri():
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
    return hashlib.sha256(state.encode("utf-8")).hexdigest()

def create_google_auth_url(user_id: int):
    flow = build_google_flow()
    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent'
    )
    save_google_oauth_state(state, flow.code_verifier, user_id)
    return auth_url

def get_frontend_url():
    configured_url = os.environ.get("CLERK_FRONTEND_URL")
    if configured_url:
        return configured_url.rstrip("/")

    render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if render_hostname:
        return f"https://{render_hostname}"

    return DEFAULT_FRONTEND_URL

def save_google_oauth_state(state: str, code_verifier: str, user_id: int):
    _oauth_states[_state_key(state)] = {
        "state": state,
        "code_verifier": code_verifier,
        "user_id": user_id,
        "created_at": datetime.utcnow().isoformat(),
    }

def load_google_oauth_state(state: Optional[str]):
    if not state:
        return {}
    return _oauth_states.get(_state_key(state), {})

def clear_google_oauth_state(state: Optional[str]):
    if not state:
        return
    _oauth_states.pop(_state_key(state), None)

def clear_google_token(user_id: int):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user:
            user.google_token_json = None
            db.commit()
    finally:
        db.close()

def _save_google_token(user_id: int, token_json: str):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == user_id).first()
        if user:
            user.google_token_json = token_json
            db.commit()
    finally:
        db.close()

def is_invalid_google_grant(error: Exception) -> bool:
    message = str(error).lower()
    return "invalid_grant" in message or "expired or revoked" in message

def google_auth_required_response(user_id: int, message: Optional[str] = None):
    return {
        "auth_url": create_google_auth_url(user_id),
        "message": message or "Google needs to be reconnected. Please sign in again."
    }

def complete_google_oauth(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    frontend_url = get_frontend_url()
    if error:
        return RedirectResponse(url=f"{frontend_url}?google_error={quote(error, safe='')}")
    if not code:
        return RedirectResponse(url=f"{frontend_url}?google_error=missing_authorization_code")

    saved_state = {}
    try:
        saved_state = load_google_oauth_state(state)
        if not saved_state.get("code_verifier"):
            return RedirectResponse(url=f"{frontend_url}?google_error=missing_code_verifier")
        if not saved_state.get("user_id"):
            return RedirectResponse(url=f"{frontend_url}?google_error=missing_google_user")
        if saved_state.get("state") and state and saved_state["state"] != state:
            return RedirectResponse(url=f"{frontend_url}?google_error=oauth_state_mismatch")

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

def get_google_creds(user_id: int):
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

def get_gmail_service(user_id: int):
    creds = get_google_creds(user_id)
    if not creds:
        # If get_google_creds returns None, it means authentication is needed.
        # We raise HTTPException here for any direct backend calls that expect a service.
        raise HTTPException(status_code=401, detail="Google needs to be reconnected. Please connect via Settings.")
    return build('gmail', 'v1', credentials=creds)

@app.get("/auth/google")
async def get_google_auth_url(user_id: int, db: Session = Depends(get_db)):
    """Returns the Google OAuth URL for the frontend to redirect to."""
    ensure_user_exists(user_id, db)
    return {"auth_url": create_google_auth_url(user_id)}

@app.get("/auth/google/callback")
async def google_callback(code: Optional[str] = None, state: str = None, error: Optional[str] = None):
    """Handles the redirect from Google after the user authenticates."""
    return complete_google_oauth(code=code, state=state, error=error)

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
    headers = payload.get("headers", [])
    sender = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "from"), "")
    if not sender:
        return None
    name_match = re.match(r'\s*"?([^"<]+?)"?\s*(?:<[^>]+>)?\s*$', sender)
    if name_match:
        return name_match.group(1).strip()
    return sender.strip()

def clean_assigner_label(value: Optional[str], source_info: str) -> str:
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
    raw_assigner = (
        task_data.get("assigner")
        or task_data.get("assigned_by")
        or task_data.get("teacher")
        or task_data.get("sender")
        or infer_assigner_from_text(text_content, source_info)
        or task_data.get("assignee")
    )
    return clean_assigner_label(raw_assigner, source_info)

def task_to_dict(task: Task) -> dict:
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
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }

def normalize_title_for_match(title: Optional[str]) -> str:
    text = re.sub(r'[^a-z0-9\s]', ' ', str(title or "").lower())
    text = re.sub(
        r'\b(google classroom|classroom|calendar|event|assignment|new|posted|assigned|due|please|reminder|notification)\b',
        ' ',
        text
    )
    return re.sub(r'\s+', ' ', text).strip()

def title_tokens_for_match(title: Optional[str]) -> set:
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
    if smaller_count == 1:
        shared = next(iter(intersection), "")
        return bool(shared and len(shared) >= 5 and (left_normalized in right_normalized or right_normalized in left_normalized))

    return (len(intersection) / smaller_count) >= 0.75

def due_day_key(due_date: Optional[str]) -> str:
    if not due_date:
        return ""
    match = re.search(r'\d{4}-\d{2}-\d{2}', str(due_date))
    return match.group(0) if match else str(due_date)

def task_match_key(task_data) -> Optional[tuple]:
    title_key = normalize_title_for_match(task_data.get("title"))
    due_key = due_day_key(task_data.get("due_date"))
    if not title_key:
        return None
    return title_key, due_key

def build_duplicate_index(db: Session, user_id: int) -> dict:
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

def find_duplicate_task(duplicate_index: dict, task_data) -> Optional[Task]:
    key = task_match_key(task_data)
    if not key:
        return None

    exact_match = duplicate_index.get(key)
    if exact_match:
        return exact_match

    title_key, due_key = key
    if not due_key:
        for existing in duplicate_index.get("__items__", []):
            if normalize_title_for_match(existing.title) == title_key:
                return existing
        return None

    for existing in duplicate_index.get("__items__", []):
        if due_day_key(existing.due_date) == due_key and titles_are_similar(existing.title, task_data.get("title")):
            return existing

    # Cross-date pass: new task has a real date, existing task has none.
    # Catches Gmail/announcement tasks that were saved without a due date
    # and are later superseded by the authoritative Classroom entry.
    for existing in duplicate_index.get("__items__", []):
        if not existing.due_date and titles_are_similar(existing.title, task_data.get("title")):
            return existing

    return None

def has_google_due_time(due_time: Optional[dict]) -> bool:
    if not due_time:
        return False
    return any(due_time.get(key) is not None for key in ("hours", "minutes", "seconds", "nanos"))

def google_due_to_iso(due: dict, due_time: Optional[dict] = None, utc_offset_minutes: int = 0) -> Optional[str]:
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

    # Google Classroom dueTime is UTC. Convert to local wall-clock time using the
    # client's UTC offset (JS getTimezoneOffset convention: positive = west of UTC).
    utc_dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    local_dt = utc_dt - timedelta(minutes=utc_offset_minutes)
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S")

def format_due_for_frontend(due_date: Optional[str], is_all_day: bool = True) -> dict:
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

def source_marker(user_id: int, source_info: str) -> str:
    return f"{user_id}:{source_info}"

def source_already_scanned(db: Session, user_id: int, source_info: str) -> bool:
    marker = source_marker(user_id, source_info)
    if db.query(RawInput).filter(RawInput.source_id == marker).first():
        return True

    # Backward compatibility for rows created before source_id was populated.
    return db.query(Task).join(RawInput, Task.raw_id == RawInput.raw_id).filter(
        Task.owner_id == user_id,
        RawInput.source_type == source_info
    ).first() is not None

def mark_source_scanned(db: Session, user_id: int, source_info: str, content: str = ""):
    if source_already_scanned(db, user_id, source_info):
        return

    db.add(RawInput(
        content=(content or f"Scanned {source_info}")[:500],
        source_type=source_info,
        source_id=source_marker(user_id, source_info),
        received_at=datetime.now()
    ))
    db.commit()

def looks_like_work_schedule(text: str) -> bool:
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
    clear_existing_work_schedule_entries(user.user_id, source_info, db)
    entries = []
    for index, task_data in enumerate(structured_tasks):
        shift_key = f"{task_data.get('due_date', 'no-date')}:{task_data.get('end_date', 'no-end')}:{index}"
        shift_source = f"{source_info}:work-shift:{shift_key}"
        shift_content = f"Uploaded work schedule for {user.preferred_name or user.username}: {task_data.get('title', 'Work shift')}"
        entries.append((task_data, shift_content, shift_source))
    return await save_structured_task_entries(entries, user.user_id, db)

def clear_existing_work_schedule_entries(user_id: int, source_info: str, db: Session):
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

def google_calendar_event_to_entry(event: dict, cal_name: str = ""):
    start = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
    end = event.get('end', {}).get('dateTime') or event.get('end', {}).get('date')
    if not start:
        return None

    is_all_day = len(str(start)) == 10
    due_date = f"{start}T12:00:00Z" if is_all_day else start
    end_date = end
    if is_all_day and end and len(str(end)) == 10:
        try:
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

def collect_google_calendar_entries(calendar, db: Session, user_id: int, max_results: int = 2500):
    summary = {"calendar": 0, "calendar_already_scanned": 0, "calendar_skipped": 0}
    sync_entries = []
    past_days = int(os.environ.get("GOOGLE_CALENDAR_SYNC_PAST_DAYS", "30"))
    time_min = (datetime.utcnow() - timedelta(days=past_days)).isoformat() + 'Z'

    # Discover every calendar the user has (primary, birthdays, holidays, shared, etc.)
    # Requires calendar.readonly scope; falls back to primary-only if scope is missing.
    # Build list of (calendar_id, calendar_name) tuples so we can tag birthday events.
    calendar_list: list[tuple[str, str]] = []
    try:
        page_token = None
        while True:
            cal_list_result = calendar.calendarList().list(pageToken=page_token).execute()
            for cal in cal_list_result.get('items', []):
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

def cleanup_classroom_noise_tasks(db: Session, user_id: int) -> int:
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

def classroom_item_to_entry(classroom, course: dict, item: dict, utc_offset_minutes: int = 0):
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
    due_date = google_due_to_iso(item.get("dueDate", {}), item.get("dueTime"), utc_offset_minutes)
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

def collect_classroom_entries(classroom, db: Session, user_id: int, utc_offset_minutes: int = 0):
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

            if not is_actionable_classroom_item(item):
                summary["skipped"] += 1
                mark_source_scanned(db, user_id, source_info, f"Skipped Classroom post: {item.get('title', '')}")
                continue

            try:
                sync_entries.append(classroom_item_to_entry(classroom, course, item, utc_offset_minutes))
                summary["classroom"] += 1
            except Exception:
                summary["skipped"] += 1
                mark_source_scanned(db, user_id, source_info, f"Skipped Classroom post: {item.get('title', '')}")

    return sync_entries, summary

@app.get("/sync-gmail")
async def sync_gmail(user_id: int):
    return await asyncio.to_thread(sync_gmail_blocking, user_id)

def sync_gmail_blocking(user_id: int):
    db = SessionLocal()
    try:
        ensure_user_exists(user_id, db)
        creds = get_google_creds(user_id)
        if not creds:
            return google_auth_required_response(user_id)
        service = build('gmail', 'v1', credentials=creds)
        # Fetch the 5 most recent emails
        results = service.users().messages().list(userId='me', maxResults=5).execute()
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

@app.get("/sync-classroom")
async def sync_classroom(user_id: int, tz_offset: int = 0):
    return await asyncio.to_thread(sync_classroom_blocking, user_id, tz_offset)

def sync_classroom_blocking(user_id: int, tz_offset: int = 0):
    db = SessionLocal()
    try:
        ensure_user_exists(user_id, db)
        creds = get_google_creds(user_id)
        if not creds:
            return google_auth_required_response(user_id)

        classroom = build('classroom', 'v1', credentials=creds)
        sync_entries, summary = collect_classroom_entries(classroom, db, user_id, tz_offset)
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

@app.get("/sync-all")
async def sync_all(user_id: int, tz_offset: int = 0):
    return await asyncio.to_thread(sync_all_blocking, user_id, tz_offset)

def sync_all_blocking(user_id: int, tz_offset: int = 0):
    db = SessionLocal()
    try:
        ensure_user_exists(user_id, db)
        creds = get_google_creds(user_id)
        if not creds:
            # If no credentials, return the auth URL for the frontend to redirect
            return google_auth_required_response(user_id)

        classroom = build('classroom', 'v1', credentials=creds)
        calendar = build('calendar', 'v3', credentials=creds)

        summary = {"classroom": 0, "calendar": 0, "gtasks": 0}
        sync_entries = []

        # 1. Classroom assignments
        try:
            classroom_entries, classroom_summary = collect_classroom_entries(classroom, db, user_id, tz_offset)
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

async def auto_sync_user(user_id: int, db: Session):
    creds = get_google_creds(user_id)
    if not creds:
        return {"gmail": 0, "classroom": 0, "calendar": 0}

    gmail_count = 0
    try:
        service = build('gmail', 'v1', credentials=creds)
        results = service.users().messages().list(userId='me', maxResults=5).execute()
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
        entries, summary = collect_classroom_entries(classroom, db, user_id)
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
    # Find all users who have a stored Google token in the database.
    return [
        u.user_id for u in
        db.query(User).filter(User.google_token_json.isnot(None)).all()
    ]

def run_auto_sync_once_blocking():
    db = SessionLocal()
    try:
        for user_id in get_auto_sync_user_ids(db):
            asyncio.run(auto_sync_user(user_id, db))
    finally:
        db.close()

async def run_auto_sync_once():
    await asyncio.to_thread(run_auto_sync_once_blocking)

async def auto_sync_loop():
    await asyncio.sleep(10)
    while True:
        try:
            await run_auto_sync_once()
        except Exception as exc:
            print(f"Auto sync loop error: {exc}")
        await asyncio.sleep(AUTO_SYNC_INTERVAL_SECONDS)

@app.on_event("startup")
async def start_auto_sync():
    global auto_sync_task
    ensure_database_schema()
    if AUTO_SYNC_ENABLED and auto_sync_task is None:
        auto_sync_task = asyncio.create_task(auto_sync_loop())

@app.on_event("shutdown")
async def stop_auto_sync():
    global auto_sync_task
    if auto_sync_task:
        auto_sync_task.cancel()
        auto_sync_task = None

# --- REUSABLE PROCESSING LOGIC ---
async def process_and_save_tasks(text_content, user_id, source_info, db, current_time=None):
    structured_tasks = await asyncio.to_thread(extract_task_from_text, text_content, current_time)
    return await save_structured_tasks(structured_tasks, text_content, user_id, source_info, db)

async def save_structured_tasks(structured_tasks, text_content, user_id, source_info, db):
    if not structured_tasks:
        # Return a success with 0 tasks instead of a 500 error
        return {"status": "success", "task_ids": [], "message": "No actionable tasks found in input."}

    entries = [(task_data, text_content, source_info) for task_data in structured_tasks]
    return await save_structured_task_entries(entries, user_id, db)

async def save_structured_task_entries(entries, user_id, db):
    if not entries:
        return {"status": "success", "task_ids": [], "message": "No actionable tasks found in input."}

    try:
        task_ids = []
        duplicate_index = build_duplicate_index(db, user_id)

        # Pre-load all source markers for this batch in a single query
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
@app.get("/tasks")
async def get_tasks(user_id: int, db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.owner_id == user_id).all()

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

@app.get("/users/{user_id}/settings")
async def get_user_settings(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        **user_settings_payload(user)
    }

@app.patch("/users/{user_id}/settings")
async def update_user_settings(user_id: int, settings: UserSettingsUpdate, db: Session = Depends(get_db)):
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

@app.post("/users/{user_id}/2fa/send-test")
async def send_two_factor_test(user_id: int, request: TwoFactorSendRequest, db: Session = Depends(get_db)):
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
async def verify_two_factor_setup(user_id: int, request: TwoFactorVerifyRequest, db: Session = Depends(get_db)):
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
async def get_task_history(user_id: int, db: Session = Depends(get_db), limit: int = 50):
    """
    Fetches recently completed or deleted tasks for a user.
    """
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
async def update_task(task_id: int, task_update: TaskUpdate, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.task_id == task_id).first()
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
async def bulk_update_tasks(action: BulkTaskAction, status: str = "deleted", db: Session = Depends(get_db)):
    if status not in {"deleted", "completed", "pending"}:
        raise HTTPException(status_code=400, detail="Unsupported bulk status.")

    task_ids = list(dict.fromkeys(action.task_ids or []))
    if not task_ids:
        raise HTTPException(status_code=400, detail="No tasks selected.")

    updated = db.query(Task).filter(Task.task_id.in_(task_ids)).update(
        {Task.status: status},
        synchronize_session=False
    )
    db.commit()
    return {"message": f"Updated {updated} tasks.", "updated": updated}

@app.delete("/tasks/bulk/permanent")
async def bulk_permanent_delete_tasks(action: BulkTaskAction, db: Session = Depends(get_db)):
    task_ids = list(dict.fromkeys(action.task_ids or []))
    if not task_ids:
        raise HTTPException(status_code=400, detail="No tasks selected.")

    deleted = db.query(Task).filter(Task.task_id.in_(task_ids)).delete(synchronize_session=False)
    db.commit()
    return {"message": f"Permanently deleted {deleted} tasks.", "deleted": deleted}

@app.delete("/tasks/{task_id}/permanent")
async def permanent_delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete(task)
    db.commit()
    return {"message": "Task permanently deleted"}
