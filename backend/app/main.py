from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from .extractor import extract_task_from_text
from scripts.init_db import SessionLocal, Task, RawInput, User
from datetime import datetime

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
        
class LoginRequest(BaseModel):
    username: str
    password: str
    
class UserInput(BaseModel):
    content: str
    source_type: Optional[str] = "text"
    source_id: Optional[str] = None
    user_id: int
    
class TaskUpdate(BaseModel):
    due_date: Optional[str] = None
    title: Optional[str] = None
    priority: Optional[str] = None


@app.post("/login")
async def login_user(data: LoginRequest, db: Session = Depends(get_db)):
    # Look for the user by username
    user = db.query(User).filter(User.username == data.username).first()
    
    # Check if user exists and password matches (simple check for now)
    if not user or user.password_hash != data.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Return the user_id so the frontend knows who is logged in
    return {"user_id": user.user_id, "username": user.username}

@app.post("/ingest")
async def ingest_task(data: UserInput, db: Session = Depends(get_db)):
    structured_data = extract_task_from_text(data.content)
    
    if not structured_data:
        raise HTTPException(status_code=500, detail="AI Extraction failed.")

    try:
        #Saves the RAW input
        new_raw = RawInput(
            content=data.content,
            source_type=data.source_type,
            source_id=data.source_id,
            received_at=datetime.now()
        )
        db.add(new_raw)
        db.flush()

        #Save the AI's results into the TASKS table
        new_task = Task(
            owner_id=data.user_id,
            raw_id=new_raw.raw_id,
            title=structured_data.get("title"),
            due_date=str(structured_data.get("due_date")),
            due_text=structured_data.get("due_text"),
            assignee=structured_data.get("assignee", "me"),
            priority=structured_data.get("priority", "normal"),
            confidence=structured_data.get("confidence"),
            status="pending"
        )
        db.add(new_task)
        db.commit()

        return {"status": "success", "task_id": new_task.task_id}

    except Exception as e:
        db.rollback()
        print(f"Database Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tasks")
async def get_tasks(user_id: int, db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.owner_id == user_id).all()
    return tasks
    
@app.delete("/tasks/{task_id}")
async def delete_task(task_id: int, db: Session = Depends(get_db)):
    task_to_delete = db.query(Task).filter(Task.task_id == task_id).first()
    
    if not task_to_delete:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        db.delete(task_to_delete)
        db.commit()
        return {"status": "success", "message": f"Task {task_id} deleted"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
@app.patch("/tasks/{task_id}")
async def update_task(task_id: int, task_update: TaskUpdate, db: Session = Depends(get_db)):
    
    task = db.query(Task).filter(Task.task_id == task_id).first() 
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if task_update.title:
        task.title = task_update.title
    if task_update.priority:
        task.priority = task_update.priority
    
    if task_update.due_date is not None:
        new_date = task_update.due_date
        if len(new_date) == 10:
            new_date += "T12:00:00Z" 
    
        task.due_date = new_date
        
    db.commit()
    db.refresh(task)
    return {"message": "Updated successfully"}