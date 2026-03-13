from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import sqlite3
import os

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "clerk.db")

class RawInputRequest(BaseModel):
    content: str
    source_type: Optional[str] = "text"
    source_id: Optional[str] = None

@app.post("/ingest")
async def ingest_data(data: RawInputRequest):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO raw_inputs (content, source_type, source_id) VALUES (?, ?, ?)",
                (data.content, data.source_type, data.source_id)
            )
            new_id = cursor.lastrowid
            return {"message": "Success", "raw_id": new_id}
    except Exception as e:
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
