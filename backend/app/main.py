from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from .extractor import extract_task_from_text
from scripts.init_db import SessionLocal, Task, RawInput, User
from datetime import datetime, timedelta
import base64
import fitz
import os.path
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/calendar.events.readonly',
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.me.readonly'
]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- SCHEMAS ---
class LoginRequest(BaseModel):
    username: str
    password: str

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
    priority: Optional[str] = None
    item_type: Optional[str] = None
    status: Optional[str] = None
    is_all_day: Optional[bool] = None

class UserSettingsUpdate(BaseModel):
    preferred_name: Optional[str] = None
    dark_mode: Optional[bool] = None
    notifications_enabled: Optional[bool] = None

# --- AUTH ROUTES ---
@app.post("/login")
async def login_user(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user or user.password_hash != data.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"user_id": user.user_id, "username": user.username}

@app.post("/register")
async def register_user(data: LoginRequest, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.username == data.username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already taken")
    new_user = User(username=data.username, password_hash=data.password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "User created", "user_id": new_user.user_id}

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

    try:
        if file_type == "application/pdf":
            # Read PDF bytes
            pdf_bytes = await file.read()
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            for page in doc:
                content += page.get_text()
            doc.close()
        elif file_type in ["text/plain", "text/markdown"]:
            # Read Text bytes
            text_bytes = await file.read()
            content = text_bytes.decode("utf-8")
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {file_type}")

        if not content.strip():
            raise HTTPException(status_code=400, detail="The uploaded file appears to be empty.")

        # Reuse the saving logic
        return await process_and_save_tasks(content, user_id, f"file: {file.filename}", db, local_time)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File processing error: {str(e)}")

# --- GMAIL SYNC ---
def get_gmail_service():
    creds = None
    # token.json stores the user's access/refresh tokens
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=8080)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def get_email_body(payload):
    """Helper to extract plain text body from Gmail payload."""
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
    elif 'body' in payload and 'data' in payload['body']:
        return base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
    return ""

@app.get("/sync-gmail")
async def sync_gmail(user_id: int, db: Session = Depends(get_db)):
    try:
        service = get_gmail_service()
        # Fetch the 5 most recent emails
        results = service.users().messages().list(userId='me', maxResults=5).execute()
        messages = results.get('messages', [])
        
        processed_count = 0
        for msg in messages:
            m = service.users().messages().get(userId='me', id=msg['id']).execute()
            # Fetch full body instead of just the snippet
            body = get_email_body(m.get('payload', {})) or m.get('snippet', '')
            if body:
                try:
                    await process_and_save_tasks(body, user_id, f"gmail: {msg['id']}", db)
                    processed_count += 1
                except HTTPException:
                    # Skip emails where no tasks were found
                    continue
            
        return {"status": "success", "message": f"Scanned {len(messages)} emails and extracted tasks from {processed_count} of them."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sync-all")
async def sync_all(user_id: int, db: Session = Depends(get_db)):
    service_gmail = get_gmail_service() # Uses your existing function
    
    # 1. Create a service for Classroom
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    classroom = build('classroom', 'v1', credentials=creds)
    calendar = build('calendar', 'v3', credentials=creds)

    summary = {"classroom": 0, "calendar": 0}

    # 2. Fetch Classroom Assignments
    try:
        courses_result = classroom.courses().list(pageSize=5).execute()
        for course in courses_result.get('courses', []):
            cw_result = classroom.courses().courseWork().list(courseId=course['id']).execute()
            for item in cw_result.get('courseWork', []):
                due = item.get('dueDate', {})
                due_str = f"{due.get('month')}/{due.get('day')}/{due.get('year')}" if due else "No date"
                
                # Create a prompt-friendly string for the AI to ingest
                content = f"Classroom Assignment: {item['title']} for {course['name']}. Due: {due_str}. Instructions: {item.get('description', '')}"
                try:
                    await process_and_save_tasks(content, user_id, f"classroom: {item['id']}", db)
                    summary["classroom"] += 1
                except: continue
    except Exception as e:
        print(f"Classroom sync error: {e}")

    # 3. Fetch Calendar Events
    try:
        now = datetime.utcnow().isoformat() + 'Z'
        cal_result = calendar.events().list(calendarId='primary', timeMin=now, maxResults=10).execute()
        for event in cal_result.get('items', []):
            # Only ingest actual events, not just "free" slots
            if event.get('summary'):
                start = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
                content = f"Calendar Event: {event['summary']} starting {start}. Description: {event.get('description', '')}"
                try:
                    await process_and_save_tasks(content, user_id, f"calendar: {event['id']}", db)
                    summary["calendar"] += 1
                except: continue
    except Exception as e:
        print(f"Calendar sync error: {e}")

    return {"status": "success", "processed": summary}

# --- REUSABLE PROCESSING LOGIC ---
async def process_and_save_tasks(text_content, user_id, source_info, db, current_time=None):
    structured_tasks = extract_task_from_text(text_content, current_time)
    
    if not structured_tasks:
        raise HTTPException(status_code=500, detail="AI Extraction failed to find any tasks.")

    try:
        new_raw = RawInput(
            content=text_content[:500], # Save snippet to avoid DB bloat
            source_type=source_info,
            received_at=datetime.now()
        )
        db.add(new_raw)
        db.flush()

        task_ids = []
        for task_data in structured_tasks:
            new_task = Task(
                owner_id=user_id,
                raw_id=new_raw.raw_id,
                title=task_data.get("title"),
                due_date=task_data.get("due_date"), # Store None if not provided, not "None" string
                end_date=task_data.get("end_date"),
                due_text=task_data.get("due"),
                assignee=task_data.get("assignee", "me"),
                priority=task_data.get("priority", "normal"),
                is_all_day=1 if task_data.get("is_all_day") else 0,
                item_type=task_data.get("item_type", "task"),
                confidence=task_data.get("confidence"),
                status="pending"
            )
            db.add(new_task)
            db.flush()
            task_ids.append(new_task.task_id)

        db.commit()
        return {"status": "success", "task_ids": task_ids, "message": f"Extracted {len(task_ids)} tasks"}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# --- TASK MANAGEMENT ROUTES ---
@app.get("/tasks")
async def get_tasks(user_id: int, db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.owner_id == user_id).all()
    return {"tasks": tasks}

@app.get("/users/{user_id}/settings")
async def get_user_settings(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "preferred_name": user.preferred_name or user.username,
        "dark_mode": bool(user.dark_mode),
        "notifications_enabled": bool(user.notifications_enabled)
    }

@app.patch("/users/{user_id}/settings")
async def update_user_settings(user_id: int, settings: UserSettingsUpdate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if settings.preferred_name is not None:
        user.preferred_name = settings.preferred_name
    if settings.dark_mode is not None:
        user.dark_mode = 1 if settings.dark_mode else 0
    if settings.notifications_enabled is not None:
        user.notifications_enabled = 1 if settings.notifications_enabled else 0
        
    db.commit()
    return {"message": "Settings updated"}

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
    return {"tasks": history_tasks}

@app.patch("/tasks/{task_id}")
async def update_task(task_id: int, task_update: TaskUpdate, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.task_id == task_id).first() 
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if task_update.title: task.title = task_update.title
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

@app.delete("/tasks/{task_id}/permanent")
async def permanent_delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete(task)
    db.commit()
    return {"message": "Task permanently deleted"}