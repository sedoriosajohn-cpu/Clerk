from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import sqlite3

app = FastAPI()

# matches the raw_inputs" table structure
class RawInputRequest(BaseModel):
    content: str
    source_type: Optional[str] = "text"
    source_id: Optional[str] = None

# matches "tasks" table structure for when we return data
class TaskResponse(BaseModel):
    task_id: int
    title: str
    due_date: Optional[str]
    status: str
    confidence: float
    

@app.post("/ingest")
async def ingest_data(data: RawInputRequest):
    try:
        conn = sqlite3.connect('clerk.db')
        cursor = conn.cursor()
        
        # Inserting the raw text into the DB
        cursor.execute(
            "INSERT INTO raw_inputs (content, source_type, source_id) VALUES (?, ?, ?)",
            (data.content, data.source_type, data.source_id)
        )
        
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        
        return {"message": "Success", "raw_id": new_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/tasks")
async def get_all_tasks():
    conn = sqlite3.connect('clerk.db')
    conn.row_factory = sqlite3.Row  # allows access to columns by name 
    cursor = conn.cursor()
    
    cursor.execute("SELECT task_id, title, due_date, status, confidence FROM tasks")
    rows = cursor.fetchall()
    
    # Convert database rows into a list of dictionaries
    tasks = [dict(row) for row in rows]
    
    conn.close()
    return {"tasks": tasks}