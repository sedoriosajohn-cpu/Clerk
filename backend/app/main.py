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
    # 1. Get the AI to extract the task
    structured_data = extract_task_from_text(data.content)
    
    # Debugging: If structured_data is None, the AI extraction failed
    if not structured_data:
        print("AI extraction returned None. Check your API key and network.")
        raise HTTPException(status_code=500, detail="AI Extraction failed. Is your .env set up?")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            
            # 2. Save the RAW input
            cursor.execute(
                "INSERT INTO raw_inputs (content, source_type, source_id) VALUES (?, ?, ?)",
                (data.content, data.source_type, data.source_id)
            )
            new_raw_id = cursor.lastrowid

            # 3. Save to the TASKS table using your specific columns
            cursor.execute(
                """INSERT INTO tasks (raw_id, title, due_date, due_text, priority, confidence) 
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    new_raw_id,
                    structured_data.get("title"),
                    structured_data.get("due_date"),     
                    structured_data.get("due_text"),     
                    structured_data.get("priority", "normal").lower(),
                    structured_data.get("confidence")
                )
            )
            
            conn.commit()

            return {
                "message": "Success", 
                "raw_id": new_raw_id, 
                "extracted": structured_data 
            }
            
    except Exception as e:
        print(f"Database Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
