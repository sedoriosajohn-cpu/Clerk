from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from .extractor import extract_task_from_text
from scripts.init_db import SessionLocal, Task, RawInput, User
from datetime import datetime, timedelta
import fitz

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