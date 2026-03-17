from .extractor import extract_task_from_text
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import sqlite3
import os

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "clerk.db")

class UserInput(BaseModel):
    content: str
    source_type: Optional[str] = "text"
    source_id: Optional[str] = None

@app.post("/ingest")
async def ingest_task(data: UserInput):
    structured_data = extract_task_from_text(data.content)
    
    if not structured_data:
        raise HTTPException(status_code=500, detail="AI Extraction failed. Check terminal for errors.")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            #Save the RAW input first
            cursor.execute(
                "INSERT INTO raw_inputs (content, source_type, source_id) VALUES (?, ?, ?)",
                (data.content, data.source_type, data.source_id)
            )
            new_raw_id = cursor.lastrowid

            #Save the AI's results into the TASKS table
            cursor.execute(
                """INSERT INTO tasks (
                    raw_id, title, due_date, due_text, 
                    assignee, priority, confidence, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_raw_id,
                    structured_data.get("title"),
                    structured_data.get("due_date"), # ISO format for sorting
                    structured_data.get("due"),      # Human format for display
                    structured_data.get("assignee", "me"),
                    structured_data.get("priority", "normal"),
                    structured_data.get("confidence", 0),
                    "pending"
                )
            )
            
            conn.commit()

            return {
                "message": "Task ingested successfully",
                "task_id": cursor.lastrowid,
                "extracted": structured_data
            }
            
    except Exception as e:
        print(f"Database Error: {e}")
        raise HTTPException(status_code=500, detail="Database insertion failed.")

@app.get("/tasks")
async def get_all_tasks():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks")
        return {"tasks": [dict(row) for row in cursor.fetchall()]}
    
@app.delete("/tasks/{task_id}")
async def delete_task(task_id: int):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            #check if it exists first
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Task not found")
            
            #deletes just the task
            cursor.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            conn.commit()
            return {"message": f"Task {task_id} deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
