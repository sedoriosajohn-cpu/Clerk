from .extractor import extract_task_from_text
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, ForeignKey
from scripts.init_db import engine, Task, RawInput, sessionmaker

app = FastAPI()

def get_db():
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class UserInput(BaseModel):
    content: str
    source_type: Optional[str] = "text"
    source_id: Optional[str] = None

@app.post("/ingest")
async def ingest_task(data: UserInput, db: Session = Depends(get_db)):
    structured_data = extract_task_from_text(data.content)
    
    if not structured_data:
        raise HTTPException(status_code=500, detail="AI Extraction failed.")

    try:
        #saves raw input
        new_raw = RawInput(
            content=data.content,
            source_type=data.source_type,
            source_id=data.source_id
        )
        db.add(new_raw)
        db.flush()

        #Save the AI's results into the TASKS table
        new_task = Task(
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
        db.commit() #saves everything to neon 

        return {"status": "success", "task_id": new_task.task_id}

    except Exception as e:
        db.rollback()
        print(f"Database Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tasks")
async def get_tasks(db: Session = Depends(get_db)):
    tasks = db.query(Task).all()
    return tasks
    
@app.post("/tasks/delete/{task_id}")
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
